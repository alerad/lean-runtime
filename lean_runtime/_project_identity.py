"""Internal, pure identity calculation for shared Lake project packages.

A shared package directory is content-addressed by the identity computed
here from its manifest entry, resolved dependencies and toolchain build
identity. Nothing in this module touches the store."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._git import git_tree_hash
from ._platform import platform_compatibility
from .errors import ProjectError
from .projects import ProjectContext
from .store import source_snapshot_digest
from .toolchains import ToolchainBuildIdentity

SHARED_PROJECT_SCHEMA = "lean-runtime-shared-project/4"
PACKAGE_ARTIFACT_SCHEMA = "lean-runtime-package-artifact-key/3"


def canonical_git_url(value: str) -> str:
    url = value.strip().rstrip("/")
    if url.startswith("git@github.com:"):
        url = "https://github.com/" + url.removeprefix("git@github.com:")
    elif url.startswith("ssh://git@github.com/"):
        url = "https://github.com/" + url.removeprefix("ssh://git@github.com/")
    return url.removesuffix(".git")


@dataclass(frozen=True, slots=True)
class PackageSourceKey:
    """Immutable identity of one Git-backed package source tree."""

    canonical_url: str
    revision: str
    subdir: str
    tree_hash: str

    def to_dict(self) -> dict[str, str]:
        return {
            "canonical_url": self.canonical_url,
            "revision": self.revision,
            "subdir": self.subdir,
            "tree_hash": self.tree_hash,
        }


@dataclass(frozen=True, slots=True)
class DependencyKey:
    """Semantic resolved identity of one package in an artifact dependency cone."""

    name: str
    source_type: str
    canonical_url: str | None = None
    revision: str | None = None
    subdir: str = ""
    config_file: str | None = None
    manifest_file: str | None = None
    directory: str | None = None
    content_digest: str | None = None

    @classmethod
    def from_entry(cls, entry: dict[str, Any]) -> DependencyKey:
        source_type = str(entry.get("type", ""))
        raw_url = entry.get("url")
        return cls(
            name=str(entry.get("name", "")),
            source_type=source_type,
            canonical_url=(
                canonical_git_url(raw_url)
                if source_type == "git" and isinstance(raw_url, str)
                else None
            ),
            revision=str(entry["rev"]) if isinstance(entry.get("rev"), str) else None,
            subdir=str(entry.get("subDir") or ""),
            config_file=(
                str(entry["configFile"]) if isinstance(entry.get("configFile"), str) else None
            ),
            manifest_file=(
                str(entry["manifestFile"]) if isinstance(entry.get("manifestFile"), str) else None
            ),
            directory=str(entry["dir"]) if isinstance(entry.get("dir"), str) else None,
            content_digest=(
                str(entry["content_digest"])
                if isinstance(entry.get("content_digest"), str)
                else None
            ),
        )

    def to_dict(self) -> dict[str, str]:
        values = {
            "name": self.name,
            "type": self.source_type,
            "url": self.canonical_url,
            "rev": self.revision,
            "subDir": self.subdir or None,
            "configFile": self.config_file,
            "manifestFile": self.manifest_file,
            "dir": self.directory,
            "content_digest": self.content_digest,
        }
        return {key: value for key, value in values.items() if value is not None}


@dataclass(frozen=True, slots=True)
class PackageArtifactKey:
    """Compatibility key for moving compiled artifacts between exact package cones."""

    package_name: str
    source: PackageSourceKey
    toolchain: str
    lean_executable_digest: str
    lake_executable_digest: str
    platform_abi: dict[str, str]
    config_file: str
    manifest_file: str
    dependency_cone: tuple[DependencyKey, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PACKAGE_ARTIFACT_SCHEMA,
            "package_name": self.package_name,
            "source": self.source.to_dict(),
            "toolchain": self.toolchain,
            "lean_executable_digest": self.lean_executable_digest,
            "lake_executable_digest": self.lake_executable_digest,
            "platform_abi": self.platform_abi,
            "config_file": self.config_file,
            "manifest_file": self.manifest_file,
            "dependency_cone": [dependency.to_dict() for dependency in self.dependency_cone],
        }


def resolved_path_entries(
    context: ProjectContext,
    packages: list[dict[str, Any]],
    *,
    digest: Callable[[Path], str] | None = None,
) -> list[dict[str, Any]]:
    identity: list[dict[str, Any]] = []
    for entry in packages:
        normalized = dict(entry)
        if entry.get("type") == "path":
            raw = entry.get("dir")
            if not isinstance(raw, str):
                raise ProjectError(f"path dependency {entry['name']!r} has no directory")
            path = (context.root / raw).resolve()
            if not path.is_dir():
                raise ProjectError(f"path dependency {entry['name']!r} does not exist: {path}")
            normalized["dir"] = str(path)
            normalized["content_digest"] = (digest or source_snapshot_digest)(path)
        identity.append(normalized)
    return identity


def entry_identity(entry: dict[str, Any]) -> dict[str, Any]:
    """Keep only fields that can alter a materialized package or its name."""
    return DependencyKey.from_entry(entry).to_dict()


def effective_dependency_entries(
    entries: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Conservative effective graph; stored manifests cannot establish its edges."""
    return tuple(DependencyKey.from_entry(entries[name]).to_dict() for name in sorted(entries))


