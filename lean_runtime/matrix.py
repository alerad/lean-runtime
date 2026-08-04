"""Bounded execution of one Lean input across exact contexts."""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised by the Python 3.10 CI job
    import tomli as tomllib

from .errors import SpecificationError
from .lockfiles import EnvironmentLock
from .models import ExecutionResult

_SELECTORS = ("requires", "lock", "environment", "toolchain", "project")
_KEYS = {"name", *_SELECTORS}


@dataclass(frozen=True, slots=True)
class MatrixContext:
    name: str
    requires: tuple[str, ...] = ()
    lock: str | None = None
    environment: str | None = None
    toolchain: str | None = None
    project: str | None = None


@dataclass(frozen=True, slots=True)
class MatrixEntry:
    context: str
    result: ExecutionResult

    def to_dict(self) -> dict[str, Any]:
        return {"context": self.context, "result": self.result.to_dict()}


@dataclass(frozen=True, slots=True)
class MatrixResult:
    entries: tuple[MatrixEntry, ...]
    duration_seconds: float

    @property
    def ok(self) -> bool:
        return bool(self.entries) and all(item.result.ok for item in self.entries)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "duration_ms": round(self.duration_seconds * 1000),
            "entries": [item.to_dict() for item in self.entries],
        }


def load_matrix(path: Path) -> tuple[MatrixContext, ...]:
    value = tomllib.loads(path.read_text(encoding="utf-8"))
    rows = value.get("context")
    if not isinstance(rows, list) or not rows:
        raise SpecificationError("matrix must contain at least one [[context]]")
    contexts: list[MatrixContext] = []
    names: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) - _KEYS:
            raise SpecificationError("matrix context contains unknown fields")
        name = row.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise SpecificationError("matrix context names must be non-empty and unique")
        selected = [key for key in _SELECTORS if row.get(key) not in (None, [])]
        # toolchain may qualify requires; otherwise exactly one selector is required.
        if "requires" in selected and "toolchain" in selected:
            selected.remove("toolchain")
        if len(selected) != 1:
            raise SpecificationError("each matrix context requires exactly one context selector")
        requires = row.get("requires", [])
        if not isinstance(requires, list) or not all(isinstance(item, str) for item in requires):
            raise SpecificationError("matrix requires must be an array of strings")
        for key in ("lock", "environment", "toolchain", "project"):
            if row.get(key) is not None and not isinstance(row[key], str):
                raise SpecificationError(f"matrix {key} must be a string")
        names.add(name)
        contexts.append(
            MatrixContext(
                name=name,
                requires=tuple(requires),
                lock=row.get("lock"),
                environment=row.get("environment"),
                toolchain=row.get("toolchain"),
                project=row.get("project"),
            )
        )
    return tuple(contexts)


def run_matrix(
    runtime: Any,
    source: str,
    *,
    filename: str,
    contexts: tuple[MatrixContext, ...],
    base: Path,
    concurrency: int,
) -> MatrixResult:
    if concurrency < 1 or concurrency > 32:
        raise SpecificationError("matrix concurrency must be between 1 and 32")

    def execute(context: MatrixContext) -> MatrixEntry:
        if context.requires:
            environment = runtime.ensure_references(context.requires, toolchain=context.toolchain)
            result = environment.check(source, filename=filename)
        elif context.lock is not None:
            environment = runtime.ensure(EnvironmentLock.load(base / context.lock))
            result = environment.check(source, filename=filename)
        elif context.environment is not None:
            result = runtime.open(context.environment).check(source, filename=filename)
        elif context.project is not None:
            result = runtime.check(
                source,
                project=base / context.project,
                filename=filename,
                toolchain=context.toolchain,
            )
        else:
            assert context.toolchain is not None
            result = runtime.check(source, toolchain=context.toolchain, filename=filename)
        return MatrixEntry(context.name, result)

    started = time.monotonic()
    if concurrency == 1:
        entries = tuple(execute(item) for item in contexts)
    else:
        by_name: dict[str, MatrixEntry] = {}
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="lean-matrix") as pool:
            futures = {pool.submit(execute, item): item.name for item in contexts}
            try:
                for future in as_completed(futures):
                    entry = future.result()
                    by_name[entry.context] = entry
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
        entries = tuple(by_name[item.name] for item in contexts)
    return MatrixResult(entries, time.monotonic() - started)
