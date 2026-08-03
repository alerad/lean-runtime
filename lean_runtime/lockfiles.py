"""Portable environment locks produced by Lake-backed resolution."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import EnvironmentError
from .serialization import sha256_id, write_json_atomic

LOCK_SCHEMA = "lean-runtime-environment-lock/1"


@dataclass(frozen=True, slots=True)
class LockedPackage:
    name: str
    url: str
    revision: str
    source_id: str
    tree_hash: str
    requested_revision: str | None = None
    inherited: bool = False
    subdir: str | None = None
    root_module: str | None = None
    artifact_command: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": "git",
            "url": self.url,
            "requested_revision": self.requested_revision,
            "revision": self.revision,
            "tree_hash": self.tree_hash,
            "source_id": self.source_id,
            "inherited": self.inherited,
            "subdir": self.subdir,
            "root_module": self.root_module,
            "artifact_command": list(self.artifact_command),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> LockedPackage:
        if value.get("source") != "git":
            raise EnvironmentError("lock contains an unsupported package source")
        return cls(
            name=str(value["name"]),
            url=str(value["url"]),
            requested_revision=value.get("requested_revision"),
            revision=str(value["revision"]),
            tree_hash=str(value["tree_hash"]),
            source_id=str(value["source_id"]),
            inherited=bool(value.get("inherited", False)),
            subdir=value.get("subdir"),
            root_module=value.get("root_module"),
            artifact_command=tuple(value.get("artifact_command", [])),
        )


@dataclass(frozen=True, slots=True)
class EnvironmentLock:
    """A canonical, platform-independent resolved dependency graph."""

    toolchain: str
    spec_digest: str
    root_lakefile: str
    root_module: str
    manifest: dict[str, Any]
    packages: tuple[LockedPackage, ...]

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema": LOCK_SCHEMA,
            "toolchain": self.toolchain,
            "spec_digest": self.spec_digest,
            "root_lakefile": self.root_lakefile,
            "root_module": self.root_module,
            "manifest": self.manifest,
            "packages": [
                package.to_dict() for package in sorted(self.packages, key=lambda item: item.name)
            ],
        }

    @property
    def lock_id(self) -> str:
        return sha256_id("lock", self.identity_payload())

    def to_dict(self) -> dict[str, Any]:
        return {"lock_id": self.lock_id, **self.identity_payload()}

    def write(self, path: str | Path) -> None:
        write_json_atomic(Path(path), self.to_dict())

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> EnvironmentLock:
        if value.get("schema") != LOCK_SCHEMA:
            raise EnvironmentError(f"unsupported lock schema: {value.get('schema')!r}")
        lock = cls(
            toolchain=str(value["toolchain"]),
            spec_digest=str(value["spec_digest"]),
            root_lakefile=str(value["root_lakefile"]),
            root_module=str(value["root_module"]),
            manifest=dict(value["manifest"]),
            packages=tuple(LockedPackage.from_dict(item) for item in value["packages"]),
        )
        recorded = value.get("lock_id")
        if recorded is not None and recorded != lock.lock_id:
            raise EnvironmentError(
                f"lock identity mismatch: recorded {recorded!r}, computed {lock.lock_id!r}"
            )
        return lock

    @classmethod
    def load(cls, path: str | Path) -> EnvironmentLock:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise EnvironmentError("environment lock must contain an object")
        return cls.from_dict(value)
