"""Internal safe creation and extraction of tar and OCI layout archives.

Every member name goes through the portable relative-path policy; symlinks
may only point inside the archive; entry counts and byte totals are bounded."""

from __future__ import annotations

import gzip
import io
import os
import shutil
import tarfile
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import nullcontext
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from ._relpath import safe_relative_posix
from .errors import EnvironmentError
from .events import current
from .progress import CountedProgress

MAX_BUNDLE_BYTES = 20 * 1024**3

MAX_FILES = 2_000_000


class BinaryReader(Protocol):
    def read(self, size: int = -1) -> bytes: ...

    def readinto(self, buffer: Any) -> int | None: ...

    def seek(self, offset: int, whence: int = 0) -> int: ...

    def tell(self) -> int: ...


class ProgressReader:
    """Report bytes after a consumer has actually read them."""

    def __init__(self, handle: BinaryReader, progress: CountedProgress, *, offset: int = 0) -> None:
        self._handle = handle
        self._progress = progress
        self._offset = offset
        self._furthest = offset

    def _report(self) -> None:
        position = self._offset + self._handle.tell()
        if position > self._furthest:
            self._furthest = position
            self._progress.advance(to=position)

    def read(self, size: int = -1) -> bytes:
        data = self._handle.read(size)
        self._report()
        return data

    def readinto(self, buffer: Any) -> int:
        count = self._handle.readinto(buffer)
        self._report()
        return count if count is not None else 0

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._handle.seek(offset, whence)

    def tell(self) -> int:
        return self._handle.tell()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._handle, name)


def normalized_info(name: str, *, mode: int, kind: bytes = tarfile.REGTYPE) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mode = mode
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def tree_entries(
    root: Path,
    excluded: Path | None = None,
    *,
    excluded_names: frozenset[str] = frozenset(),
    omit_volatile_build_metadata: bool = False,
) -> Iterable[tuple[Path, str]]:
    scan = CountedProgress(
        current().emit,
        "bundle.tree_scan",
        f"Scanning {root.name}",
        1,
        phase="bundle",
    )
    scan.start()
    paths = sorted(root.rglob("*"), key=lambda value: value.relative_to(root).as_posix())
    scan.advance(f"{len(paths)} entries")
    progress = CountedProgress(
        current().emit,
        "bundle.tree_inventory",
        f"Inventorying {root.name}",
        len(paths),
        phase="bundle",
    )
    progress.start()
    for path in paths:
        if excluded is not None and (path == excluded or excluded in path.parents):
            progress.advance(path.relative_to(root).as_posix())
            continue
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] in excluded_names:
            continue
        if (
            omit_volatile_build_metadata
            and ".lake" in relative.parts
            and (
                relative.name.endswith(".trace")
                or relative.name.endswith(".setup.json")
                or relative.name.endswith(".rsp")
            )
        ):
            progress.advance(relative.as_posix())
            continue
        yield path, relative.as_posix()
        progress.advance(relative.as_posix())


def write_tar_gzip(
    root: Path,
    output: Path,
    *,
    excluded: Path | None = None,
    excluded_names: frozenset[str] = frozenset(),
    extra_files: Mapping[str, bytes] | None = None,
    omit_volatile_build_metadata: bool = False,
) -> None:
    with (
        output.open("wb") as raw_output,
        gzip.GzipFile(
            filename="", mode="wb", fileobj=raw_output, compresslevel=6, mtime=0
        ) as compressed,
        tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive,
    ):
        for path, name in tree_entries(
            root,
            excluded,
            excluded_names=excluded_names,
            omit_volatile_build_metadata=omit_volatile_build_metadata,
        ):
            stat = path.lstat()
            mode = stat.st_mode & 0o777
            if path.is_symlink():
                info = normalized_info(name, mode=mode or 0o777, kind=tarfile.SYMTYPE)
                info.linkname = os.readlink(path)
                archive.addfile(info)
            elif path.is_dir():
                archive.addfile(
                    normalized_info(name + "/", mode=mode or 0o755, kind=tarfile.DIRTYPE)
                )
            elif path.is_file():
                info = normalized_info(name, mode=mode or 0o644)
                info.size = stat.st_size
                with path.open("rb") as handle:
                    archive.addfile(info, handle)
            else:
                raise EnvironmentError(f"bundle contains unsupported filesystem entry: {path}")
        for name, data in sorted((extra_files or {}).items()):
            safe_name(name)
            info = normalized_info(name, mode=0o644)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def tar_gzip(root: Path, *, excluded: Path | None = None) -> bytes:
    """Compatibility helper for small tests; production export streams to disk."""
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "layer.tar.gz"
        write_tar_gzip(root, path, excluded=excluded)
        return path.read_bytes()


