from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from lean_runtime import ExecutionJob, Runtime, ToolchainError


class FakeToolchains:
    def __init__(self, home: Path) -> None:
        self.home = home

    @property
    def environment(self) -> dict[str, str]:
        return os.environ.copy()

    def ensure(self, toolchain: str) -> str:
        return toolchain

    def command(self, toolchain: str, executable: str, *args: str) -> list[str]:
        if executable == "lean":
            source = args[-1]
            script = (
                "import pathlib,sys; "
                "text=pathlib.Path(sys.argv[1]).read_text(); "
                "bad='BAD' in text; "
                "print(f'{sys.argv[1]}:1:1: error: rejected') if bad else None; "
                "raise SystemExit(1 if bad else 0)"
            )
            return [sys.executable, "-c", script, source]
        return [sys.executable, "-c", "raise SystemExit(0)"]


def test_check_accepts_source(tmp_path: Path) -> None:
    runtime = Runtime(toolchains=FakeToolchains(tmp_path))  # type: ignore[arg-type]
    result = runtime.check("example : True := by trivial", toolchain="4.32.0")
    assert result.ok
    assert result.exit_code == 0
    assert result.toolchain == "leanprover/lean4:v4.32.0"


def test_repeated_requests_have_unique_execution_history_ids(tmp_path: Path) -> None:
    runtime = Runtime(toolchains=FakeToolchains(tmp_path))  # type: ignore[arg-type]
    first = runtime.check("example : True := by trivial", toolchain="4.32.0")
    second = runtime.check("example : True := by trivial", toolchain="4.32.0")
    assert first.execution_id != second.execution_id
    assert first.provenance is not None and second.provenance is not None
    assert first.provenance.request_digest == second.provenance.request_digest


def test_finished_job_cannot_be_cancelled() -> None:
    job = ExecutionJob(lambda _cancel: 42)
    assert job.result() == 42
    assert not job.cancel()


def test_check_returns_structured_rejection(tmp_path: Path) -> None:
    runtime = Runtime(toolchains=FakeToolchains(tmp_path))  # type: ignore[arg-type]
    result = runtime.check("BAD", toolchain="4.32.0")
    assert not result.ok
    assert result.exit_code == 1
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0].message == "rejected"


def test_check_requires_version_without_project(tmp_path: Path) -> None:
    runtime = Runtime(toolchains=FakeToolchains(tmp_path))  # type: ignore[arg-type]
    with pytest.raises(ToolchainError):
        runtime.check("example : True := by trivial")


def test_build_infers_project_toolchain(tmp_path: Path) -> None:
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.32.0\n")
    (tmp_path / "lakefile.toml").write_text('name = "sample"\n')
    runtime = Runtime(toolchains=FakeToolchains(tmp_path / "cache"))  # type: ignore[arg-type]
    result = runtime.build(tmp_path, targets=["Example"])
    assert result.ok
    assert result.toolchain == "leanprover/lean4:v4.32.0"
