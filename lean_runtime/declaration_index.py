"""Composable declaration-to-module indexes for published environments."""

from __future__ import annotations

import contextlib
import re
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from .errors import EnvironmentError
from .models import ExecutionResult
from .serialization import sha256_id

DECLARATION_SHARD_SCHEMA = "lean-runtime.declaration-shard/v1"
DECLARATION_INDEX_SCHEMA = "lean-runtime.declaration-index/v2"
DECLARATION_INDEX_USER_VERSION = 1

_UNKNOWN_NAME = re.compile(
    r"unknown (?:identifier|constant)\s+['‘`](?P<name>[^'’`\n]+)['’`]",
    re.IGNORECASE,
)
_KIND = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_SHARD_ID = re.compile(r"declshard_[0-9a-f]{64}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def valid_declaration_name(value: str) -> bool:
    """Accept Lean's Unicode and quoted names while excluding unsafe display text."""

    return (
        0 < len(value) <= 1024
        and value.isprintable()
        and "`" not in value
        and not value.startswith(".")
        and not value.endswith(".")
        and ".." not in value
    )


def declaration_shard_identity(
    *,
    source_id: str,
    toolchain: str,
    subdir: str | None,
) -> str:
    """Conservatively identify one package revision under one Lean toolchain."""

    return sha256_id(
        "declshard",
        {
            "schema": DECLARATION_SHARD_SCHEMA,
            "source_id": source_id,
            "toolchain": toolchain,
            "subdir": subdir,
        },
    )


@dataclass(frozen=True, slots=True)
class DeclarationMatch:
    name: str
    module: str
    kind: str
    weight: int


@dataclass(frozen=True, slots=True)
class DeclarationShard:
    shard_id: str
    package: str
    source_id: str
    toolchain: str
    subdir: str | None
    module_roots: tuple[str, ...]
    namespace_roots: tuple[str, ...]
    sqlite_digest: str
    sqlite_size: int
    layer_digest: str
    layer_size: int

    def __post_init__(self) -> None:
        expected = declaration_shard_identity(
            source_id=self.source_id,
            toolchain=self.toolchain,
            subdir=self.subdir,
        )
        if _SHARD_ID.fullmatch(self.shard_id) is None or self.shard_id != expected:
            raise EnvironmentError("declaration shard identity is invalid")
        if not self.package or not self.source_id or not self.toolchain:
            raise EnvironmentError("declaration shard metadata is incomplete")
        if self.sqlite_size < 1 or self.layer_size < 1:
            raise EnvironmentError("declaration shard sizes must be positive")
        if (
            _DIGEST.fullmatch(self.sqlite_digest) is None
            or _DIGEST.fullmatch(self.layer_digest) is None
        ):
            raise EnvironmentError("declaration shard digests are invalid")
        if any(not valid_declaration_name(item) for item in self.module_roots):
            raise EnvironmentError("declaration shard has invalid module roots")
        if any(not valid_declaration_name(item) for item in self.namespace_roots):
            raise EnvironmentError("declaration shard has invalid namespace roots")

    def to_dict(self) -> dict[str, object]:
        return {
            "shard_id": self.shard_id,
            "package": self.package,
            "source_id": self.source_id,
            "toolchain": self.toolchain,
            "subdir": self.subdir,
            "module_roots": list(self.module_roots),
            "namespace_roots": list(self.namespace_roots),
            "sqlite": {"digest": self.sqlite_digest, "size": self.sqlite_size},
            "layer": {"digest": self.layer_digest, "size": self.layer_size},
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> DeclarationShard:
        sqlite_record = value.get("sqlite")
        layer_record = value.get("layer")
        if not isinstance(sqlite_record, dict) or not isinstance(layer_record, dict):
            raise EnvironmentError("declaration shard has invalid content records")
        module_roots = value.get("module_roots")
        namespace_roots = value.get("namespace_roots")
        if not isinstance(module_roots, list) or not all(
            isinstance(item, str) for item in module_roots
        ):
            raise EnvironmentError("declaration shard module roots must be strings")
        if not isinstance(namespace_roots, list) or not all(
            isinstance(item, str) for item in namespace_roots
        ):
            raise EnvironmentError("declaration shard namespace roots must be strings")
        strings = ("shard_id", "package", "source_id", "toolchain")
        if any(not isinstance(value.get(key), str) for key in strings):
            raise EnvironmentError("declaration shard metadata is incomplete")
        subdir = value.get("subdir")
        if subdir is not None and not isinstance(subdir, str):
            raise EnvironmentError("declaration shard subdir must be a string or null")
        try:
            return cls(
                shard_id=str(value["shard_id"]),
                package=str(value["package"]),
                source_id=str(value["source_id"]),
                toolchain=str(value["toolchain"]),
                subdir=subdir,
                module_roots=tuple(module_roots),
                namespace_roots=tuple(namespace_roots),
                sqlite_digest=str(sqlite_record["digest"]),
                sqlite_size=int(sqlite_record["size"]),
                layer_digest=str(layer_record["digest"]),
                layer_size=int(layer_record["size"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EnvironmentError("declaration shard content metadata is invalid") from exc


class DeclarationLookup(Protocol):
    def resolve(self, name: str) -> DeclarationMatch | None: ...

    def resolve_suffix(self, suffix: str, *, limit: int = 3) -> tuple[DeclarationMatch, ...]: ...


class DeclarationIndex:
    """A validated, immutable SQLite package shard."""

    def __init__(self, path: Path, *, expected_shard_id: str) -> None:
        self.path = path.resolve()
        self.shard_id = expected_shard_id
        self._validate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path.as_uri() + "?mode=ro&immutable=1",
            uri=True,
            timeout=2,
        )
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        return connection

    def _validate(self) -> None:
        try:
            with contextlib.closing(self._connect()) as connection:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                metadata = dict(connection.execute("SELECT key, value FROM meta"))
                columns = {
                    str(row[1]) for row in connection.execute("PRAGMA table_info(decl)").fetchall()
                }
                suffix_columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(suffix)").fetchall()
                }
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise EnvironmentError("declaration shard is not a valid SQLite index") from exc
        if version != DECLARATION_INDEX_USER_VERSION:
            raise EnvironmentError(f"unsupported declaration shard version: {version}")
        if metadata.get("schema") != DECLARATION_SHARD_SCHEMA:
            raise EnvironmentError("declaration shard has an unsupported schema")
        if metadata.get("shard_id") != self.shard_id:
            raise EnvironmentError("declaration shard does not match its identity")
        if columns != {"name", "module", "kind", "weight"}:
            raise EnvironmentError("declaration shard declaration table has invalid columns")
        if suffix_columns != {"suffix", "name"}:
            raise EnvironmentError("declaration shard suffix table has invalid columns")

    @staticmethod
    def _match(row: tuple[object, ...] | None) -> DeclarationMatch | None:
        if row is None:
            return None
        name = str(row[0])
        module = str(row[1])
        kind = str(row[2])
        try:
            weight = int(str(row[3]))
        except ValueError as exc:
            raise EnvironmentError("declaration shard returned an invalid weight") from exc
        if (
            not valid_declaration_name(name)
            or not valid_declaration_name(module)
            or _KIND.fullmatch(kind) is None
            or not 0 <= weight <= 2**63 - 1
        ):
            raise EnvironmentError("declaration shard returned an invalid declaration record")
        return DeclarationMatch(name, module, kind, weight)

    @property
    def declaration_count(self) -> int:
        try:
            with contextlib.closing(self._connect()) as connection:
                return int(connection.execute("SELECT count(*) FROM decl").fetchone()[0])
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise EnvironmentError("declaration shard count failed") from exc

    def resolve(self, name: str) -> DeclarationMatch | None:
        if not valid_declaration_name(name):
            return None
        try:
            with contextlib.closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT name, module, kind, weight FROM decl WHERE name = ?",
                    (name,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise EnvironmentError("declaration shard lookup failed") from exc
        return self._match(row)

    def resolve_suffix(self, suffix: str, *, limit: int = 3) -> tuple[DeclarationMatch, ...]:
        if not valid_declaration_name(suffix) or not 1 <= limit <= 20:
            return ()
        try:
            with contextlib.closing(self._connect()) as connection:
                rows = connection.execute(
                    """
                    SELECT d.name, d.module, d.kind, d.weight
                    FROM suffix AS s
                    JOIN decl AS d ON d.name = s.name
                    WHERE s.suffix = ?
                    ORDER BY d.weight DESC, length(d.name), d.name
                    LIMIT ?
                    """,
                    (suffix, limit),
                ).fetchall()
        except sqlite3.Error as exc:
            raise EnvironmentError("declaration shard suffix lookup failed") from exc
        return tuple(match for row in rows if (match := self._match(row)) is not None)


@dataclass(frozen=True, slots=True)
class DeclarationIndexSet:
    lock_id: str
    indexes: tuple[tuple[DeclarationShard, DeclarationIndex], ...]

    @property
    def declaration_count(self) -> int:
        return sum(index.declaration_count for _shard, index in self.indexes)

    def resolve(self, name: str) -> DeclarationMatch | None:
        for _shard, index in self.indexes:
            match = index.resolve(name)
            if match is not None:
                return match
        return None

    def resolve_suffix(self, suffix: str, *, limit: int = 3) -> tuple[DeclarationMatch, ...]:
        candidates = [
            match
            for _shard, index in self.indexes
            for match in index.resolve_suffix(suffix, limit=limit)
        ]
        unique = {match.name: match for match in candidates}
        return tuple(
            sorted(unique.values(), key=lambda item: (-item.weight, len(item.name), item.name))[
                :limit
            ]
        )


def unknown_declaration_names(result: ExecutionResult) -> tuple[str, ...]:
    """Extract compiler-rejected declaration names, with raw-output fallback."""

    messages = [item.message for item in result.diagnostics]
    messages.extend((result.stderr, result.stdout))
    return tuple(
        dict.fromkeys(
            match.group("name")
            for message in messages
            for match in _UNKNOWN_NAME.finditer(message)
            if valid_declaration_name(match.group("name"))
        )
    )


class DeclarationResolver:
    def suggestions(
        self,
        index: DeclarationLookup,
        result: ExecutionResult,
        *,
        environment_label: str,
    ) -> tuple[str, ...]:
        hints: list[str] = []
        for requested in unknown_declaration_names(result):
            exact = index.resolve(requested)
            if exact is not None:
                hints.append(
                    f"`{exact.name}` is defined in `{exact.module}` ({environment_label})."
                )
                continue
            if "." in requested:
                continue
            matches = index.resolve_suffix(requested, limit=3)
            if len(matches) == 1:
                match = matches[0]
                hints.append(
                    f"`{match.name}` is defined in `{match.module}` ({environment_label})."
                )
            elif matches:
                rendered = ", ".join(f"`{match.name}` in `{match.module}`" for match in matches)
                hints.append(f"Possible definitions for `{requested}`: {rendered}.")
        return tuple(dict.fromkeys(hints))

    def enrich(
        self,
        index: DeclarationLookup,
        result: ExecutionResult,
        *,
        environment_label: str,
    ) -> ExecutionResult:
        if result.ok or result.cancelled or result.timed_out:
            return result
        hints = self.suggestions(index, result, environment_label=environment_label)
        return result if not hints else replace(result, hints=(*result.hints, *hints))
