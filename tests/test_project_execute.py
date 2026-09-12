from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lean_runtime.backends import LocalBackend
from lean_runtime.policies import ExecutionPolicy
from lean_runtime.project_execution import ProjectExecutor
from lean_runtime.projects import ProjectEnvironment, discover_project


def test_project_execute_uses_pinned_toolchain_and_persistent_root(tmp_path: Path) -> None:
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n")
    (tmp_path / "lakefile.toml").write_text('name="fixture"\n')
    (tmp_path / "lake-manifest.json").write_text('{"packages":[]}')
    runtime = SimpleNamespace(
        toolchains=SimpleNamespace(
            ensure_full=Mock(),
            command=Mock(return_value=["/pinned/lake", "env", "extract"]),
            environment_for=Mock(return_value={"PATH": "/pinned"}),
        ),
        events=SimpleNamespace(emit=Mock()),
        _raw_result=Mock(return_value="result"),
    )
    runtime.project_executor = ProjectExecutor(runtime)
    project = ProjectEnvironment(runtime, discover_project(tmp_path))
    callback = Mock()
    cancel = threading.Event()
    assert project.execute(["lake", "env", "extract"], cancel=cancel, on_bytes=callback) == "result"
    kwargs = runtime._raw_result.call_args.kwargs
    assert kwargs["cwd"] == tmp_path
    assert kwargs["toolchain"] == "leanprover/lean4:v4.32.2"
    assert kwargs["logical_command"] == ["lake", "env", "extract"]
    assert kwargs["cancel"] is cancel and kwargs["on_bytes"] is callback
    assert kwargs["project"].root == str(tmp_path)


def test_raw_byte_callback_is_exact_and_bounded(tmp_path: Path) -> None:
    output = {"stdout": b"", "stderr": b""}

    def receive(name: str, data: bytes) -> None:
        output[name] += data

    result = LocalBackend().execute(
        [sys.executable, "-c", 'import os; os.write(1,b"a\\xff\\n"); os.write(2,b"err")'],
        cwd=tmp_path,
        environment=os.environ,
        policy=ExecutionPolicy(),
        on_bytes=receive,
    )
    assert result.exit_code == 0
    assert output == {"stdout": b"a\xff\n", "stderr": b"err"}
    output = {"stdout": b"", "stderr": b""}
    result = LocalBackend().execute(
        [sys.executable, "-c", 'print("x"*10000)'],
        cwd=tmp_path,
        environment=os.environ,
        policy=ExecutionPolicy(max_output_bytes=100),
        on_bytes=receive,
    )
    assert result.output_truncated
    assert sum(map(len, output.values())) == 100


def test_raw_byte_sink_failure_stops_process_and_propagates(tmp_path: Path) -> None:
    def fail(name: str, data: bytes) -> None:
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        LocalBackend().execute(
            [sys.executable, "-c", 'import time; print("hello",flush=True); time.sleep(10)'],
            cwd=tmp_path,
            environment=os.environ,
            policy=ExecutionPolicy(timeout_seconds=5),
            on_bytes=fail,
        )
