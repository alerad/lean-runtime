"""Deterministic declaration-shard generation from public Lean `.ilean` files."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .capsules import CAPSULE_MANIFEST
from .declaration_index import (
    DECLARATION_INDEX_USER_VERSION,
    DECLARATION_SHARD_SCHEMA,
    DeclarationIndex,
    declaration_shard_identity,
    valid_declaration_name,
)
from .declaration_index_oci import DeclarationShardSource
from .errors import EnvironmentError
from .lockfiles import EnvironmentLock, LockedPackage
from .runtime import Runtime
from .serialization import canonical_json_bytes

DECLARATION_INDEX_BUILD_SCHEMA = "lean-runtime.declaration-index-build/v1"
MAX_ILEAN_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class BuiltDeclarationShard:
    source: DeclarationShardSource
    declarations: int

    def to_dict(self, *, base: Path) -> dict[str, object]:
        return {
            "shard_id": self.source.shard_id,
            "package": self.source.package,
            "source_id": self.source.source_id,
            "toolchain": self.source.toolchain,
            "subdir": self.source.subdir,
            "module_roots": list(self.source.module_roots),
            "namespace_roots": list(self.source.namespace_roots),
            "path": self.source.path.relative_to(base).as_posix(),
            "declarations": self.declarations,
        }


@dataclass(frozen=True, slots=True)
class DeclarationIndexBuild:
    lock_id: str
    shards: tuple[BuiltDeclarationShard, ...]
    manifest_path: Path

    def to_dict(self) -> dict[str, object]:
        base = self.manifest_path.parent
        return {
            "schema": DECLARATION_INDEX_BUILD_SCHEMA,
            "lock_id": self.lock_id,
            "shards": [item.to_dict(base=base) for item in self.shards],
        }


def _weights(path: Path | None) -> dict[str, int]:
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EnvironmentError(f"could not read declaration weights: {path}") from exc
    if not isinstance(value, dict):
        raise EnvironmentError("declaration weights must be a JSON object")
    result: dict[str, int] = {}
    for name, weight in value.items():
        if (
            not isinstance(name, str)
            or not valid_declaration_name(name)
            or isinstance(weight, bool)
            or not isinstance(weight, int)
            or not 0 <= weight <= 2**63 - 1
        ):
            raise EnvironmentError("declaration weights contain an invalid name or count")
        result[name] = weight
    return result


def _module(path: Path, root: Path, payload: Mapping[str, Any]) -> str:
    recorded = payload.get("module")
    if isinstance(recorded, str) and valid_declaration_name(recorded):
        return recorded
    relative = path.relative_to(root).with_suffix("")
    inferred = ".".join(relative.parts)
    if not valid_declaration_name(inferred):
        raise EnvironmentError(f"could not infer a safe module name from {path}")
    return inferred


def _declarations(root: Path) -> tuple[dict[str, str], tuple[str, ...], tuple[str, ...]]:
    if not root.is_dir():
        raise EnvironmentError(f"compiled declaration root is unavailable: {root}")
    declarations: dict[str, str] = {}
    ambiguous: set[str] = set()
    modules: set[str] = set()
    ileans = tuple(sorted(root.rglob("*.ilean"), key=lambda item: item.as_posix()))
    if not ileans:
        raise EnvironmentError(f"compiled declaration root contains no .ilean files: {root}")
    for path in ileans:
        if path.stat().st_size > MAX_ILEAN_BYTES:
            raise EnvironmentError(f"Lean index exceeds its supported size: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise EnvironmentError(f"could not read Lean declaration index: {path}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("decls"), dict):
            raise EnvironmentError(f"Lean declaration index has no declaration map: {path}")
        module = _module(path, root, payload)
        modules.add(module.split(".", 1)[0])
        for raw_name in payload["decls"]:
            name = str(raw_name)
            if not valid_declaration_name(name) or any(
                component.startswith("_aux") for component in name.split(".")
            ):
                continue
            if name in ambiguous:
                continue
            previous = declarations.get(name)
            if previous is not None and previous != module:
                declarations.pop(name)
                ambiguous.add(name)
                continue
            declarations[name] = module
    if not declarations:
        raise EnvironmentError(f"compiled declaration root produced no public names: {root}")
    namespace_roots = {name.split(".", 1)[0] for name in declarations}
    return declarations, tuple(sorted(modules)), tuple(sorted(namespace_roots))


def _write_shard(
    output: Path,
    *,
    shard_id: str,
    source_id: str,
    toolchain: str,
    subdir: str | None,
    declarations: Mapping[str, str],
    weights: Mapping[str, int],
) -> None:
    if output.exists():
        raise EnvironmentError(f"refusing to overwrite declaration shard: {output}")
    with sqlite3.connect(output) as connection:
        connection.executescript(
            f"""
            PRAGMA page_size = 4096;
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;
            PRAGMA temp_store = MEMORY;
            PRAGMA auto_vacuum = NONE;
            PRAGMA application_id = 1279411268;
            PRAGMA user_version = {DECLARATION_INDEX_USER_VERSION};
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE decl (
              name TEXT PRIMARY KEY,
              module TEXT NOT NULL,
              kind TEXT NOT NULL,
              weight INTEGER NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE suffix (
              suffix TEXT NOT NULL,
              name TEXT NOT NULL,
              PRIMARY KEY (suffix, name)
            ) WITHOUT ROWID;
            """
        )
        metadata = {
            "schema": DECLARATION_SHARD_SCHEMA,
            "shard_id": shard_id,
            "source_id": source_id,
            "subdir": subdir or "",
            "toolchain": toolchain,
        }
        connection.executemany("INSERT INTO meta VALUES (?, ?)", sorted(metadata.items()))
        rows = [
            (name, declarations[name], "declaration", int(weights.get(name, 0)))
            for name in sorted(declarations)
        ]
        connection.executemany("INSERT INTO decl VALUES (?, ?, ?, ?)", rows)
        connection.executemany(
            "INSERT INTO suffix VALUES (?, ?)",
            sorted((name.rsplit(".", 1)[-1], name) for name in declarations),
        )
        connection.commit()
        connection.execute("VACUUM")
    DeclarationIndex(output, expected_shard_id=shard_id)


def _package_root(workspace: Path, lock: EnvironmentLock, package: LockedPackage) -> Path:
    raw_packages_dir = lock.manifest.get("packagesDir", ".lake/packages")
    if not isinstance(raw_packages_dir, str):
        raise EnvironmentError("lock packagesDir must be a relative string")
    packages_dir = PurePosixPath(raw_packages_dir)
    if packages_dir.is_absolute() or ".." in packages_dir.parts:
        raise EnvironmentError("lock packagesDir must be a safe relative string")
    root = workspace.joinpath(*packages_dir.parts) / package.name
    if package.subdir:
        root = root.joinpath(*PurePosixPath(package.subdir).parts)
    return root / ".lake" / "build" / "lib" / "lean"


def build_declaration_index(
    runtime: Runtime,
    lock: EnvironmentLock,
    output: Path,
    *,
    weights_path: Path | None = None,
) -> DeclarationIndexBuild:
    """Build deterministic core and package shards for one exact environment."""

    destination = output.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    manifest_path = destination / "declaration-index-build.json"
    if manifest_path.exists() or any(destination.glob("*.sqlite")):
        raise EnvironmentError(f"declaration index output is not empty: {destination}")
    weights = _weights(weights_path)
    environment = runtime.open_exact(lock)
    if (environment.workspace / CAPSULE_MANIFEST).is_file():
        raise EnvironmentError(
            "declaration shard generation requires a full source environment, not a sparse capsule"
        )
    roots: list[tuple[str, str, str | None, Path]] = [
        (
            "core",
            "core:" + lock.toolchain,
            None,
            runtime.toolchains.full_path(lock.toolchain) / "lib" / "lean",
        )
    ]
    roots.extend(
        (
            package.name,
            package.source_id,
            package.subdir,
            _package_root(environment.workspace, lock, package),
        )
        for package in lock.packages
    )
    built: list[BuiltDeclarationShard] = []
    for package, source_id, subdir, root in roots:
        shard_id = declaration_shard_identity(
            source_id=source_id,
            toolchain=lock.toolchain,
            subdir=subdir,
        )
        declarations, module_roots, namespace_roots = _declarations(root)
        path = destination / f"{shard_id}.sqlite"
        _write_shard(
            path,
            shard_id=shard_id,
            source_id=source_id,
            toolchain=lock.toolchain,
            subdir=subdir,
            declarations=declarations,
            weights=weights,
        )
        source = DeclarationShardSource(
            shard_id,
            package,
            source_id,
            lock.toolchain,
            subdir,
            module_roots,
            namespace_roots,
            path,
        )
        built.append(BuiltDeclarationShard(source, len(declarations)))
    result = DeclarationIndexBuild(lock.lock_id, tuple(built), manifest_path)
    manifest_path.write_bytes(canonical_json_bytes(result.to_dict()))
    return result


def load_declaration_index_build(
    path: Path, *, expected_lock_id: str | None = None
) -> DeclarationIndexBuild:
    source = path.expanduser().resolve()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EnvironmentError(f"could not read declaration index build: {source}") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema") != DECLARATION_INDEX_BUILD_SCHEMA
        or not isinstance(value.get("lock_id"), str)
        or not isinstance(value.get("shards"), list)
    ):
        raise EnvironmentError("declaration index build manifest is invalid")
    lock_id = str(value["lock_id"])
    if expected_lock_id is not None and lock_id != expected_lock_id:
        raise EnvironmentError("declaration index build does not match its lock")
    built: list[BuiltDeclarationShard] = []
    for raw in value["shards"]:
        if not isinstance(raw, dict):
            raise EnvironmentError("declaration index build contains an invalid shard")
        required = ("shard_id", "package", "source_id", "toolchain", "path")
        if any(not isinstance(raw.get(key), str) for key in required):
            raise EnvironmentError("declaration index build shard metadata is incomplete")
        subdir = raw.get("subdir")
        module_roots = raw.get("module_roots")
        namespace_roots = raw.get("namespace_roots")
        declarations = raw.get("declarations")
        if (
            subdir is not None
            and not isinstance(subdir, str)
            or not isinstance(module_roots, list)
            or not all(isinstance(item, str) for item in module_roots)
            or not isinstance(namespace_roots, list)
            or not all(isinstance(item, str) for item in namespace_roots)
            or isinstance(declarations, bool)
            or not isinstance(declarations, int)
            or declarations < 1
        ):
            raise EnvironmentError("declaration index build shard fields are invalid")
        relative = PurePosixPath(str(raw["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise EnvironmentError("declaration index build has an unsafe shard path")
        shard_path = source.parent.joinpath(*relative.parts)
        shard = DeclarationShardSource(
            str(raw["shard_id"]),
            str(raw["package"]),
            str(raw["source_id"]),
            str(raw["toolchain"]),
            subdir,
            tuple(module_roots),
            tuple(namespace_roots),
            shard_path,
        )
        DeclarationIndex(shard_path, expected_shard_id=shard.shard_id)
        built.append(BuiltDeclarationShard(shard, declarations))
    return DeclarationIndexBuild(lock_id, tuple(built), source)
