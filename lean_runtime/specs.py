"""Declarative Lean environment specifications."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[import-not-found]

from .errors import SpecificationError
from .serialization import sha256_id
from .toolchains import normalize_toolchain

SPEC_SCHEMA = "lean-runtime-environment-spec/1"
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_'-]*")
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*")
_COMMIT = re.compile(r"[0-9a-fA-F]{40}")


@dataclass(frozen=True, slots=True)
class GitPackage:
    """One exact Git dependency.

    Initial environment locks intentionally accept only full commit hashes.
    ``root_module`` is imported by the generated root library so Lake builds
    the dependency's Lean artifacts before the environment is published.
    """

    name: str
    url: str
    rev: str
    root_module: str | None = None
    subdir: str | None = None
    artifact_command: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if _NAME.fullmatch(self.name) is None:
            raise SpecificationError(f"invalid Lake package name: {self.name!r}")
        if not self.url or "\n" in self.url or "\r" in self.url:
            raise SpecificationError(f"invalid Git URL for package {self.name!r}")
        if _COMMIT.fullmatch(self.rev) is None:
            raise SpecificationError(
                f"package {self.name!r} requires a full 40-character Git commit"
            )
        if self.root_module is not None and _MODULE.fullmatch(self.root_module) is None:
            raise SpecificationError(
                f"invalid root module for package {self.name!r}: {self.root_module!r}"
            )
        if self.subdir is not None:
            subdir = Path(self.subdir)
            if subdir.is_absolute() or ".." in subdir.parts:
                raise SpecificationError(f"package subdir must be relative: {self.subdir!r}")
        if any(not item or "\x00" in item for item in self.artifact_command):
            raise SpecificationError(f"invalid artifact command for package {self.name!r}")

    @property
    def module(self) -> str:
        if self.root_module:
            return self.root_module
        return self.name[0].upper() + self.name[1:]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": "git",
            "name": self.name,
            "url": self.url,
            "rev": self.rev.lower(),
            "root_module": self.module,
            "subdir": self.subdir,
            "artifact_command": list(self.artifact_command),
        }

    @classmethod
    def git(
        cls,
        name: str,
        url: str,
        rev: str,
        *,
        root_module: str | None = None,
        subdir: str | None = None,
        artifact_command: tuple[str, ...] = (),
    ) -> GitPackage:
        """Convenience constructor allowing ``Package.git(...)``."""
        return cls(name, url, rev, root_module, subdir, artifact_command)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> GitPackage:
        if value.get("source", "git") != "git":
            raise SpecificationError("the initial release supports only Git packages")
        command = value.get("artifact_command") or []
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            raise SpecificationError("artifact_command must be an array of strings")
        return cls(
            name=str(value.get("name", "")),
            url=str(value.get("url", "")),
            rev=str(value.get("rev", "")),
            root_module=value.get("root_module"),
            subdir=value.get("subdir"),
            artifact_command=tuple(command),
        )


Package = GitPackage


@dataclass(frozen=True, slots=True)
class EnvironmentSpec:
    """Canonical inputs to the environment compiler."""

    toolchain: str
    packages: tuple[GitPackage, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "toolchain", normalize_toolchain(self.toolchain))
        names = [package.name for package in self.packages]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise SpecificationError("duplicate direct package names: " + ", ".join(duplicates))

    @property
    def spec_digest(self) -> str:
        return sha256_id("spec", self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SPEC_SCHEMA,
            "toolchain": self.toolchain,
            "packages": [
                package.to_dict() for package in sorted(self.packages, key=lambda item: item.name)
            ],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> EnvironmentSpec:
        schema = value.get("schema", SPEC_SCHEMA)
        if schema != SPEC_SCHEMA:
            raise SpecificationError(f"unsupported environment specification schema: {schema!r}")
        raw_packages = value.get("packages", value.get("package", []))
        if not isinstance(raw_packages, list):
            raise SpecificationError("packages must be an array")
        return cls(
            toolchain=str(value.get("toolchain", "")),
            packages=tuple(GitPackage.from_dict(item) for item in raw_packages),
        )

    @classmethod
    def load(cls, path: str | Path) -> EnvironmentSpec:
        source = Path(path)
        with source.open("rb") as handle:
            value = (
                tomllib.load(handle)
                if source.suffix.lower() == ".toml"
                else json.loads(handle.read().decode("utf-8"))
            )
        if not isinstance(value, dict):
            raise SpecificationError("environment specification must be an object")
        return cls.from_dict(value)
