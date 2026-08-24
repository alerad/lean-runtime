"""The first dependency materialization of a project must be deliberate and serial."""

from __future__ import annotations

import json
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from lean_runtime import ProjectError
from lean_runtime.locking import FileLock
from lean_runtime.project_execution import ProjectExecutor
from lean_runtime.projects import discover_project


def _executor() -> ProjectExecutor:
    return ProjectExecutor(SimpleNamespace())  # type: ignore[arg-type]


def _pin(root: Path, lakefile: str = 'name = "fixture"\n') -> None:
    (root / "lakefile.toml").write_text(lakefile)
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n")


def _manifest(root: Path, *names: str) -> None:
    (root / "lake-manifest.json").write_text(
        json.dumps(
            {
                "version": "1.1.0",
                "packagesDir": ".lake/packages",
                "packages": [{"name": name} for name in names],
            }
        )
    )


def test_missing_manifest_with_dependencies_fails_fast(tmp_path: Path) -> None:
    _pin(
        tmp_path,
        'name = "fixture"\n\n[[require]]\nname = "mathlib"\nscope = "leanprover-community"\n',
    )
    context = discover_project(tmp_path)
    with pytest.raises(ProjectError, match="lake update"):
        _executor()._bootstrap_guard(context)


def test_missing_manifest_with_lean_lakefile_requires_fails_fast(tmp_path: Path) -> None:
    (tmp_path / "lakefile.lean").write_text(
        'import Lake\nopen Lake DSL\n\npackage fixture\n\nrequire mathlib from git\n  "https://github.com/leanprover-community/mathlib4"\n'
    )
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n")
    context = discover_project(tmp_path)
    with pytest.raises(ProjectError, match="lake update"):
        _executor()._bootstrap_guard(context)


def test_missing_manifest_without_dependencies_serializes_creation(tmp_path: Path) -> None:
    _pin(tmp_path)
    context = discover_project(tmp_path)
    guard = _executor()._bootstrap_guard(context)
    assert isinstance(guard, FileLock)


def test_materialized_packages_need_no_lock(tmp_path: Path) -> None:
    _pin(tmp_path)
    _manifest(tmp_path, "mathlib")
    (tmp_path / ".lake" / "packages" / "mathlib").mkdir(parents=True)
    context = discover_project(tmp_path)
    guard = _executor()._bootstrap_guard(context)
    assert isinstance(guard, nullcontext)


def test_unmaterialized_packages_serialize_behind_one_lock(tmp_path: Path) -> None:
    _pin(tmp_path)
    _manifest(tmp_path, "mathlib", "batteries")
    context = discover_project(tmp_path)
    executor = _executor()
    active = []
    overlaps = []

    def bootstrap() -> None:
        with executor._bootstrap_guard(context):
            active.append(threading.current_thread().name)
            time.sleep(0.1)
            if len(active) > 1:
                overlaps.append(tuple(active))
            active.remove(threading.current_thread().name)

    workers = [threading.Thread(target=bootstrap) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
    assert not overlaps, "bootstrap must never run concurrently for one project root"


def test_corrupt_manifest_defers_to_lake(tmp_path: Path) -> None:
    _pin(tmp_path)
    (tmp_path / "lake-manifest.json").write_text("{not json")
    context = discover_project(tmp_path)
    guard = _executor()._bootstrap_guard(context)
    assert isinstance(guard, nullcontext)
