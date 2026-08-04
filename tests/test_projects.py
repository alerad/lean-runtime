from __future__ import annotations

import asyncio
import os
import sys
import threading
from pathlib import Path

import pytest

from lean_runtime import ProjectError, Runtime, discover_project


class ProjectToolchains:
    def __init__(self, home: Path) -> None:
        self.home = home

    @property
    def environment(self) -> dict[str, str]:
        return os.environ.copy()

    def command(self, _toolchain: str, executable: str, *args: str) -> list[str]:
        if executable == "lake" and args[:2] == ("env", "lean"):
            script = (
                "import pathlib,sys,time; "
                "source=pathlib.Path(sys.argv[1]); "
                "text=source.read_text(); "
                "time.sleep(10) if 'SLOW' in text else None; "
                "raise SystemExit(1 if 'BAD' in text else 0)"
            )
            return [sys.executable, "-c", script, args[-1]]
        return [sys.executable, "-c", "raise SystemExit(0)"]


def _project(root: Path, *, name: str = "sample") -> Path:
    root.mkdir(parents=True)
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.32.0\n")
    (root / "lakefile.toml").write_text(f'name = "{name}"\n')
    source = root / "Sample" / "Main.lean"
    source.parent.mkdir()
    source.write_text("example : True := by trivial\n")
    return source


def test_discover_project_walks_up_from_a_lean_file(tmp_path: Path) -> None:
    source = _project(tmp_path / "project")
    context = discover_project(source)
    assert context.root == tmp_path / "project"
    assert context.toolchain == "leanprover/lean4:v4.32.0"
    assert context.lakefile.name == "lakefile.toml"
    assert context.manifest is None


def test_discover_project_selects_the_nearest_nested_project(tmp_path: Path) -> None:
    _project(tmp_path / "outer", name="outer")
    nested = _project(tmp_path / "outer" / "vendor" / "inner", name="inner")
    assert discover_project(nested).root == nested.parents[1]


def test_discover_project_requires_lakefile_and_toolchain(tmp_path: Path) -> None:
    source = tmp_path / "Main.lean"
    source.write_text("example : True := by trivial\n")
    with pytest.raises(ProjectError, match="no pinned Lake project"):
        discover_project(source)


def test_project_environment_checks_actual_relative_file_and_records_provenance(
    tmp_path: Path,
) -> None:
    source = _project(tmp_path / "project")
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        caches=[],  # type: ignore[arg-type]
    )
    project = runtime.project(source)
    result = project.check_file(source)
    assert result.ok
    assert result.cwd == str(project.root)
    assert result.command[-1] == "Sample/Main.lean"
    assert result.environment_id is None
    assert result.provenance is not None
    assert result.provenance.project is not None
    assert result.provenance.project.root == str(project.root)
    assert result.provenance.project.workspace_digest.startswith("sha256:")
    assert result.provenance.project.lakefile_digest.startswith("sha256:")


def test_runtime_check_file_discovers_project_and_source_checks_are_disposable(
    tmp_path: Path,
) -> None:
    source = _project(tmp_path / "project")
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        caches=[],  # type: ignore[arg-type]
    )
    assert runtime.check_file(source).ok
    project = runtime.project(source)
    assert project.check("example : True := by trivial", filename="Scratch.lean").ok
    jobs = project.root / ".lake" / "lean-runtime"
    assert not list(jobs.glob("check-*"))


def test_project_request_identity_changes_with_local_workspace(tmp_path: Path) -> None:
    source = _project(tmp_path / "project")
    dependency = source.parent / "Dependency.lean"
    dependency.write_text("def value := 1\n")
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        caches=[],  # type: ignore[arg-type]
    )
    first = runtime.check_file(source)
    dependency.write_text("def value := 2\n")
    second = runtime.check_file(source)
    assert first.provenance is not None and second.provenance is not None
    assert first.provenance.request_digest != second.provenance.request_digest


def test_project_environment_rejects_files_outside_its_root(tmp_path: Path) -> None:
    source = _project(tmp_path / "project")
    outside = tmp_path / "Outside.lean"
    outside.write_text("example : True := by trivial\n")
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        caches=[],  # type: ignore[arg-type]
    )
    with pytest.raises(ProjectError, match="outside the project root"):
        runtime.project(source).check_file(outside)


def test_project_check_propagates_cancellation_to_the_active_process(tmp_path: Path) -> None:
    source = _project(tmp_path / "project")
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        caches=[],  # type: ignore[arg-type]
    )
    cancel = threading.Event()
    timer = threading.Timer(0.1, cancel.set)
    timer.start()
    try:
        result = runtime.project(source).check("-- SLOW", cancel=cancel)
    finally:
        timer.cancel()
    assert result.cancelled
    assert result.exit_code == 130
    assert any("cancelled" in item.message for item in result.diagnostics)


def test_project_async_cancellation_waits_for_process_cleanup(tmp_path: Path) -> None:
    source = _project(tmp_path / "project")
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        caches=[],  # type: ignore[arg-type]
    )

    async def cancel_check() -> None:
        task = asyncio.create_task(runtime.project(source).check_async("-- SLOW"))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_check())
