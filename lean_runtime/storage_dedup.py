"""Byte-verified CoW compaction without weakening build compatibility keys.

A persistent, disposable index remembers unmodified files and possible donors.
Donors are only hints: their CoW snapshot is hashed before replacing a target.
Each replacement preserves target metadata, paths and independent write behavior.
No package, trace, or toolchain is deleted. APFS shared extents are not visible
in st_blocks; cloned_bytes is NOT a measurement of reclaimed disk space.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ._cow import copy_metadata, reflink
from .errors import EnvironmentError
from .events import current
from .locking import FileLock, LockPaths
from .package_ids import package_directories, package_id_matches

MINIMUM_BYTES = 64 * 1024


@dataclass
class DedupReport:
    dry_run: bool
    supported: bool | None = None
    scanned_files: int = 0
    hashed_bytes: int = 0
    duplicate_bytes: int = 0
    cloned_bytes: int = 0
    cloned_files: int = 0
    indexed_files: int = 0
    skipped_busy: int = 0
    unsupported_files: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"schema": "lean-runtime-storage-dedup/1", **asdict(self)}


def _signature(info: os.stat_result) -> str:
    return ":".join(
        str(n)
        for n in (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
            info.st_mode,
            info.st_nlink,
        )
    )


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _files(package: Path) -> Iterator[tuple[Path, os.stat_result]]:
    # Include subDir packages, but never traverse borrowed trees or Git objects.
    for directory, directories, _names in os.walk(package, followlinks=False):
        root = Path(directory)
        directories[:] = [n for n in directories if n != ".git" and not (root / n).is_symlink()]
        if root.name == ".lake":
            directories[:] = [n for n in directories if n == "build"]
        for name in _names:
            path = root / name
            if ".lake" not in path.relative_to(package).parts:
                continue
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_size >= MINIMUM_BYTES and info.st_nlink == 1:
                yield path, info


def _preserve_metadata(target: Path, temporary: Path) -> None:
    # clonefile copies donor xattrs too. Remove any not present on the target.
    if hasattr(os, "listxattr"):
        for name in set(os.listxattr(temporary)) - set(os.listxattr(target)):
            getattr(os, "removexattr")(temporary, name)  # noqa: B009
    copy_metadata(target, temporary)


def _compact_file(
    connection: sqlite3.Connection,
    path: Path,
    info: os.stat_result,
    report: DedupReport,
    package_roots: tuple[Path, ...],
) -> None:
    report.scanned_files += 1
    signature = _signature(info)
    previous = connection.execute(
        "SELECT signature FROM files WHERE path = ?", (str(path),)
    ).fetchone()
    if previous == (signature,):
        report.indexed_files += 1
        return
    digest = _digest(path)
    report.hashed_bytes += info.st_size
    if _signature(path.lstat()) != signature:
        return
    donor = connection.execute("SELECT path FROM donors WHERE digest = ?", (digest,)).fetchone()
    if donor is not None and donor[0] != str(path):
        source = Path(donor[0])
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.dedup-tmp")
        try:
            # The disposable index is not authority to read outside this package store.
            if (
                not any(source.is_relative_to(root) for root in package_roots)
                or source.resolve() != source
            ):
                raise FileNotFoundError(str(source))
            source_info = source.lstat()
            if not stat.S_ISREG(source_info.st_mode) or source_info.st_size != info.st_size:
                raise FileNotFoundError(str(source))
            if report.dry_run:
                identical = _digest(source) == digest
            else:
                reflink(source, temporary)
                identical = _digest(temporary) == digest
            if identical:
                report.duplicate_bytes += info.st_size
                if not report.dry_run:
                    _preserve_metadata(path, temporary)
                    temporary_info = temporary.stat()
                    if (temporary_info.st_uid, temporary_info.st_gid) != (info.st_uid, info.st_gid):
                        os.chown(temporary, info.st_uid, info.st_gid)
                    os.utime(temporary, ns=(info.st_atime_ns, info.st_mtime_ns))
                    if _signature(path.lstat()) != signature:
                        return
                    temporary.replace(path)
                    report.cloned_files += 1
                    report.cloned_bytes += info.st_size
                    signature = _signature(path.lstat())
            else:
                connection.execute("DELETE FROM donors WHERE digest = ?", (digest,))
        except FileNotFoundError:
            connection.execute("DELETE FROM donors WHERE digest = ?", (digest,))
        except OSError as exc:
            if exc.errno not in {
                errno.ENOTSUP,
                errno.EOPNOTSUPP,
                errno.EXDEV,
                errno.ENOSYS,
                errno.ENOTTY,
                errno.EINVAL,
            }:
                raise
            report.unsupported_files += 1
        finally:
            temporary.unlink(missing_ok=True)
    connection.execute("INSERT OR IGNORE INTO donors VALUES (?, ?)", (digest, str(path)))
    connection.execute("INSERT OR REPLACE INTO files VALUES (?, ?)", (str(path), signature))


def deduplicate(
    home: Path,
    *,
    dry_run: bool = True,
    package_ids: tuple[str, ...] | None = None,
    already_locked: bool = False,
) -> DedupReport:
    """Compact shared build outputs, skipping busy packages, with no hard links.

    already_locked is for the shared build-lock owner only. A dry run uses an
    in-memory index, does not create the persistent index or replace any files.
    The index lock is always acquired AFTER package build locks.
    """
    from contextlib import ExitStack

    from .store import invalidate_storage_ledger

    home = home.resolve()
    locks = LockPaths(home)
    report = DedupReport(dry_run)
    if not dry_run:
        # Fail closed on filesystems without CoW; never replace duplicates by hard
        # links or full copies. Probe once, rather than once per duplicate file.
        with tempfile.TemporaryDirectory(prefix=".dedup-probe-", dir=home) as raw:
            probe = Path(raw)
            (probe / "source").write_bytes(b"CoW probe")
            try:
                reflink(probe / "source", probe / "clone")
            except OSError as exc:
                if exc.errno not in {
                    errno.ENOTSUP,
                    errno.EOPNOTSUPP,
                    errno.EXDEV,
                    errno.ENOSYS,
                    errno.ENOTTY,
                    errno.EINVAL,
                }:
                    raise
                report.supported = False
                return report
        report.supported = True
    package_roots = (home / "project-packages", home / "compositions" / "packages")
    packages = package_directories(package_roots[0])
    packages += sorted(
        p
        for p in package_roots[1].glob("composition_package_*")
        if re.fullmatch(r"composition_package_[0-9a-f]{64}", p.name) and p.is_dir()
    )
    if package_ids is not None:
        packages = [p for p in packages if p.name in package_ids]
    # Dry-run hints are shared between packages without changing the store.
    memory = sqlite3.connect(":memory:") if dry_run else None
    try:
        for package in packages:
            if package.is_symlink():
                continue
            composition = package.parent == package_roots[1]
            if not composition:
                try:
                    marker = json.loads((package / ".lean-runtime-package.json").read_text())
                except (OSError, ValueError):
                    continue
                if not isinstance(marker, dict) or not package_id_matches(marker, package.name):
                    continue
            with ExitStack() as stack:
                if not already_locked:
                    try:
                        if composition:
                            stack.enter_context(
                                FileLock(
                                    home / "compositions" / ".locks" / "lifecycle.lock",
                                    timeout=0,
                                )
                            )
                        stack.enter_context(FileLock(locks.package_build(package.name), timeout=0))
                    except EnvironmentError:
                        report.skipped_busy += 1
                        continue
                try:
                    stack.enter_context(FileLock(locks.package(package.name), timeout=0))
                except EnvironmentError:
                    report.skipped_busy += 1
                    continue
                if memory is None:
                    try:
                        stack.enter_context(
                            FileLock(
                                locks.collector("deduplication"),
                                timeout=0 if already_locked else 300,
                            )
                        )
                    except EnvironmentError:
                        report.skipped_busy += 1
                        continue
                    connection = sqlite3.connect(home / "deduplication.sqlite3")
                    stack.callback(connection.close)
                    connection.execute("PRAGMA journal_mode=WAL")
                else:
                    connection = memory
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS donors "
                    "(digest TEXT PRIMARY KEY, path TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS files "
                    "(path TEXT PRIMARY KEY, signature TEXT NOT NULL)"
                )
                for path, info in _files(package):
                    _compact_file(connection, path, info, report, package_roots)
                connection.commit()
                current().emit(
                    "storage.deduplicated",
                    f"Compacted {package.name}",
                    phase="storage",
                    **report.to_dict(),
                )
    except sqlite3.Error as exc:
        raise EnvironmentError(
            "could not use the disposable deduplication index; stop compactors and "
            "remove deduplication.sqlite3 (and its -wal/-shm sidecars) to rebuild it: "
            f"{exc}"
        ) from exc
    finally:
        if memory is not None:
            memory.close()
        if report.cloned_files:
            invalidate_storage_ledger(home)
    return report
