"""Content-addressed storage for locks, sources, and environments."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import EnvironmentError
from .lockfiles import EnvironmentLock
from .locking import FileLock
from .serialization import sha256_id, write_json_atomic

STORE_SCHEMA = "lean-runtime-store/1"
_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def platform_record() -> dict[str, str]:
    return {
        "system": platform.system().lower(),
        "machine": platform.machine().lower(),
        "python_platform": platform.platform(),
    }


def environment_identity(lock: EnvironmentLock, build_profile: str = "release") -> str:
    return sha256_id(
        "env",
        {
            "schema": STORE_SCHEMA,
            "lock_id": lock.lock_id,
            "platform": platform_record(),
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
class GarbageCollectionReport:
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
        self.lock_dir = home / ".locks"
        for path in (
            self.sources,
            self.locks,
            self.environments,
            self.names,
            self.jobs,
            self.executions,
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
                shutil.copytree(
                    checkout,
                    stage,
                    symlinks=True,
                    ignore=shutil.ignore_patterns(".lake"),
                )
                write_json_atomic(stage / ".lean-runtime-source.json", metadata)
                stage.replace(destination)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        return destination

    def environment_path(self, environment_id: str) -> Path:
        return self.environments / environment_id

    def validate_alias(self, name: str) -> str:
        if _ALIAS.fullmatch(name) is None:
            raise EnvironmentError(f"invalid environment name: {name!r}")
        return name

    def set_alias(self, name: str, environment_id: str) -> None:
        self.validate_alias(name)
        record = {
            "schema": "lean-runtime-environment-alias/1",
            "name": name,
            "environment_id": environment_id,
        }
        with FileLock(self.lock_dir / f"name-{name}.lock"):
            write_json_atomic(self.names / f"{name}.json", record)

    def resolve_identifier(self, identifier: str) -> str:
        direct = self.environment_path(identifier)
        if direct.is_dir():
            return identifier
        alias_path = self.names / f"{self.validate_alias(identifier)}.json"
        if not alias_path.is_file():
            raise EnvironmentError(f"unknown environment: {identifier}")
        value = json.loads(alias_path.read_text(encoding="utf-8"))
        environment_id = value.get("environment_id")
        if (
            not isinstance(environment_id, str)
            or not self.environment_path(environment_id).is_dir()
        ):
            raise EnvironmentError(f"environment alias is dangling: {identifier}")
        return environment_id

    def aliases(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for path in sorted(self.names.glob("*.json")):
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value.get("name"), str) and isinstance(value.get("environment_id"), str):
                result[value["name"]] = value["environment_id"]
        return result

    def gc(
        self, *, dry_run: bool = True, minimum_age_seconds: float = 2_592_000
    ) -> GarbageCollectionReport:
        """Remove old environments not reachable through a name.

        Locks and immutable sources are retained in the first store schema.
        """
        referenced = set(self.aliases().values())
        now = time.time()
        candidates: list[str] = []
        retained: list[str] = []
        removed: list[str] = []
        with FileLock(self.lock_dir / "gc.lock"):
            for path in sorted(self.environments.glob("env_*")):
                age = now - path.stat().st_mtime
                if path.name in referenced or age < minimum_age_seconds:
                    retained.append(path.name)
                    continue
                candidates.append(path.name)
                if not dry_run:
                    shutil.rmtree(path)
                    removed.append(path.name)
        return GarbageCollectionReport(tuple(candidates), tuple(removed), tuple(retained), dry_run)