def oci_archive(entries: dict[str, bytes]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, data in sorted(entries.items()):
            info = normalized_info(name, mode=0o644)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    output = io.BytesIO()
    with gzip.GzipFile(
        filename="", mode="wb", fileobj=output, compresslevel=6, mtime=0
    ) as compressed:
        compressed.write(raw.getvalue())
    return output.getvalue()


def write_oci_archive(entries: dict[str, Path], output: Path) -> None:
    ordered = sorted(entries.items())
    total = sum(path.stat().st_size for _name, path in ordered)
    progress = CountedProgress(
        current().emit,
        "bundle.archive_write",
        "Writing portable archive",
        total,
        phase="bundle",
        unit="bytes",
    )
    progress.start()
    written = 0
    with (
        output.open("wb") as raw_output,
        gzip.GzipFile(
            filename="", mode="wb", fileobj=raw_output, compresslevel=6, mtime=0
        ) as compressed,
        tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive,
    ):
        for name, path in ordered:
            info = normalized_info(name, mode=0o644)
            info.size = path.stat().st_size
            with path.open("rb") as handle:
                archive.addfile(info, ProgressReader(handle, progress, offset=written))
            written += info.size
            progress.advance(name, to=written)


def safe_name(name: str) -> PurePosixPath:
    try:
        return safe_relative_posix(name)
    except ValueError as exc:
        raise EnvironmentError(f"unsafe bundle member path: {name!r}") from exc


def internal_link_target(member: PurePosixPath, linkname: str) -> PurePosixPath:
    link = PurePosixPath(linkname)
    if not linkname or link.is_absolute() or "\\" in linkname or "\x00" in linkname:
        raise EnvironmentError(f"unsafe bundle symlink: {member.as_posix()!r}")
    parts: list[str] = []
    for part in member.parent.joinpath(link).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise EnvironmentError(f"unsafe bundle symlink: {member.as_posix()!r}")
            parts.pop()
        else:
            parts.append(part)
    return PurePosixPath(*parts) if parts else PurePosixPath(".")


def ensure_directories(destination: Path, relative: PurePosixPath) -> None:
    current = destination
    for part in relative.parts:
        if part == ".":
            continue
        current = current / part
        if current.is_symlink():
            raise EnvironmentError(
                f"bundle member traverses an extracted symlink: {relative.as_posix()!r}"
            )
        if current.exists():
            if not current.is_dir():
                raise EnvironmentError(
                    f"bundle member parent is not a directory: {relative.as_posix()!r}"
                )
        else:
            current.mkdir()


def extract_layer(data: bytes | Path, destination: Path) -> None:
    total = 0
    count = 0
    if destination.is_symlink():
        raise EnvironmentError("bundle extraction destination must not be a symlink")
    destination.mkdir(parents=True, exist_ok=True)
    progress: CountedProgress | None = None
    raw_handle: Any = None
    try:
        if isinstance(data, Path):
            raw_handle = data.open("rb")
            progress = CountedProgress(
                current().emit,
                "bundle.layer_extract",
                f"Extracting {destination.name}",
                data.stat().st_size,
                phase="bundle",
                unit="bytes",
            )
            progress.start()
            archive = tarfile.open(  # noqa: SIM115
                fileobj=ProgressReader(raw_handle, progress), mode="r:gz"
            )
        else:
            archive = tarfile.open(  # noqa: SIM115
                fileobj=io.BytesIO(data), mode="r:gz"
            )
    except (tarfile.TarError, OSError) as exc:
        if raw_handle is not None:
            raw_handle.close()
        raise EnvironmentError("bundle layer is not a valid gzip tar archive") from exc
    with archive, raw_handle if raw_handle is not None else nullcontext():
        for member in archive:
            count += 1
            total += member.size
            if count > MAX_FILES or total > MAX_BUNDLE_BYTES:
                raise EnvironmentError("bundle layer exceeds extraction limits")
            relative = safe_name(member.name)
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                ensure_directories(destination, relative)
                target.chmod(member.mode & 0o777)
            elif member.isfile():
                ensure_directories(destination, relative.parent)
                if target.exists() or target.is_symlink():
                    raise EnvironmentError(f"duplicate bundle member: {member.name!r}")
                source = archive.extractfile(member)
                if source is None:
                    raise EnvironmentError(f"bundle member has no content: {member.name}")
                with target.open("wb") as handle:
                    shutil.copyfileobj(source, handle)
                target.chmod(member.mode & 0o777)
            elif member.issym():
                internal_link_target(relative, member.linkname)
                ensure_directories(destination, relative.parent)
                if target.exists() or target.is_symlink():
                    raise EnvironmentError(f"duplicate bundle member: {member.name!r}")
                target.symlink_to(member.linkname)
            else:
                raise EnvironmentError(f"unsupported bundle member: {member.name!r}")
    if progress is not None:
        progress.advance(to=progress.total)


def extract_oci_archive(bundle: Path, destination: Path) -> dict[str, Path]:
    entries: dict[str, Path] = {}
    total = 0
    count = 0
    byte_progress = CountedProgress(
        current().emit,
        "bundle.archive_extract",
        f"Reading {bundle.name}",
        bundle.stat().st_size,
        phase="bundle",
        unit="bytes",
    )
    byte_progress.start()
    raw_handle: Any = None
    try:
        raw_handle = bundle.open("rb")
        archive = tarfile.open(  # noqa: SIM115
            fileobj=ProgressReader(raw_handle, byte_progress), mode="r:gz"
        )
    except (tarfile.TarError, OSError) as exc:
        if raw_handle is not None:
            raw_handle.close()
        raise EnvironmentError(f"could not read OCI bundle: {bundle}") from exc
    with archive, raw_handle:
        for member in archive:
            count += 1
            if count > MAX_FILES:
                raise EnvironmentError("OCI bundle exceeds file-count limit")
            safe_name(member.name)
            if not member.isfile():
                raise EnvironmentError("OCI bundle may contain only regular files")
            total += member.size
            if total > MAX_BUNDLE_BYTES:
                raise EnvironmentError("OCI bundle exceeds import limits")
            source = archive.extractfile(member)
            assert source is not None
            if member.name in entries:
                raise EnvironmentError(f"duplicate OCI bundle member: {member.name}")
            path = destination.joinpath(*PurePosixPath(member.name).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as output:
                shutil.copyfileobj(source, output)
            entries[member.name] = path
    byte_progress.advance(to=byte_progress.total)
    return entries
