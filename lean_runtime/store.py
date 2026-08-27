"""Content-addressed storage for locks, sources, and environments."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ._git import git_command
from ._paths import remove_tree
from .declaration_index import DeclarationShard
from .errors import EnvironmentError
from .events import current
from .lockfiles import EnvironmentLock
from .locking import FileLock
from .package_ids import package_directories
from .progress import CountedProgress
from .serialization import sha256_id, write_json_atomic

STORE_SCHEMA = "lean-runtime-store/2"
PLATFORM_COMPATIBILITY_SCHEMA = "lean-runtime-platform/1"
_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_ENVIRONMENT_ID = re.compile(r"env_[0-9a-f]{64}")
_OCI_BLOB = re.compile(r"[0-9a-f]{64}")
ALIAS_SCHEMA = "lean-runtime-environment-alias/1"
SUPPORTED_BUILD_PROFILE = "release"


def source_snapshot_digest(root: Path) -> str:
    """Hash checked-out content while excluding Git and runtime metadata."""
    scan = CountedProgress(
        current().emit,
        "source.snapshot_scan",
        f"Scanning {root.name}",
        1,
        phase="fingerprint",
    )
    scan.start()
    entries: list[Path] = []
    for directory, directories, filenames in os.walk(root, followlinks=False):
        current_dir = Path(directory)
        symlink_directories = [name for name in directories if (current_dir / name).is_symlink()]
        directories[:] = sorted(
            name
            for name in directories
            if name not in {".git", ".lake"} and name not in symlink_directories
        )
        entries.extend(current_dir / name for name in sorted([*filenames, *symlink_directories]))
    scan.advance(f"{len(entries)} entries")
    progress = CountedProgress(
        current().emit,
        "source.snapshot_digest",
        f"Hashing {root.name}",
        len(entries),
        phase="fingerprint",
    )
    progress.start()
    digest = hashlib.sha256()
    for path in entries:
        relative = path.relative_to(root)
        if relative.as_posix() == ".lean-runtime-source.json":
            progress.advance(relative.as_posix())
            continue
        if path.is_symlink():
            digest.update(b"link\0" + relative.as_posix().encode() + b"\0")
            digest.update(os.readlink(path).encode())
        elif path.is_file():
            mode = path.stat().st_mode & 0o111
            digest.update(
                b"file\0" + relative.as_posix().encode() + b"\0" + str(mode).encode() + b"\0"
            )
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        progress.advance(relative.as_posix())
    return "sha256:" + digest.hexdigest()


def platform_record() -> dict[str, str]:
    return {
        "system": platform.system().lower(),
        "machine": platform.machine().lower(),
        "python_platform": platform.platform(),
    }


def platform_compatibility() -> dict[str, str]:
    """Return only fields that determine compatibility of built artifacts."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    abi = "native"
    if system == "linux":
        libc, _version = platform.libc_ver()
        abi = {"glibc": "gnu", "musl": "musl"}.get(libc.lower(), libc.lower() or "unknown")
    elif system == "windows":
        abi = "msvc"
    return {
        "schema": PLATFORM_COMPATIBILITY_SCHEMA,
        "system": system,
        "machine": machine,
        "abi": abi,
    }


def environment_identity(lock: EnvironmentLock, build_profile: str = "release") -> str:
    if build_profile != SUPPORTED_BUILD_PROFILE:
        raise EnvironmentError(
            f"unsupported build profile {build_profile!r}; only 'release' is implemented"
        )
    return sha256_id(
        "env",
        {
            "schema": STORE_SCHEMA,
            "lock_id": lock.lock_id,
            "platform_compatibility": platform_compatibility(),
            "build_profile": build_profile,
        },
    )


