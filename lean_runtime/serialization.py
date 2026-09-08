"""Canonical serialization and digest helpers."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any


def canonical_json_bytes(value: Any) -> bytes:
    """Encode JSON data with a stable, whitespace-free representation."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_id(prefix: str, value: Any) -> str:
    """Return a namespaced SHA-256 identity for canonical JSON data."""
    digest = hashlib.sha256(canonical_json_bytes(value)).hexdigest()
    return f"{prefix}_{digest}"


def sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    """Stream a file into a ``sha256:`` digest without loading it whole."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _rejected(*_args: Any, **_kwargs: Any) -> Any:
    raise TypeError(
        "identity-bearing JSON data is immutable; serialize with to_dict() and edit the copy"
    )


class FrozenDict(dict):  # type: ignore[type-arg]
    """A ``dict`` that refuses mutation after construction.

    It remains a ``dict`` for ``json``, equality and ``isinstance`` checks so
    consumers keep working, but the identity of the object holding it cannot
    drift after its digest was computed. Copies are ordinary mutable data.
    """

    __slots__ = ()
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _rejected
    __ior__ = _rejected

    def __copy__(self) -> dict[Any, Any]:
        return {key: thaw_json(item) for key, item in self.items()}

    def __deepcopy__(self, memo: dict[int, Any]) -> dict[Any, Any]:
        return self.__copy__()

    def __reduce__(self) -> tuple[Any, ...]:
        return (dict, (thaw_json(self),))


class FrozenList(list):  # type: ignore[type-arg]
    """A ``list`` that refuses mutation after construction; see :class:`FrozenDict`."""

    __slots__ = ()
    __setitem__ = __delitem__ = append = extend = insert = pop = remove = clear = _rejected
    sort = reverse = __iadd__ = __imul__ = _rejected

    def __copy__(self) -> list[Any]:
        return [thaw_json(item) for item in self]

    def __deepcopy__(self, memo: dict[int, Any]) -> list[Any]:
        return self.__copy__()

    def __reduce__(self) -> tuple[Any, ...]:
        return (list, (thaw_json(self),))


def freeze_json(value: Any) -> Any:
    """Return ``value`` with every nested dict and list made immutable."""
    if isinstance(value, dict):
        return FrozenDict((key, freeze_json(item)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return FrozenList(freeze_json(item) for item in value)
    return value


def thaw_json(value: Any) -> Any:
    """Return a detached, ordinary, mutable copy of JSON-shaped data."""
    if isinstance(value, dict):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_json(item) for item in value]
    return value


def write_json_atomic(path: Path, value: Any) -> None:
    """Atomically publish formatted JSON next to its destination."""
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    write_bytes_atomic(path, payload.encode("utf-8"))


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Atomically publish ``data`` at ``path`` via a same-directory temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
