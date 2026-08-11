import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from lean_runtime import Diagnostic, ExecutionResult, LeanCheckError, SpecificationError
from lean_runtime.facade import check, check_file, setup


def _result(*, ok: bool = True) -> ExecutionResult:
    return ExecutionResult(
        ok=ok,
        exit_code=0 if ok else 1,
        toolchain="leanprover/lean4:v4.32.0",
        command=("lean", "Main.lean"),
        cwd="/tmp",
        stdout="",
        stderr="",
        elapsed_seconds=0.01,
        diagnostics=(Diagnostic("error", "unsolved goals"),) if not ok else (),
    )


class Prepared:
    def check(self, source: str, *, filename: str, policy: object, cancel=None) -> ExecutionResult:
        del source, filename, policy, cancel
        return _result()


class FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def open_references(self, deps, *, toolchain=None, name=None, cancel=None):
        del cancel
        self.calls.append(("deps", (deps, toolchain, name)))
        return Prepared()

    def open_toolchain(self, toolchain, *, name=None, cancel=None):
        del cancel
        self.calls.append(("toolchain", (toolchain, name)))
        return Prepared()

    def project(self, path, *, toolchain=None):
        self.calls.append(("project", (path, toolchain)))
        return SimpleNamespace(check=lambda *_args, **_kwargs: _result())

    def check(self, source, **kwargs):
        self.calls.append(("check", (source, kwargs)))
        return _result()

    def check_file(self, path, **kwargs):
        self.calls.append(("check_file", (path, kwargs)))
        return _result()


def test_setup_routes_dependencies_and_projects() -> None:
    runtime = FakeRuntime()
    setup(["mathlib@v4.32.2"], runtime=runtime)  # type: ignore[arg-type]
    setup(project=".", runtime=runtime)  # type: ignore[arg-type]
    assert runtime.calls[0][0] == "deps"
    assert runtime.calls[1][0] == "project"


def test_setup_accepts_one_dependency_without_a_list() -> None:
    runtime = FakeRuntime()
    setup("mathlib@v4.32.2", runtime=runtime)  # type: ignore[arg-type]
    assert runtime.calls[0][1] == (("mathlib@v4.32.2",), None, None)


def test_setup_requires_exactly_one_context() -> None:
    runtime = FakeRuntime()
    with pytest.raises(SpecificationError, match="exactly one"):
        setup(runtime=runtime)  # type: ignore[arg-type]
    with pytest.raises(SpecificationError, match="exactly one"):
        setup(["mathlib@v1"], project=".", runtime=runtime)  # type: ignore[arg-type]


def test_setup_toolchain_alone_is_a_core_context() -> None:
    runtime = FakeRuntime()
    setup(toolchain="v4.32.2", runtime=runtime)  # type: ignore[arg-type]
    assert runtime.calls == [("toolchain", ("v4.32.2", None))]


def test_setup_empty_deps_error_teaches_the_toolchain_form() -> None:
    runtime = FakeRuntime()
    with pytest.raises(SpecificationError, match="for core Lean"):
        setup([], toolchain="v4.32.2", runtime=runtime)  # type: ignore[arg-type]


def test_top_level_check_and_file_delegate_without_global_runtime(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    assert check("example : True := by trivial", toolchain="4.32.0", runtime=runtime).ok  # type: ignore[arg-type]
    path = tmp_path / "Main.lean"
    path.write_text("example : True := by trivial")
    assert check_file(path, runtime=runtime).ok  # type: ignore[arg-type]
    assert [call[0] for call in runtime.calls] == ["check", "check_file"]


def test_execution_result_raise_for_error() -> None:
    assert _result().raise_for_error().ok
    with pytest.raises(LeanCheckError, match="unsolved goals") as failure:
        _result(ok=False).raise_for_error()
    assert failure.value.result.exit_code == 1


def test_importing_package_does_not_initialize_default_runtime(tmp_path: Path) -> None:
    home = tmp_path / "not-created"
    environment = os.environ.copy()
    environment["LEAN_RUNTIME_HOME"] = str(home)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import lean_runtime, pathlib, sys; "
            "raise SystemExit(pathlib.Path(sys.argv[1]).exists())",
            str(home),
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
