"""Versioned catalogs of exact Lean Runtime environment locks."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from ..errors import LeanRuntimeError
from ..lockfiles import EnvironmentLock
from .errors import CatalogError

CATALOG_SCHEMA = "lean-runtime.discovery.catalog/v1"
MAX_CATALOG_BYTES = 20 * 1024 * 1024
_TOP_LEVEL_KEYS = frozenset({"schema", "generated_at", "entries"})
_ENTRY_KEYS = frozenset(
    {
        "id",
        "channel",
        "toolchain",
        "lock_id",
        "lock",
        "modules",
        # Accepted for compatibility with early v1 catalogs, but ignored.
        "library_hints",
        "created_at",
    }
)
_REQUIRED_ENTRY_KEYS = _ENTRY_KEYS - {"library_hints"}
_ENTRY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*")


def _timestamp(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise CatalogError(f"{field_name} must be a non-empty timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CatalogError(f"{field_name} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise CatalogError(f"{field_name} must include a timezone")
    return value


def _strings(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise CatalogError(f"{field_name} must be an array of non-empty strings")
    if len(value) != len(set(value)):
        raise CatalogError(f"{field_name} must not contain duplicates")
    return tuple(value)


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One exact environment worth probing."""

    id: str
    channel: str
    toolchain: str
    lock: EnvironmentLock
    modules: frozenset[str] = field(default_factory=frozenset)
    created_at: str = "1970-01-01T00:00:00Z"
    _created_timestamp: float = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if _ENTRY_ID.fullmatch(self.id) is None:
            raise CatalogError(f"invalid catalog entry id: {self.id!r}")
        if _ENTRY_ID.fullmatch(self.channel) is None:
            raise CatalogError(f"invalid catalog channel: {self.channel!r}")
        if self.toolchain != self.lock.toolchain:
            raise CatalogError(f"entry {self.id!r} toolchain disagrees with its lock")
        if any(_MODULE.fullmatch(item) is None for item in self.modules):
            raise CatalogError(f"entry {self.id!r} contains an invalid module")
        created_at = _timestamp(self.created_at, f"entry {self.id!r} created_at")
        object.__setattr__(
            self,
            "_created_timestamp",
            datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp(),
        )

    @property
    def package_names(self) -> frozenset[str]:
        return frozenset(package.name.lower() for package in self.lock.packages)

    @property
    def created_timestamp(self) -> float:
        return self._created_timestamp

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "channel": self.channel,
            "toolchain": self.toolchain,
            "lock_id": self.lock.lock_id,
            "lock": self.lock.to_dict(),
            "modules": sorted(self.modules),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CatalogEntry:
        unknown = set(value) - _ENTRY_KEYS
        missing = _REQUIRED_ENTRY_KEYS - set(value)
        if unknown or missing:
            detail = f"missing={sorted(missing)}, unknown={sorted(unknown)}"
            raise CatalogError(f"catalog entry fields mismatch; {detail}")
        strings = ("id", "channel", "toolchain", "lock_id")
        if any(not isinstance(value.get(key), str) or not value[key] for key in strings):
            raise CatalogError("catalog entry is missing a required non-empty string")
        lock_value = value["lock"]
        if not isinstance(lock_value, dict):
            raise CatalogError("catalog entry lock must be an object")
        try:
            lock = EnvironmentLock.from_dict(lock_value)
        except LeanRuntimeError as exc:
            raise CatalogError(f"invalid Runtime lock in entry {value['id']!r}: {exc}") from exc
        if value["lock_id"] != lock.lock_id:
            raise CatalogError(f"entry {value['id']!r} lock_id disagrees with its lock")
        return cls(
            id=value["id"],
            channel=value["channel"],
            toolchain=value["toolchain"],
            lock=lock,
            modules=frozenset(_strings(value["modules"], "modules")),
            created_at=_timestamp(value["created_at"], "created_at"),
        )


