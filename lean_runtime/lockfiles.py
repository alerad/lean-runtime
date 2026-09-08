"""Portable environment locks produced by Lake-backed resolution."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from ._relpath import packages_directory, safe_relative_posix
from .errors import EnvironmentError
from .serialization import freeze_json, sha256_id, thaw_json, write_json_atomic

LOCK_SCHEMA = "lean-runtime-environment-lock/1"
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_'-]*")
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*")
_COMMIT = re.compile(r"[0-9a-fA-F]{40}")
_SOURCE_ID = re.compile(r"source_[0-9a-f]{64}")
_SPEC_ID = re.compile(r"spec_[0-9a-f]{64}")


def _optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise EnvironmentError(f"lock field {field_name!r} must be a string or null")
    return value


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

    def __post_init__(self) -> None:
        if _NAME.fullmatch(self.name) is None:
            raise EnvironmentError(f"invalid locked package name: {self.name!r}")
        if not self.url or "\n" in self.url or "\r" in self.url:
            raise EnvironmentError(f"invalid locked Git URL for package {self.name!r}")
        if _COMMIT.fullmatch(self.revision) is None:
            raise EnvironmentError(f"invalid locked revision for package {self.name!r}")
        if _COMMIT.fullmatch(self.tree_hash) is None:
            raise EnvironmentError(f"invalid locked tree hash for package {self.name!r}")
        if _SOURCE_ID.fullmatch(self.source_id) is None:
            raise EnvironmentError(f"invalid source identity for package {self.name!r}")
        if self.requested_revision is not None and not self.requested_revision:
            raise EnvironmentError(f"empty requested revision for package {self.name!r}")
        if self.root_module is not None and _MODULE.fullmatch(self.root_module) is None:
            raise EnvironmentError(f"invalid root module for package {self.name!r}")
        if self.subdir is not None:
            try:
                safe_relative_posix(self.subdir)
            except ValueError as exc:
                raise EnvironmentError(f"unsafe package subdir in lock: {self.subdir!r}") from exc
        if not isinstance(self.artifact_command, tuple) or any(
            not isinstance(item, str) or not item or "\x00" in item
            for item in self.artifact_command
        ):
            raise EnvironmentError(f"invalid artifact command for package {self.name!r}")

    @property
    def subdir_path(self) -> PurePosixPath | None:
        """The validated package subdirectory, ready to join below the package root."""
        return safe_relative_posix(self.subdir) if self.subdir is not None else None

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
        command = value.get("artifact_command", [])
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            raise EnvironmentError("locked artifact_command must be an array of strings")
        inherited = value.get("inherited", False)
        if not isinstance(inherited, bool):
            raise EnvironmentError("locked package inherited must be Boolean")
        required = ("name", "url", "revision", "tree_hash", "source_id")
        if any(not isinstance(value.get(key), str) for key in required):
            raise EnvironmentError("locked package is missing a required string field")
        return cls(
            name=value["name"],
            url=value["url"],
            requested_revision=_optional_string(
                value.get("requested_revision"), "requested_revision"
            ),
            revision=value["revision"],
            tree_hash=value["tree_hash"],
            source_id=value["source_id"],
            inherited=inherited,
            subdir=_optional_string(value.get("subdir"), "subdir"),
            root_module=_optional_string(value.get("root_module"), "root_module"),
            artifact_command=tuple(command),
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

    def __post_init__(self) -> None:
        if not self.toolchain or "\x00" in self.toolchain:
            raise EnvironmentError("lock contains an invalid toolchain")
        if _SPEC_ID.fullmatch(self.spec_digest) is None:
            raise EnvironmentError("lock contains an invalid specification digest")
        if not self.root_lakefile or "\x00" in self.root_lakefile:
            raise EnvironmentError("lock contains an invalid root Lake configuration")
        if "\x00" in self.root_module:
            raise EnvironmentError("lock contains invalid root Lean source")
        if not isinstance(self.manifest, dict):
            raise EnvironmentError("environment lock manifest must be an object")
        if not isinstance(self.packages, tuple) or not all(
            isinstance(item, LockedPackage) for item in self.packages
        ):
            raise EnvironmentError("environment lock packages must be locked packages")
        names = [package.name for package in self.packages]
        if len(names) != len(set(names)):
            raise EnvironmentError("lock contains duplicate package names")
        try:
            packages_directory(self.manifest)
        except ValueError as exc:
            raise EnvironmentError(f"lock packagesDir is unsafe: {exc}") from exc
        # The manifest is part of the lock's identity: freeze it so nothing can
        # move the identity out from under handles that already hold lock_id.
        object.__setattr__(self, "manifest", freeze_json(self.manifest))

    @property
    def packages_directory(self) -> PurePosixPath:
        """The validated Lake ``packagesDir``, relative to the workspace."""
        return packages_directory(self.manifest)

    def package_root(self, workspace: Path, package: LockedPackage) -> Path:
        """Where ``package``'s own Lake root lives below a materialized workspace."""
        root = workspace.joinpath(*self.packages_directory.parts) / package.name
        subdir = package.subdir_path
        return root.joinpath(*subdir.parts) if subdir is not None else root

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema": LOCK_SCHEMA,
            "toolchain": self.toolchain,
            "spec_digest": self.spec_digest,
            "root_lakefile": self.root_lakefile,
            "root_module": self.root_module,
            "manifest": thaw_json(self.manifest),
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
        required_strings = ("toolchain", "spec_digest", "root_lakefile", "root_module")
        if any(not isinstance(value.get(key), str) for key in required_strings):
            raise EnvironmentError("environment lock is missing a required string field")
        manifest = value.get("manifest")
        packages = value.get("packages")
        if not isinstance(manifest, dict):
            raise EnvironmentError("environment lock manifest must be an object")
        if not isinstance(packages, list) or not all(isinstance(item, dict) for item in packages):
            raise EnvironmentError("environment lock packages must be an array of objects")
        lock = cls(
            toolchain=value["toolchain"],
            spec_digest=value["spec_digest"],
            root_lakefile=value["root_lakefile"],
            root_module=value["root_module"],
            manifest=manifest,
            packages=tuple(LockedPackage.from_dict(item) for item in packages),
        )
        recorded = value.get("lock_id")
        if recorded is not None and recorded != lock.lock_id:
            raise EnvironmentError(
                f"lock identity mismatch: recorded {recorded!r}, computed {lock.lock_id!r}"
            )
        return lock

    @classmethod
    def load(cls, path: str | Path) -> EnvironmentLock:
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EnvironmentError(f"could not read environment lock: {path}") from exc
        if not isinstance(value, dict):
            raise EnvironmentError("environment lock must contain an object")
        return cls.from_dict(value)
