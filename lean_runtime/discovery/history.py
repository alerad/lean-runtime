"""Bounded compiler-backed discovery history."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, cast

from ..locking import FileLock
from ..serialization import write_json_atomic
from .analyzer import SourceEvidence

_SCHEMA = "lean-runtime-discovery-history/1"
_MAX_SOURCES = 512
_MAX_HEADERS = 256


@dataclass(frozen=True, slots=True)
class DecisionHint:
    lock_id: str
    exact_source: bool


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _source_key(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()


def _header_key(evidence: SourceEvidence) -> str:
    return _digest(
        {
            "imports": evidence.imports,
            "packages": evidence.package_hints,
            "toolchain": evidence.toolchain_hint,
        }
    )


def _empty() -> dict[str, Any]:
    return {"schema": _SCHEMA, "sources": {}, "headers": {}}


def _trim(records: dict[str, Any], limit: int) -> dict[str, Any]:
    ordered = sorted(
        records.items(),
        key=lambda item: float(item[1].get("last_used_at", 0)) if isinstance(item[1], dict) else 0,
        reverse=True,
    )
    return dict(ordered[:limit])


class DiscoveryHistory:
    """Persist only exact compiler outcomes and successful context hints."""

    def __init__(self, home: Path) -> None:
        self.path = home / "discovery" / "history.json"
        self.lock_path = home / "discovery" / "history.lock"

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("schema") != _SCHEMA:
                return _empty()
            if not isinstance(value.get("sources"), dict) or not isinstance(
                value.get("headers"), dict
            ):
                return _empty()
            return cast(dict[str, Any], value)
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            return _empty()

    def lookup(
        self,
        source: str,
        evidence: SourceEvidence,
        *,
        allowed_lock_ids: frozenset[str] | None = None,
    ) -> DecisionHint | None:
        value = self._load()
        exact = value["sources"].get(_source_key(source))
        if isinstance(exact, dict):
            lock_id = exact.get("accepted")
            if isinstance(lock_id, str) and (
                allowed_lock_ids is None or lock_id in allowed_lock_ids
            ):
                return DecisionHint(lock_id, True)
        header = value["headers"].get(_header_key(evidence))
        if isinstance(header, dict):
            lock_id = header.get("accepted")
            if isinstance(lock_id, str) and (
                allowed_lock_ids is None or lock_id in allowed_lock_ids
            ):
                return DecisionHint(lock_id, False)
        return None

    def rejected_locks(self, source: str) -> frozenset[str]:
        record = self._load()["sources"].get(_source_key(source))
        if not isinstance(record, dict):
            return frozenset()
        rejected = record.get("rejected")
        if not isinstance(rejected, list):
            return frozenset()
        return frozenset(item for item in rejected if isinstance(item, str))

    def remember_success(
        self,
        source: str,
        evidence: SourceEvidence,
        lock_id: str,
    ) -> None:
        now = time.time()
        with self._edit() as value:
            source_record = value["sources"].setdefault(_source_key(source), {})
            source_record.update({"accepted": lock_id, "last_used_at": now})
            rejected = source_record.get("rejected")
            if isinstance(rejected, list) and lock_id in rejected:
                source_record["rejected"] = [item for item in rejected if item != lock_id]
            value["headers"][_header_key(evidence)] = {
                "accepted": lock_id,
                "last_used_at": now,
            }

    def remember_rejection(self, source: str, lock_id: str) -> None:
        self.remember_rejections(source, (lock_id,))

    def remember_rejections(self, source: str, lock_ids: tuple[str, ...]) -> None:
        if not lock_ids:
            return
        now = time.time()
        with self._edit() as value:
            record = value["sources"].setdefault(_source_key(source), {})
            rejected = record.setdefault("rejected", [])
            if isinstance(rejected, list):
                for lock_id in lock_ids:
                    if lock_id not in rejected:
                        rejected.append(lock_id)
            record["last_used_at"] = now

    class _Editor:
        def __init__(self, history: DiscoveryHistory) -> None:
            self.history = history
            self.value: dict[str, Any] = {}
            self.lock: FileLock | None = None

        def __enter__(self) -> dict[str, Any]:
            self.history.path.parent.mkdir(parents=True, exist_ok=True)
            self.lock = FileLock(self.history.lock_path, timeout=30)
            self.lock.__enter__()
            self.value = self.history._load()
            return self.value

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            try:
                if exc_type is None:
                    self.value["sources"] = _trim(self.value["sources"], _MAX_SOURCES)
                    self.value["headers"] = _trim(self.value["headers"], _MAX_HEADERS)
                    write_json_atomic(self.history.path, self.value)
            finally:
                assert self.lock is not None
                self.lock.__exit__(exc_type, exc, traceback)

    def _edit(self) -> DiscoveryHistory._Editor:
        return self._Editor(self)