@dataclass(frozen=True, slots=True)
class Catalog:
    """An immutable catalog snapshot."""

    generated_at: str
    entries: tuple[CatalogEntry, ...]
    schema: str = CATALOG_SCHEMA
    _digest: str = field(init=False, repr=False, compare=False)
    _entries_by_id: dict[str, CatalogEntry] = field(init=False, repr=False, compare=False)
    _entries_by_lock: dict[str, CatalogEntry] = field(init=False, repr=False, compare=False)
    _module_index: dict[str, frozenset[str]] = field(init=False, repr=False, compare=False)
    _package_index: dict[str, frozenset[str]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.schema != CATALOG_SCHEMA:
            raise CatalogError(f"unsupported catalog schema: {self.schema!r}")
        _timestamp(self.generated_at, "generated_at")
        ids = [entry.id for entry in self.entries]
        locks = [entry.lock.lock_id for entry in self.entries]
        if len(ids) != len(set(ids)):
            raise CatalogError("catalog contains duplicate entry ids")
        if len(locks) != len(set(locks)):
            raise CatalogError("catalog contains duplicate exact locks")
        entries_by_id = {entry.id: entry for entry in self.entries}
        entries_by_lock = {entry.lock.lock_id: entry for entry in self.entries}
        module_index: dict[str, set[str]] = {}
        package_index: dict[str, set[str]] = {}
        for entry in self.entries:
            for module in entry.modules:
                module_index.setdefault(module, set()).add(entry.id)
            for package in entry.package_names:
                package_index.setdefault(package, set()).add(entry.id)
        object.__setattr__(self, "_entries_by_id", entries_by_id)
        object.__setattr__(self, "_entries_by_lock", entries_by_lock)
        object.__setattr__(
            self,
            "_module_index",
            {name: frozenset(entry_ids) for name, entry_ids in module_index.items()},
        )
        object.__setattr__(
            self,
            "_package_index",
            {name: frozenset(entry_ids) for name, entry_ids in package_index.items()},
        )
        object.__setattr__(
            self,
            "_digest",
            f"sha256:{hashlib.sha256(self.canonical_bytes()).hexdigest()}",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "generated_at": self.generated_at,
            "entries": [
                entry.to_dict() for entry in sorted(self.entries, key=lambda item: item.id)
            ],
        }

    def canonical_bytes(self) -> bytes:
        return (json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")) + "\n").encode()

    def write(self, path: str | Path) -> None:
        Path(path).write_bytes(self.canonical_bytes())

    @property
    def digest(self) -> str:
        return self._digest

    def entry_for_lock(self, lock_id: str) -> CatalogEntry | None:
        return self._entries_by_lock.get(lock_id)

    def entry_ids_for_modules(self, modules: tuple[str, ...]) -> frozenset[str]:
        """Return entries whose declared inventories contain every external module."""

        if not modules:
            return frozenset(self._entries_by_id)
        providers = [self._module_index.get(module, frozenset()) for module in modules]
        if any(not entry_ids for entry_ids in providers):
            return frozenset()
        return frozenset.intersection(*providers)

    def entry_ids_for_packages(self, packages: frozenset[str]) -> frozenset[str]:
        if not packages:
            return frozenset(self._entries_by_id)
        providers = [self._package_index.get(package, frozenset()) for package in packages]
        if any(not entry_ids for entry_ids in providers):
            return frozenset()
        return frozenset.intersection(*providers)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Catalog:
        unknown = set(value) - _TOP_LEVEL_KEYS
        missing = _TOP_LEVEL_KEYS - set(value)
        if unknown or missing:
            raise CatalogError(
                f"catalog fields mismatch; missing={sorted(missing)}, unknown={sorted(unknown)}"
            )
        entries = value["entries"]
        if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
            raise CatalogError("catalog entries must be an array of objects")
        schema = value["schema"]
        if not isinstance(schema, str):
            raise CatalogError("catalog schema must be a string")
        return cls(
            schema=schema,
            generated_at=_timestamp(value["generated_at"], "generated_at"),
            entries=tuple(CatalogEntry.from_dict(item) for item in entries),
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> Catalog:
        if len(payload) > MAX_CATALOG_BYTES:
            raise CatalogError("catalog exceeds the maximum supported size")
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CatalogError("catalog is not valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise CatalogError("catalog must contain a JSON object")
        return cls.from_dict(value)

    @classmethod
    def from_file(cls, path: str | Path) -> Catalog:
        try:
            payload = Path(path).read_bytes()
        except OSError as exc:
            raise CatalogError(f"could not read catalog: {path}") from exc
        return cls.from_bytes(payload)

    @classmethod
    def from_url(cls, url: str, *, timeout: float = 15.0) -> Catalog:
        try:
            with urlopen(url, timeout=timeout) as response:  # noqa: S310 - caller selects URL
                payload = response.read(MAX_CATALOG_BYTES + 1)
        except (OSError, URLError) as exc:
            raise CatalogError(f"could not download catalog: {url}") from exc
        return cls.from_bytes(payload)