def clone_tree(source: Path, destination: Path) -> None:
    """Clone a tree using copy-on-write where the host supports it."""
    if destination.exists():
        raise EnvironmentError(f"clone destination already exists: {destination}")
    progress = CountedProgress(
        current().emit,
        "storage.tree_clone",
        f"Copying {source.name}",
        1,
        phase="storage",
    )
    progress.start()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Darwin":
        result = subprocess.run(
            # Lake traces are metadata-sensitive: preserve mtimes or a CoW clone of a warm
            # package is interpreted as a cold tree and needlessly rebuilt in full.
            ["cp", "-cRp", str(source), str(destination)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            progress.advance()
            return
    elif platform.system() == "Linux":
        result = subprocess.run(
            ["cp", "--reflink=auto", "-a", str(source), str(destination)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            progress.advance()
            return
    shutil.copytree(source, destination, symlinks=True)
    progress.advance()


def _tree_bytes(root: Path) -> int:
    """Sum regular-file sizes under a directory, tolerating races."""
    if not root.is_dir():
        return 0
    total = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                        elif entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _tree_metrics(root: Path, seen: set[tuple[int, int]]) -> tuple[int, int]:
    """Return materialized bytes and allocated bytes, deduplicating hard links.

    The allocated figure is an estimate: filesystems may share blocks through
    copy-on-write clones without exposing that relationship through ``stat``.
    """
    if not root.is_dir():
        return 0, 0
    materialized = 0
    allocated = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_file(follow_symlinks=False):
                            record = entry.stat(follow_symlinks=False)
                            if os.name == "nt" and not record.st_ino:
                                # DirEntry.stat() leaves st_ino/st_nlink at zero on
                                # Windows; only os.stat() reports hard-link identity.
                                record = os.stat(entry.path)
                            materialized += record.st_size
                            if record.st_nlink > 1 and record.st_ino:
                                identity = (record.st_dev, record.st_ino)
                                if identity in seen:
                                    continue
                                seen.add(identity)
                            blocks = getattr(record, "st_blocks", None)
                            allocated += record.st_size if blocks is None else blocks * 512
                        elif entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            continue
    return materialized, allocated


class WorkspaceLease:
    """Process-held ownership for disposable store workspaces."""

    def __init__(self, store: EnvironmentStore, path: Path, kind: str) -> None:
        self.store = store
        self.path = path
        self.kind = kind
        self._lock = FileLock(store._workspace_lock_path(path))
        self.path.mkdir(parents=True, exist_ok=True)
        self._lock.__enter__()
        try:
            write_json_atomic(
                self.path / ".lean-runtime-workspace.json",
                {
                    "schema": "lean-runtime.workspace/v1",
                    "kind": kind,
                    "pid": os.getpid(),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except BaseException:
            self._lock.__exit__(None, None, None)
            raise

    def close(self) -> None:
        try:
            if self.path.exists():
                remove_tree(self.path)
        finally:
            self._lock.__exit__(None, None, None)


@dataclass(frozen=True, slots=True)
class CleanupReport:
    candidates: tuple[str, ...]
    removed: tuple[str, ...]
    retained: tuple[str, ...]
    dry_run: bool
    candidate_bytes: int = 0
    reclaimed_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": list(self.candidates),
            "removed": list(self.removed),
            "retained": list(self.retained),
            "dry_run": self.dry_run,
            "candidate_bytes": self.candidate_bytes,
            "reclaimed_bytes": self.reclaimed_bytes,
        }


@dataclass(frozen=True, slots=True)
class DownloadCleanupReport:
    candidates: tuple[str, ...]
    removed: tuple[str, ...]
    retained: tuple[str, ...]
    reclaimed_bytes: int
    dry_run: bool
    candidate_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": list(self.candidates),
            "removed": list(self.removed),
            "retained": list(self.retained),
            "reclaimed_bytes": self.reclaimed_bytes,
            "dry_run": self.dry_run,
            "candidate_bytes": self.candidate_bytes,
        }


@dataclass(frozen=True, slots=True)
class EnvironmentUsage:
    """Disk footprint and recency of one published environment."""

    environment_id: str
    bytes_used: int
    aliases: tuple[str, ...]
    toolchain: str | None
    last_used_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment_id": self.environment_id,
            "bytes_used": self.bytes_used,
            "aliases": list(self.aliases),
            "toolchain": self.toolchain,
            "last_used_at": self.last_used_at,
        }


@dataclass(frozen=True, slots=True)
class StoreStatus:
    home: str
    environments: int
    locks: int
    sources: int
    oci_blobs: int
    cas_artifacts: int
    executions: int
    aliases: int
    bytes_used: int
    bytes_free: int
    declaration_indexes: int = 0
    environments_bytes: int = 0
    sources_bytes: int = 0
    oci_blobs_bytes: int = 0
    cas_artifacts_bytes: int = 0
    declaration_indexes_bytes: int = 0
    project_packages: int = 0
    project_packages_bytes: int = 0
    toolchains_bytes: int = 0
    executions_bytes: int = 0
    scratch_workspaces: int = 0
    scratch_bytes: int = 0
    allocated_bytes: int = 0
    environment_usage: tuple[EnvironmentUsage, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "home": self.home,
            "environments": self.environments,
            "locks": self.locks,
            "sources": self.sources,
            "oci_blobs": self.oci_blobs,
            "cas_artifacts": self.cas_artifacts,
            "declaration_indexes": self.declaration_indexes,
            "executions": self.executions,
            "aliases": self.aliases,
            "bytes_used": self.bytes_used,
            "bytes_free": self.bytes_free,
            "environments_bytes": self.environments_bytes,
            "sources_bytes": self.sources_bytes,
            "oci_blobs_bytes": self.oci_blobs_bytes,
            "cas_artifacts_bytes": self.cas_artifacts_bytes,
            "declaration_indexes_bytes": self.declaration_indexes_bytes,
            "project_packages": self.project_packages,
            "project_packages_bytes": self.project_packages_bytes,
            "toolchains_bytes": self.toolchains_bytes,
            "executions_bytes": self.executions_bytes,
            "scratch_workspaces": self.scratch_workspaces,
            "scratch_bytes": self.scratch_bytes,
            "allocated_bytes": self.allocated_bytes,
            "environment_usage": [usage.to_dict() for usage in self.environment_usage],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> StoreStatus:
        fields = dict(value)
        fields["environment_usage"] = tuple(
            EnvironmentUsage(
                environment_id=str(item["environment_id"]),
                bytes_used=int(item["bytes_used"]),
                aliases=tuple(str(alias) for alias in item["aliases"]),
                toolchain=str(item["toolchain"]) if item.get("toolchain") is not None else None,
                last_used_at=(
                    str(item["last_used_at"]) if item.get("last_used_at") is not None else None
                ),
            )
            for item in fields.get("environment_usage", [])
        )
        return cls(**fields)


class EnvironmentStore:
    """Filesystem-backed, atomically published content-addressed store."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.sources = home / "sources" / "git"
        self.locks = home / "locks"
        self.environments = home / "environments"
        self.programs = home / "programs"
        self.names = home / "names"
        self.jobs = home / "jobs"
        self.executions = home / "executions"
        self.usage = home / "usage"
        self.leases = home / "leases"
        self.oci_blobs = home / "oci" / "blobs" / "sha256"
        self.cas_artifacts = home / "cas" / "artifacts" / "sha256"
        self.declaration_indexes = home / "declaration-indexes"
        self.declaration_index_objects = self.declaration_indexes / "objects" / "sha256"
        self.declaration_index_locks = self.declaration_indexes / "locks"
        self.lock_dir = home / ".locks"
        for path in (
            self.sources,
            self.locks,
            self.environments,
            self.programs,
            self.names,
            self.jobs,
            self.executions,
            self.usage,
            self.leases,
            self.oci_blobs,
            self.cas_artifacts,
            self.declaration_indexes,
            self.declaration_index_objects,
            self.declaration_index_locks,
            self.lock_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def declaration_index_record(self, lock_id: str) -> dict[str, Any] | None:
        """Return the retained shard manifest for an exact lock, if present."""
        if re.fullmatch(r"lock_[0-9a-f]{64}", lock_id) is None:
            raise EnvironmentError(f"invalid lock identity: {lock_id!r}")
        record_path = self.declaration_index_locks / f"{lock_id}.json"
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        if (
            not isinstance(record, dict)
            or record.get("schema") != "lean-runtime.declaration-index-retention/v2"
            or record.get("lock_id") != lock_id
            or not isinstance(record.get("shards"), list)
        ):
            return None
        return record

    def declaration_index_shards(
        self, lock_id: str
    ) -> tuple[tuple[DeclarationShard, Path | None], ...]:
        record = self.declaration_index_record(lock_id)
        if record is None:
            return ()
        result: list[tuple[DeclarationShard, Path | None]] = []
        for raw in record["shards"]:
            if not isinstance(raw, dict):
                return ()
            try:
                shard = DeclarationShard.from_dict(raw)
            except EnvironmentError:
                return ()
            name = shard.sqlite_digest.removeprefix("sha256:")
            path = self.declaration_index_objects / name
            result.append((shard, path if path.is_file() else None))
        return tuple(result)

    def publish_declaration_index_shards(
        self,
        lock_id: str,
        shards: tuple[DeclarationShard, ...],
        sources: dict[str, Path],
        *,
        manifest_digest: str,
        library: str,
    ) -> tuple[Path, ...]:
        """Atomically retain selected shard objects and their complete lock manifest."""
        if re.fullmatch(r"lock_[0-9a-f]{64}", lock_id) is None:
            raise EnvironmentError(f"invalid lock identity: {lock_id!r}")
        if not shards or len({item.shard_id for item in shards}) != len(shards):
            raise EnvironmentError("declaration index shard manifest is empty or duplicated")
        destinations: list[Path] = []
        with FileLock(self.lock_dir / f"declaration-index-{lock_id}.lock", timeout=1800):
            for shard_id, source in sources.items():
                shard = next((item for item in shards if item.shard_id == shard_id), None)
                if shard is None:
                    raise EnvironmentError("retained declaration shard is not in its manifest")
                name = shard.sqlite_digest.removeprefix("sha256:")
                if _OCI_BLOB.fullmatch(name) is None:
                    raise EnvironmentError("declaration shard has an invalid content digest")
                destination = self.declaration_index_objects / name
                if not destination.is_file():
                    source.replace(destination)
                else:
                    source.unlink(missing_ok=True)
                destinations.append(destination)
            write_json_atomic(
                self.declaration_index_locks / f"{lock_id}.json",
                {
                    "schema": "lean-runtime.declaration-index-retention/v2",
                    "lock_id": lock_id,
                    "manifest_digest": manifest_digest,
                    "library": library,
                    "shards": [item.to_dict() for item in shards],
                },
            )
        return tuple(destinations)

    def lock_path(self, lock_id: str) -> Path:
        return self.locks / lock_id / "environment.lock.json"

    def _workspace_lock_path(self, path: Path) -> Path:
        identity = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
        return self.lock_dir / f"workspace-{identity}.lock"

    def lease_workspace(self, path: Path, kind: str) -> WorkspaceLease:
        return WorkspaceLease(self, path, kind)

    def _scratch_paths(self) -> tuple[Path, ...]:
        resolution = self.home / "resolution"
        return tuple(
            [path for path in self.jobs.glob("execution_*") if path.is_dir()]
            + [path for path in self.jobs.glob(".trash-execution_*") if path.is_dir()]
            + [path for path in resolution.glob("resolve-*") if path.is_dir()]
            + [path for path in resolution.glob(".trash-resolve-*") if path.is_dir()]
        )

    def _workspace_active(self, path: Path) -> bool:
        try:
            with FileLock(self._workspace_lock_path(path), timeout=0):
                return False
        except EnvironmentError:
            return True

    def publish_lock(self, lock: EnvironmentLock) -> Path:
        destination = self.lock_path(lock.lock_id)
        with FileLock(self.lock_dir / f"{lock.lock_id}.lock"):
            if destination.is_file():
                EnvironmentLock.load(destination)
                return destination
            destination.parent.mkdir(parents=True, exist_ok=True)
            lock.write(destination)
        return destination

    def load_lock(self, lock_id: str) -> EnvironmentLock:
        path = self.lock_path(lock_id)
        if not path.is_file():
            raise EnvironmentError(f"environment lock is not present: {lock_id}")
        return EnvironmentLock.load(path)

    def source_path(self, source_id: str) -> Path:
        return self.sources / source_id / "source"

    def validate_source(
        self,
        source_id: str,
        *,
        url: str,
        revision: str,
        tree_hash: str,
    ) -> Path:
        source = self.source_path(source_id)
        marker = source / ".lean-runtime-source.json"
        if not source.is_dir() or not marker.is_file():
            raise EnvironmentError(f"immutable source snapshot is incomplete: {source_id}")
        metadata = json.loads(marker.read_text(encoding="utf-8"))
        expected = {
            "source_id": source_id,
            "url": url,
            "revision": revision,
            "tree_hash": tree_hash,
        }
        if any(metadata.get(key) != value for key, value in expected.items()):
            raise EnvironmentError(f"immutable source metadata mismatch: {source_id}")
        content_hash = metadata.get("content_hash")
        if isinstance(content_hash, str) and source_snapshot_digest(source) != content_hash:
            raise EnvironmentError(f"immutable source content was modified: {source_id}")
        commands = (("rev-parse", "HEAD"), ("rev-parse", "HEAD^{tree}"))
        observed = []
        for arguments in commands:
            process = subprocess.run(
                git_command("-C", str(source), *arguments),
                text=True,
                capture_output=True,
                check=False,
            )
            if process.returncode:
                raise EnvironmentError(f"immutable source Git metadata is invalid: {source_id}")
            observed.append(process.stdout.strip().lower())
        if observed != [revision.lower(), tree_hash.lower()]:
            raise EnvironmentError(f"immutable source content mismatch: {source_id}")
        return source

    def publish_source(self, checkout: Path, source_id: str, metadata: dict[str, Any]) -> Path:
        destination = self.source_path(source_id)
        with FileLock(self.lock_dir / f"{source_id}.lock"):
            if destination.is_dir():
                self.validate_source(
                    source_id,
                    url=str(metadata["url"]),
                    revision=str(metadata["revision"]),
                    tree_hash=str(metadata["tree_hash"]),
                )
                return destination
            parent = destination.parent
            parent.mkdir(parents=True, exist_ok=True)
            # Keep this short: Git rejects shallow-clone metadata paths near MAX_PATH
            # on Windows ("'$GIT_DIR' too big"), and source ids are already 71 chars.
            stage = parent / f".staging-{uuid.uuid4().hex[:12]}"
            try:
                command = git_command(
                    "clone",
                    "--quiet",
                    "--no-local",
                    "--depth",
                    "1",
                    "--no-tags",
                    checkout.resolve().as_uri(),
                    str(stage),
                )
                process = subprocess.run(
                    command,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if process.returncode:
                    raise EnvironmentError(
                        f"could not create compact source snapshot {source_id}: "
                        + process.stdout
                        + process.stderr
                    )
                remote = subprocess.run(
                    git_command(
                        "-C",
                        str(stage),
                        "remote",
                        "set-url",
                        "origin",
                        str(metadata["url"]),
                    ),
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if remote.returncode:
                    raise EnvironmentError(
                        f"could not normalize source remote {source_id}: "
                        + remote.stdout
                        + remote.stderr
                    )
                snapshot_metadata = {**metadata, "content_hash": source_snapshot_digest(stage)}
                write_json_atomic(stage / ".lean-runtime-source.json", snapshot_metadata)
                stage.replace(destination)
            finally:
                if stage.exists():
                    # Failed Git checkouts can leave locked pack files on
                    # Windows. Cleanup must not mask the actionable Git error.
                    with suppress(OSError):
                        remove_tree(stage)
        return destination

    def environment_path(self, environment_id: str) -> Path:
        return self.environments / environment_id

    def validate_environment_id(self, environment_id: str) -> str:
        if _ENVIRONMENT_ID.fullmatch(environment_id) is None:
            raise EnvironmentError(f"invalid environment identity: {environment_id!r}")
        return environment_id

    def touch_environment(self, environment_id: str) -> None:
        self.validate_environment_id(environment_id)
        write_json_atomic(
            self.usage / f"{environment_id}.json",
            {"environment_id": environment_id, "last_used_at": time.time()},
        )

    @contextmanager
    def execution_lease(self, environment_id: str) -> Iterator[None]:
        """Prevent GC during a clone without serializing concurrent clones."""
        self.validate_environment_id(environment_id)
        lease_directory = self.leases / environment_id
        lease = lease_directory / f"{os.getpid()}-{uuid.uuid4().hex}.json"
        with FileLock(self.lock_dir / f"{environment_id}.lock"):
            if not self.environment_path(environment_id).is_dir():
                raise EnvironmentError(
                    f"environment disappeared before execution: {environment_id}"
                )
            lease_directory.mkdir(parents=True, exist_ok=True)
            self.touch_environment(environment_id)
            write_json_atomic(
                lease,
                {"environment_id": environment_id, "pid": os.getpid(), "created_at": time.time()},
            )
        try:
            yield None
        finally:
            lease.unlink(missing_ok=True)
            with suppress(OSError):
                lease_directory.rmdir()

    def has_execution_leases(self, environment_id: str) -> bool:
        return any((self.leases / environment_id).glob("*.json"))

    @contextmanager
    def oci_blob_lease(self, digests: Iterable[str]) -> Iterator[None]:
        """Prevent OCI blob collection while a pull is downloading or importing blobs."""
        names = sorted(
            {
                digest.removeprefix("sha256:")
                for digest in digests
                if _OCI_BLOB.fullmatch(digest.removeprefix("sha256:")) is not None
            }
        )
        lease_id = f"{os.getpid()}-{uuid.uuid4().hex}.json"
        created: list[Path] = []
        for name in names:
            directory = self.leases / "oci" / name
            directory.mkdir(parents=True, exist_ok=True)
            lease = directory / lease_id
            write_json_atomic(
                lease,
                {"digest": f"sha256:{name}", "pid": os.getpid(), "created_at": time.time()},
            )
            created.append(lease)
        try:
            yield None
        finally:
            for lease in created:
                lease.unlink(missing_ok=True)
                with suppress(OSError):
                    lease.parent.rmdir()

    def has_oci_blob_leases(self, digest: str) -> bool:
        name = digest.removeprefix("sha256:")
        return any((self.leases / "oci" / name).glob("*.json"))

    @contextmanager
    def cas_artifact_lease(self, digests: Iterable[str]) -> Iterator[None]:
        """Prevent sparse CAS collection while artifacts are unpacked and projected."""
        names = sorted(
            {
                digest.removeprefix("sha256:")
                for digest in digests
                if _OCI_BLOB.fullmatch(digest.removeprefix("sha256:")) is not None
            }
        )
        lease_id = f"{os.getpid()}-{uuid.uuid4().hex}.json"
        created: list[Path] = []
        for digest in names:
            directory = self.leases / "cas" / digest
            directory.mkdir(parents=True, exist_ok=True)
            lease = directory / lease_id
            write_json_atomic(
                lease,
                {
                    "digest": f"sha256:{digest}",
                    "pid": os.getpid(),
                    "created_at": time.time(),
                },
            )
            created.append(lease)
        try:
            yield None
        finally:
            for lease in created:
                lease.unlink(missing_ok=True)
                with suppress(OSError):
                    lease.parent.rmdir()

    def has_cas_artifact_leases(self, digest: str) -> bool:
        name = digest.removeprefix("sha256:")
        return any((self.leases / "cas" / name).glob("*.json"))

    def referenced_oci_blobs(self) -> set[str]:
        referenced: set[str] = set()
        for path in self.environments.glob("env_*/metadata.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            origin = value.get("origin") if isinstance(value, dict) else None
            digests = origin.get("blob_digests", ()) if isinstance(origin, dict) else ()
            if not isinstance(digests, list):
                continue
            referenced.update(
                digest.removeprefix("sha256:")
                for digest in digests
                if isinstance(digest, str)
                and _OCI_BLOB.fullmatch(digest.removeprefix("sha256:")) is not None
            )
        return referenced

    def validate_alias(self, name: str) -> str:
        if _ALIAS.fullmatch(name) is None:
            raise EnvironmentError(f"invalid environment name: {name!r}")
        return name

    def set_alias(self, name: str, environment_id: str) -> None:
        self.validate_alias(name)
        self.validate_environment_id(environment_id)
        record = {
            "schema": ALIAS_SCHEMA,
            "name": name,
            "environment_id": environment_id,
        }
        with FileLock(self.lock_dir / f"name-{name}.lock"):
            write_json_atomic(self.names / f"{name}.json", record)

    def resolve_identifier(self, identifier: str) -> str:
        if _ENVIRONMENT_ID.fullmatch(identifier) is not None:
            direct = self.environment_path(identifier)
            if direct.is_dir():
                return identifier
        alias_path = self.names / f"{self.validate_alias(identifier)}.json"
        if not alias_path.is_file():
            raise EnvironmentError(f"unknown environment: {identifier}")
        environment_id = self._read_alias(alias_path, expected_name=identifier)
        if not self.environment_path(environment_id).is_dir():
            raise EnvironmentError(f"environment alias is dangling: {identifier}")
        return environment_id

    def _read_alias(self, path: Path, *, expected_name: str | None = None) -> str:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EnvironmentError(f"invalid environment alias record: {path.name}") from exc
        if not isinstance(value, dict) or value.get("schema") != ALIAS_SCHEMA:
            raise EnvironmentError(f"invalid environment alias record: {path.name}")
        name = value.get("name")
        environment_id = value.get("environment_id")
        if not isinstance(name, str) or name != path.stem or name != (expected_name or name):
            raise EnvironmentError(f"environment alias name mismatch: {path.name}")
        self.validate_alias(name)
        if not isinstance(environment_id, str):
            raise EnvironmentError(f"environment alias has no identity: {name}")
        return self.validate_environment_id(environment_id)

    def aliases(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for path in sorted(self.names.glob("*.json")):
            result[path.stem] = self._read_alias(path)
        return result

    def _storage_fingerprint(self) -> list[list[object]]:
        roots = (
            self.environments,
            self.sources,
            self.oci_blobs,
            self.cas_artifacts,
            self.executions,
            self.home / "project-packages",
            self.home / "project-sources",
            self.home / "project-workspaces",
            self.home / "elan",
            self.home / "toolchains",
            self.jobs,
            self.home / "resolution",
        )
        fingerprint: list[list[object]] = []
        for root in roots:
            with suppress(OSError):
                children = tuple(root.iterdir())
                fingerprint.append(
                    [
                        root.name,
                        root.stat().st_mtime_ns,
                        len(children),
                        sum(child.stat().st_mtime_ns for child in children),
                    ]
                )
        return fingerprint

    def status(self, *, verify: bool = False) -> StoreStatus:
        ledger = self.home / "storage-ledger.json"
        fingerprint = self._storage_fingerprint()
        if not verify:
            with suppress(OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                value = json.loads(ledger.read_text(encoding="utf-8"))
                if value.get("accounting") == 2 and value["fingerprint"] == fingerprint:
                    cached = StoreStatus.from_dict(value["status"])
                    return replace(cached, bytes_free=shutil.disk_usage(self.home).free)
        aliases = self.aliases()
        allocated_seen: set[tuple[int, int]] = set()
        allocated_bytes = 0
        names_by_environment: dict[str, list[str]] = {}
        for name, environment_id in aliases.items():
            names_by_environment.setdefault(environment_id, []).append(name)
        usage: list[EnvironmentUsage] = []
        environment_paths = sorted(self.environments.glob("env_*"))
        accounting = CountedProgress(
            current().emit,
            "storage.accounting",
            "Measuring storage",
            len(environment_paths) + 10,
            phase="storage",
        )
        accounting.start()
        for path in environment_paths:
            if not path.is_dir():
                accounting.advance(path.name)
                continue
            toolchain: str | None = None
            with suppress(OSError, json.JSONDecodeError, TypeError):
                record = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
                value = record.get("toolchain")
                toolchain = value if isinstance(value, str) else None
            environment_bytes, environment_allocated = _tree_metrics(path, allocated_seen)
            allocated_bytes += environment_allocated
            usage.append(
                EnvironmentUsage(
                    environment_id=path.name,
                    bytes_used=environment_bytes,
                    aliases=tuple(sorted(names_by_environment.get(path.name, ()))),
                    toolchain=toolchain,
                    last_used_at=self._last_used_at(path),
                )
            )
            accounting.advance(path.name)
        usage.sort(key=lambda item: item.bytes_used, reverse=True)
        project_packages = self.home / "project-packages"
        project_sources = self.home / "project-sources"
        project_workspaces = self.home / "project-workspaces"
        environments_bytes = sum(item.bytes_used for item in usage)

        def measure(*roots: Path) -> int:
            nonlocal allocated_bytes
            materialized = 0
            for root in roots:
                root_bytes, root_allocated = _tree_metrics(root, allocated_seen)
                materialized += root_bytes
                allocated_bytes += root_allocated
            return materialized

        sources_bytes = measure(self.sources)
        accounting.advance("sources")
        oci_blobs_bytes = measure(self.oci_blobs)
        accounting.advance("OCI blobs")
        cas_artifacts_bytes = measure(self.cas_artifacts)
        accounting.advance("capsule artifacts")
        declaration_indexes_bytes = measure(self.declaration_indexes)
        accounting.advance("declaration indexes")
        project_packages_bytes = measure(project_packages, project_sources, project_workspaces)
        accounting.advance("shared projects")
        toolchains_bytes = measure(self.home / "elan", self.home / "toolchains")
        accounting.advance("toolchains")
        executions_bytes = measure(self.executions)
        accounting.advance("executions")
        scratch_paths = self._scratch_paths()
        scratch_bytes = measure(*scratch_paths)
        accounting.advance("scratch workspaces")
        locks_bytes = measure(self.locks)
        accounting.advance("locks")
        names_bytes = measure(self.names)
        accounting.advance("names")
        bytes_used = sum(
            (
                environments_bytes,
                sources_bytes,
                oci_blobs_bytes,
                cas_artifacts_bytes,
                declaration_indexes_bytes,
                project_packages_bytes,
                toolchains_bytes,
                executions_bytes,
                scratch_bytes,
                locks_bytes,
                names_bytes,
            )
        )
        status = StoreStatus(
            home=str(self.home),
            environments=len(usage),
            locks=sum(1 for path in self.locks.glob("lock_*") if path.is_dir()),
            sources=sum(1 for path in self.sources.glob("source_*") if path.is_dir()),
            oci_blobs=sum(1 for path in self.oci_blobs.glob("[0-9a-f]" * 64) if path.is_file()),
            cas_artifacts=sum(
                1 for path in self.cas_artifacts.glob("[0-9a-f]" * 64) if path.is_file()
            ),
            declaration_indexes=sum(
                1 for path in self.declaration_index_objects.glob("[0-9a-f]" * 64) if path.is_file()
            ),
            executions=sum(1 for path in self.executions.glob("execution_*.json")),
            aliases=len(aliases),
            bytes_used=bytes_used,
            bytes_free=shutil.disk_usage(self.home).free,
            environments_bytes=environments_bytes,
            sources_bytes=sources_bytes,
            oci_blobs_bytes=oci_blobs_bytes,
            cas_artifacts_bytes=cas_artifacts_bytes,
            declaration_indexes_bytes=declaration_indexes_bytes,
            project_packages=sum(1 for _path in package_directories(project_packages)),
            project_packages_bytes=project_packages_bytes,
            toolchains_bytes=toolchains_bytes,
            executions_bytes=executions_bytes,
            scratch_workspaces=len(scratch_paths),
            scratch_bytes=scratch_bytes,
            allocated_bytes=allocated_bytes,
            environment_usage=tuple(usage),
        )
        write_json_atomic(
            ledger,
            {"accounting": 2, "fingerprint": fingerprint, "status": status.to_dict()},
        )
        return status

    def _last_used_at(self, environment_path: Path) -> str | None:
        marker = self.usage / f"{environment_path.name}.json"
        try:
            timestamp = (marker if marker.exists() else environment_path).stat().st_mtime
        except OSError:
            return None
        return (
            datetime.fromtimestamp(timestamp, tz=timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )

    def clean(
        self,
        *,
        dry_run: bool = True,
        minimum_age_seconds: float = 2_592_000,
        keep_last: int = 0,
    ) -> CleanupReport:
        """Remove old environments not reachable through a name.

        Locks, immutable sources, and OCI blobs are retained in the current store schema.
        """
        referenced = set(self.aliases().values())
        now = time.time()
        candidates: list[str] = []
        retained: list[str] = []
        removed: list[str] = []
        candidate_bytes = 0
        reclaimed_bytes = 0
        if keep_last < 0:
            raise ValueError("clean keep_last must be nonnegative")
        unaliased = [
            path
            for path in self.environments.glob("env_*")
            if path.is_dir() and path.name not in referenced
        ]
        unaliased.sort(
            key=lambda path: (
                (self.usage / f"{path.name}.json").stat().st_mtime
                if (self.usage / f"{path.name}.json").exists()
                else path.stat().st_mtime
            ),
            reverse=True,
        )
        protected = {path.name for path in unaliased[:keep_last]}
        paths = sorted(self.environments.glob("env_*"))
        progress = CountedProgress(
            current().emit, "storage.cleanup", "Inspecting environments", len(paths), phase="clean"
        )
        progress.start()
        with FileLock(self.lock_dir / "gc.lock"):
            for path in paths:
                if _ENVIRONMENT_ID.fullmatch(path.name) is None:
                    retained.append(path.name)
                    progress.advance(path.name)
                    continue
                usage = self.usage / f"{path.name}.json"
                age = now - (usage.stat().st_mtime if usage.exists() else path.stat().st_mtime)
                if (
                    path.name in referenced
                    or path.name in protected
                    or age < minimum_age_seconds
                    or self.has_execution_leases(path.name)
                ):
                    retained.append(path.name)
                    progress.advance(path.name)
                    continue
                candidates.append(path.name)
                size = _tree_bytes(path)
                candidate_bytes += size
                if not dry_run:
                    with FileLock(self.lock_dir / f"{path.name}.lock"):
                        referenced = set(self.aliases().values())
                        age = now - (
                            usage.stat().st_mtime if usage.exists() else path.stat().st_mtime
                        )
                        if (
                            path.name in referenced
                            or age < minimum_age_seconds
                            or self.has_execution_leases(path.name)
                        ):
                            retained.append(path.name)
                            progress.advance(path.name)
                            continue
                        remove_tree(path)
                        usage.unlink(missing_ok=True)
                        removed.append(path.name)
                        reclaimed_bytes += size
                progress.advance(path.name)
        return CleanupReport(
            tuple(candidates),
            tuple(removed),
            tuple(retained),
            dry_run,
            candidate_bytes=candidate_bytes,
            reclaimed_bytes=reclaimed_bytes,
        )

    def clean_scratch(
        self,
        *,
        dry_run: bool = True,
        minimum_age_seconds: float = 3600,
        include_legacy: bool = True,
        legacy_minimum_age_seconds: float | None = None,
    ) -> CleanupReport:
        """Remove disposable workspaces whose process ownership lease is no longer held."""
        now = time.time()
        candidates: list[str] = []
        retained: list[str] = []
        removed: list[str] = []
        candidate_bytes = 0
        reclaimed_bytes = 0
        paths = self._scratch_paths()
        progress = CountedProgress(
            current().emit,
            "storage.scratch_cleanup",
            "Inspecting scratch workspaces",
            len(paths),
            phase="clean",
        )
        progress.start()
        with FileLock(self.lock_dir / "scratch-gc.lock"):
            for path in paths:
                try:
                    age = max(0.0, now - path.stat().st_mtime)
                except OSError:
                    progress.advance(path.name)
                    continue
                label = f"{path.parent.name}/{path.name}"
                marker = path / ".lean-runtime-workspace.json"
                required_age = (
                    legacy_minimum_age_seconds
                    if legacy_minimum_age_seconds is not None and not marker.is_file()
                    else minimum_age_seconds
                )
                if (
                    age < required_age
                    or (not include_legacy and not marker.is_file())
                    or self._workspace_active(path)
                ):
                    retained.append(label)
                    progress.advance(label)
                    continue
                size = _tree_bytes(path)
                candidates.append(label)
                candidate_bytes += size
                if dry_run:
                    progress.advance(label)
                    continue
                if path.name.startswith(".trash-"):
                    remove_tree(path)
                    removed.append(label)
                    reclaimed_bytes += size
                    progress.advance(label)
                    continue
                tombstone = path.with_name(f".trash-{path.name}-{uuid.uuid4().hex}")
                try:
                    path.replace(tombstone)
                except FileNotFoundError:
                    progress.advance(label)
                    continue
                remove_tree(tombstone)
                removed.append(label)
                reclaimed_bytes += size
                progress.advance(label)
        return CleanupReport(
            tuple(candidates),
            tuple(removed),
            tuple(retained),
            dry_run,
            candidate_bytes,
            reclaimed_bytes,
        )

    def clean_legacy_project_artifacts(
        self,
        *,
        dry_run: bool = True,
        minimum_age_seconds: float = 0,
    ) -> CleanupReport:
        """Reclaim package trees whose compiled artifacts use the obsolete /1 key."""
        root = self.home / "project-packages"
        now = time.time()
        candidates: list[str] = []
        removed: list[str] = []
        retained: list[str] = []
        candidate_bytes = 0
        reclaimed_bytes = 0
        packages = tuple(package_directories(root))
        progress = CountedProgress(
            current().emit,
            "storage.project_cleanup",
            "Inspecting legacy project artifacts",
            len(packages),
            phase="clean",
        )
        progress.start()
        with FileLock(self.lock_dir / "project-artifact-gc.lock"):
            for package in packages:
                marker = package / ".lean-runtime-package.json"
                try:
                    record = json.loads(marker.read_text(encoding="utf-8"))
                    artifact = record.get("artifact_key")
                    legacy = (
                        isinstance(artifact, dict)
                        and artifact.get("schema") == "lean-runtime-package-artifact-key/1"
                    )
                    age = max(0.0, now - package.stat().st_mtime)
                except (OSError, AttributeError, json.JSONDecodeError):
                    retained.append(package.name)
                    progress.advance(package.name)
                    continue
                if not legacy or age < minimum_age_seconds:
                    retained.append(package.name)
                    progress.advance(package.name)
                    continue
                size = _tree_bytes(package)
                candidates.append(package.name)
                candidate_bytes += size
                if dry_run:
                    progress.advance(package.name)
                    continue
                try:
                    with (
                        FileLock(self.lock_dir / f"{package.name}-build.lock", timeout=0),
                        FileLock(self.lock_dir / f"{package.name}.lock", timeout=0),
                    ):
                        if not package.is_dir():
                            progress.advance(package.name)
                            continue
                        remove_tree(package)
                except EnvironmentError:
                    retained.append(package.name)
                    progress.advance(package.name)
                    continue
                removed.append(package.name)
                reclaimed_bytes += size
                progress.advance(package.name)
        return CleanupReport(
            tuple(candidates),
            tuple(removed),
            tuple(retained),
            dry_run,
            candidate_bytes,
            reclaimed_bytes,
        )

    def clean_downloads(
        self, *, dry_run: bool = True, minimum_age_seconds: float = 2_592_000
    ) -> DownloadCleanupReport:
        """Remove old OCI blobs not referenced by an imported environment or active pull."""
        candidates: list[str] = []
        removed: list[str] = []
        retained: list[str] = []
        reclaimed_bytes = 0
        candidate_bytes = 0
        now = time.time()
        blob_paths = sorted(self.oci_blobs.iterdir())
        artifact_paths = sorted(self.cas_artifacts.iterdir())
        progress = CountedProgress(
            current().emit,
            "storage.download_cleanup",
            "Inspecting downloaded artifacts",
            len(blob_paths) + len(artifact_paths),
            phase="clean",
        )
        progress.start()
        with FileLock(self.lock_dir / "oci-gc.lock"):
            referenced = self.referenced_oci_blobs()
            for path in blob_paths:
                if not path.is_file() or _OCI_BLOB.fullmatch(path.name) is None:
                    retained.append(path.name)
                    progress.advance(path.name)
                    continue
                age = max(0.0, now - path.stat().st_mtime)
                if (
                    path.name in referenced
                    or age < minimum_age_seconds
                    or self.has_oci_blob_leases(path.name)
                ):
                    retained.append(path.name)
                    progress.advance(path.name)
                    continue
                candidates.append(path.name)
                with suppress(OSError):
                    candidate_bytes += path.stat().st_size
                if dry_run:
                    progress.advance(path.name)
                    continue
                with FileLock(self.lock_dir / f"oci-{path.name}.lock"):
                    referenced = self.referenced_oci_blobs()
                    if (
                        not path.is_file()
                        or path.name in referenced
                        or max(0.0, now - path.stat().st_mtime) < minimum_age_seconds
                        or self.has_oci_blob_leases(path.name)
                    ):
                        retained.append(path.name)
                        progress.advance(path.name)
                        continue
                    size = path.stat().st_size
                    path.unlink()
                    reclaimed_bytes += size
                    removed.append(path.name)
                progress.advance(path.name)
            for path in artifact_paths:
                label = f"cas:{path.name}"
                if not path.is_file() or _OCI_BLOB.fullmatch(path.name) is None:
                    retained.append(label)
                    progress.advance(label)
                    continue
                age = max(0.0, now - path.stat().st_mtime)
                if age < minimum_age_seconds or self.has_cas_artifact_leases(path.name):
                    retained.append(label)
                    progress.advance(label)
                    continue
                candidates.append(label)
                with suppress(OSError):
                    candidate_bytes += path.stat().st_size
                if dry_run:
                    progress.advance(label)
                    continue
                with FileLock(self.lock_dir / f"cas-{path.name}.lock"):
                    if (
                        not path.is_file()
                        or max(0.0, now - path.stat().st_mtime) < minimum_age_seconds
                        or self.has_cas_artifact_leases(path.name)
                    ):
                        retained.append(label)
                        progress.advance(label)
                        continue
                    size = path.stat().st_size
                    path.unlink()
                    reclaimed_bytes += size
                    removed.append(label)
                progress.advance(label)
        return DownloadCleanupReport(
            tuple(candidates),
            tuple(removed),
            tuple(retained),
            reclaimed_bytes,
            dry_run,
            candidate_bytes=candidate_bytes,
        )
