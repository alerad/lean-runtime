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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import EnvironmentError
from .lockfiles import EnvironmentLock
from .locking import FileLock
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
    digest = hashlib.sha256()
    for directory, directories, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        symlink_directories = [name for name in directories if (current / name).is_symlink()]
        directories[:] = sorted(
            name
            for name in directories
            if name not in {".git", ".lake"} and name not in symlink_directories
        )
        for name in sorted([*filenames, *symlink_directories]):
            path = current / name
            relative = path.relative_to(root)
            if relative.as_posix() == ".lean-runtime-source.json":
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
    destination.parent.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Darwin":
        result = subprocess.run(
            ["cp", "-cR", str(source), str(destination)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            return
    elif platform.system() == "Linux":
        result = subprocess.run(
            ["cp", "--reflink=auto", "-a", str(source), str(destination)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            return
    shutil.copytree(source, destination, symlinks=True)


@dataclass(frozen=True, slots=True)
class CleanupReport:
    candidates: tuple[str, ...]
    removed: tuple[str, ...]
    retained: tuple[str, ...]
    dry_run: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": list(self.candidates),
            "removed": list(self.removed),
            "retained": list(self.retained),
            "dry_run": self.dry_run,
        }


@dataclass(frozen=True, slots=True)
class DownloadCleanupReport:
    candidates: tuple[str, ...]
    removed: tuple[str, ...]
    retained: tuple[str, ...]
    reclaimed_bytes: int
    dry_run: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": list(self.candidates),
            "removed": list(self.removed),
            "retained": list(self.retained),
            "reclaimed_bytes": self.reclaimed_bytes,
            "dry_run": self.dry_run,
        }


@dataclass(frozen=True, slots=True)
class StoreStatus:
    home: str
    environments: int
    locks: int
    sources: int
    oci_blobs: int
    executions: int
    aliases: int
    bytes_used: int
    bytes_free: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "home": self.home,
            "environments": self.environments,
            "locks": self.locks,
            "sources": self.sources,
            "oci_blobs": self.oci_blobs,
            "executions": self.executions,
            "aliases": self.aliases,
            "bytes_used": self.bytes_used,
            "bytes_free": self.bytes_free,
        }


class EnvironmentStore:
    """Filesystem-backed, atomically published content-addressed store."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.sources = home / "sources" / "git"
        self.locks = home / "locks"
        self.environments = home / "environments"
        self.names = home / "names"
        self.jobs = home / "jobs"
        self.executions = home / "executions"
        self.usage = home / "usage"
        self.leases = home / "leases"
        self.oci_blobs = home / "oci" / "blobs" / "sha256"
        self.lock_dir = home / ".locks"
        for path in (
            self.sources,
            self.locks,
            self.environments,
            self.names,
            self.jobs,
            self.executions,
            self.usage,
            self.leases,
            self.oci_blobs,
            self.lock_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def lock_path(self, lock_id: str) -> Path:
        return self.locks / lock_id / "environment.lock.json"

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
                ["git", "-C", str(source), *arguments],
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
            stage = parent / f".staging-{os.getpid()}-{uuid.uuid4().hex}"
            try:
                command = [
                    "git",
                    "clone",
                    "--quiet",
                    "--no-local",
                    "--depth",
                    "1",
                    "--no-tags",
                    checkout.resolve().as_uri(),
                    str(stage),
                ]
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
                    [
                        "git",
                        "-C",
                        str(stage),
                        "remote",
                        "set-url",
                        "origin",
                        str(metadata["url"]),
                    ],
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
                    shutil.rmtree(stage)
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

    def status(self) -> StoreStatus:
        bytes_used = 0
        for path in self.home.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    bytes_used += path.stat().st_size
            except OSError:
                continue
        return StoreStatus(
            home=str(self.home),
            environments=sum(1 for path in self.environments.glob("env_*") if path.is_dir()),
            locks=sum(1 for path in self.locks.glob("lock_*") if path.is_dir()),
            sources=sum(1 for path in self.sources.glob("source_*") if path.is_dir()),
            oci_blobs=sum(1 for path in self.oci_blobs.glob("[0-9a-f]" * 64) if path.is_file()),
            executions=sum(1 for path in self.executions.glob("execution_*.json")),
            aliases=len(self.aliases()),
            bytes_used=bytes_used,
            bytes_free=shutil.disk_usage(self.home).free,
        )

    def clean(
        self, *, dry_run: bool = True, minimum_age_seconds: float = 2_592_000
    ) -> CleanupReport:
        """Remove old environments not reachable through a name.

        Locks, immutable sources, and OCI blobs are retained in the current store schema.
        """
        referenced = set(self.aliases().values())
        now = time.time()
        candidates: list[str] = []
        retained: list[str] = []
        removed: list[str] = []
        with FileLock(self.lock_dir / "gc.lock"):
            for path in sorted(self.environments.glob("env_*")):
                if _ENVIRONMENT_ID.fullmatch(path.name) is None:
                    retained.append(path.name)
                    continue
                usage = self.usage / f"{path.name}.json"
                age = now - (usage.stat().st_mtime if usage.exists() else path.stat().st_mtime)
                if (
                    path.name in referenced
                    or age < minimum_age_seconds
                    or self.has_execution_leases(path.name)
                ):
                    retained.append(path.name)
                    continue
                candidates.append(path.name)
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
                            continue
                        shutil.rmtree(path)
                        usage.unlink(missing_ok=True)
                        removed.append(path.name)
        return CleanupReport(tuple(candidates), tuple(removed), tuple(retained), dry_run)

    def clean_downloads(
        self, *, dry_run: bool = True, minimum_age_seconds: float = 2_592_000
    ) -> DownloadCleanupReport:
        """Remove old OCI blobs not referenced by an imported environment or active pull."""
        candidates: list[str] = []
        removed: list[str] = []
        retained: list[str] = []
        reclaimed_bytes = 0
        now = time.time()
        with FileLock(self.lock_dir / "oci-gc.lock"):
            referenced = self.referenced_oci_blobs()
            for path in sorted(self.oci_blobs.iterdir()):
                if not path.is_file() or _OCI_BLOB.fullmatch(path.name) is None:
                    retained.append(path.name)
                    continue
                age = now - path.stat().st_mtime
                if (
                    path.name in referenced
                    or age < minimum_age_seconds
                    or self.has_oci_blob_leases(path.name)
                ):
                    retained.append(path.name)
                    continue
                candidates.append(path.name)
                if dry_run:
                    continue
                with FileLock(self.lock_dir / f"oci-{path.name}.lock"):
                    referenced = self.referenced_oci_blobs()
                    if (
                        not path.is_file()
                        or path.name in referenced
                        or now - path.stat().st_mtime < minimum_age_seconds
                        or self.has_oci_blob_leases(path.name)
                    ):
                        retained.append(path.name)
                        continue
                    size = path.stat().st_size
                    path.unlink()
                    reclaimed_bytes += size
                    removed.append(path.name)
        return DownloadCleanupReport(
            tuple(candidates), tuple(removed), tuple(retained), reclaimed_bytes, dry_run
        )