def package_subdir(entry: dict[str, Any]) -> Path | None:
    raw = entry.get("subDir")
    if raw in {None, ""}:
        return None
    if not isinstance(raw, str):
        raise ProjectError(f"package {entry.get('name')!r} has a non-string subDir")
    subdir = Path(raw)
    if subdir.is_absolute() or ".." in subdir.parts:
        raise ProjectError(f"package {entry.get('name')!r} has an unsafe subDir: {raw}")
    return subdir


def package_source_key(entry: dict[str, Any], source_package: Path) -> PackageSourceKey | None:
    url = entry.get("url")
    revision = entry.get("rev")
    tree_hash = git_tree_hash(source_package)
    if not isinstance(url, str) or not isinstance(revision, str) or tree_hash is None:
        return None
    return PackageSourceKey(
        canonical_url=canonical_git_url(url),
        revision=revision,
        subdir=str(entry.get("subDir") or ""),
        tree_hash=tree_hash,
    )


def resolved_dependency_names(source_package: Path, manifest_name: str) -> set[str] | None:
    manifest_path = source_package / manifest_name
    if not manifest_path.is_file():
        return None
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectError(f"could not read dependency manifest {manifest_path}: {exc}") from exc
    entries = value.get("packages") if isinstance(value, dict) else None
    if not isinstance(entries, list):
        return None
    return {
        str(item["name"])
        for item in entries
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def package_artifact_key(
    *,
    context: ProjectContext,
    entry: dict[str, Any],
    source_package: Path,
    effective_entries: dict[str, dict[str, Any]],
    toolchain_identity: ToolchainBuildIdentity | None,
) -> PackageArtifactKey | None:
    if toolchain_identity is None:
        return None
    source = package_source_key(entry, source_package)
    if source is None:
        return None
    manifest_name = str(entry.get("manifestFile", "lake-manifest.json"))
    cone = tuple(
        DependencyKey.from_entry(dependency)
        for dependency in effective_dependency_entries(effective_entries)
    )
    return PackageArtifactKey(
        package_name=str(entry.get("name", "")),
        source=source,
        toolchain=toolchain_identity.toolchain,
        lean_executable_digest=toolchain_identity.lean_executable_digest,
        lake_executable_digest=toolchain_identity.lake_executable_digest,
        platform_abi=platform_compatibility(),
        config_file=str(entry.get("configFile", "lakefile.toml")),
        manifest_file=manifest_name,
        dependency_cone=cone,
    )


def compute_package_identity(
    *,
    context: ProjectContext,
    entry: dict[str, Any],
    source_package: Path,
    effective_entries: dict[str, dict[str, Any]],
    toolchain_identity: ToolchainBuildIdentity | None,
) -> dict[str, Any]:
    dependencies = list(effective_dependency_entries(effective_entries))
    artifact_key = package_artifact_key(
        context=context,
        entry=entry,
        source_package=source_package,
        effective_entries=effective_entries,
        toolchain_identity=toolchain_identity,
    )
    return {
        "schema": SHARED_PROJECT_SCHEMA,
        "toolchain": context.toolchain,
        "platform": platform_compatibility(),
        "package": entry_identity(entry),
        "effective_dependencies": dependencies,
        "artifact_key": artifact_key.to_dict() if artifact_key is not None else None,
    }


def normalized_package_identity(identity: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize legacy marker spellings without weakening graph compatibility."""
    if identity.get("schema") != SHARED_PROJECT_SCHEMA:
        return None
    package = identity.get("package")
    dependencies = identity.get("effective_dependencies")
    if not isinstance(package, dict) or not isinstance(dependencies, list):
        return None
    normalized_dependencies: list[dict[str, Any]] = []
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            return None
        normalized_dependencies.append(entry_identity(dependency))
    artifact_key = identity.get("artifact_key")
    normalized_artifact = (
        artifact_key
        if isinstance(artifact_key, dict) and artifact_key.get("schema") == PACKAGE_ARTIFACT_SCHEMA
        else None
    )
    return {
        "toolchain": identity.get("toolchain"),
        "platform": identity.get("platform"),
        "package": entry_identity(package),
        "effective_dependencies": normalized_dependencies,
        "artifact_key": normalized_artifact,
    }
