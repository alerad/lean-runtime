"""Strict source manifests used to build discovery catalogs."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised by the Python 3.10 CI job
    import tomli as tomllib

from .errors import CatalogError

CATALOG_SOURCE_SCHEMA = "lean-runtime.discovery.catalog-source/v1"
_TOP_LEVEL_KEYS = frozenset({"schema", "generated_at", "environment"})
_ENTRY_KEYS = frozenset(
    {
        "id",
        "channel",
        "lock",
        "references",
        "inventory_packages",
        "modules",
        # Accepted for compatibility with existing source manifests, but ignored.
        "library_hints",
        "created_at",
    }
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


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
class CatalogSourceEntry:
    """One exact environment requested by a catalog source manifest."""

    id: str
    channel: str
    lock_path: Path
    references: tuple[str, ...]
    inventory_packages: tuple[str, ...]
    modules: tuple[str, ...]
    created_at: str

    @classmethod
    def from_dict(cls, value: dict[str, Any], *, base: Path) -> CatalogSourceEntry:
        unknown = set(value) - _ENTRY_KEYS
        missing = {"id", "channel", "lock", "inventory_packages", "created_at"} - set(value)
        if unknown or missing:
            raise CatalogError(
                "catalog source entry fields mismatch; "
                f"missing={sorted(missing)}, unknown={sorted(unknown)}"
            )
        entry_id = value["id"]
        channel = value["channel"]
        lock = value["lock"]
        if not isinstance(entry_id, str) or _IDENTIFIER.fullmatch(entry_id) is None:
            raise CatalogError(f"invalid catalog source entry id: {entry_id!r}")
        if not isinstance(channel, str) or _IDENTIFIER.fullmatch(channel) is None:
            raise CatalogError(f"invalid catalog source channel: {channel!r}")
        if not isinstance(lock, str) or not lock or "\x00" in lock:
            raise CatalogError(f"entry {entry_id!r} lock must be a non-empty path")
        references = _strings(value.get("references", []), f"entry {entry_id!r} references")
        inventory_packages = _strings(
            value["inventory_packages"], f"entry {entry_id!r} inventory_packages"
        )
        modules = _strings(value.get("modules", []), f"entry {entry_id!r} modules")
        if not inventory_packages and not modules:
            raise CatalogError(
                f"entry {entry_id!r} must inventory packages or declare explicit modules"
            )
        path = Path(lock)
        return cls(
            id=entry_id,
            channel=channel,
            lock_path=(base / path).resolve() if not path.is_absolute() else path.resolve(),
            references=references,
            inventory_packages=inventory_packages,
            modules=modules,
            created_at=_timestamp(value["created_at"], f"entry {entry_id!r} created_at"),
        )


@dataclass(frozen=True, slots=True)
class CatalogSourceManifest:
    """Closed, versioned input to deterministic catalog generation."""

    generated_at: str
    entries: tuple[CatalogSourceEntry, ...]
    schema: str = CATALOG_SOURCE_SCHEMA

    @classmethod
    def from_dict(cls, value: dict[str, Any], *, base: Path) -> CatalogSourceManifest:
        unknown = set(value) - _TOP_LEVEL_KEYS
        missing = _TOP_LEVEL_KEYS - set(value)
        if unknown or missing:
            raise CatalogError(
                "catalog source fields mismatch; "
                f"missing={sorted(missing)}, unknown={sorted(unknown)}"
            )
        if value["schema"] != CATALOG_SOURCE_SCHEMA:
            raise CatalogError(f"unsupported catalog source schema: {value['schema']!r}")
        environments = value["environment"]
        if not isinstance(environments, list) or not all(
            isinstance(item, dict) for item in environments
        ):
            raise CatalogError("catalog source environment must be an array of tables")
        entries = tuple(CatalogSourceEntry.from_dict(item, base=base) for item in environments)
        if not entries:
            raise CatalogError("catalog source must contain at least one environment")
        ids = [entry.id for entry in entries]
        if len(ids) != len(set(ids)):
            raise CatalogError("catalog source contains duplicate entry ids")
        return cls(
            schema=CATALOG_SOURCE_SCHEMA,
            generated_at=_timestamp(value["generated_at"], "generated_at"),
            entries=entries,
        )

    @classmethod
    def from_file(cls, path: str | Path) -> CatalogSourceManifest:
        source = Path(path).expanduser().resolve()
        try:
            with source.open("rb") as stream:
                value = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise CatalogError(f"could not read catalog source manifest: {source}") from exc
        if not isinstance(value, dict):
            raise CatalogError("catalog source manifest must contain a TOML table")
        return cls.from_dict(value, base=source.parent)
