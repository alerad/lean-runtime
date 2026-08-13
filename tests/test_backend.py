from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from lean_runtime.backends import LocalBackend
from lean_runtime.policies import ExecutionPolicy


def test_output_is_bounded(tmp_path: Path) -> None:
    result = LocalBackend().execute(
        [sys.executable, "-c", "print('x' * 10000)"],
        cwd=tmp_path,
        environment=os.environ,
        policy=ExecutionPolicy(max_output_bytes=100),
    )
    assert result.exit_code == 0
    assert result.output_truncated
    assert len(result.stdout.encode()) + len(result.stderr.encode()) <= 100


def test_timeout_stops_process(tmp_path: Path) -> None:
    result = LocalBackend().execute(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        cwd=tmp_path,
        environment=os.environ,
        policy=ExecutionPolicy(timeout_seconds=0.1),
    )
    assert result.timed_out
    assert result.exit_code == 124


def test_cancellation_stops_process(tmp_path: Path) -> None:
    cancel = threading.Event()
    timer = threading.Timer(0.1, cancel.set)
    timer.start()
    try:
        result = LocalBackend().execute(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            cwd=tmp_path,
            environment=os.environ,
            policy=ExecutionPolicy(timeout_seconds=5),
            cancel=cancel,
        )
    finally:
        timer.cancel()
    assert result.cancelled
    assert result.exit_code == 130
    assert result.elapsed_seconds < 3


def test_caller_interrupt_stops_child_before_propagating(tmp_path: Path) -> None:
    pid_path = tmp_path / "pid"

    class Interrupt:
        def is_set(self) -> bool:
            if pid_path.exists() and pid_path.read_text():
                raise KeyboardInterrupt
            return False

    with pytest.raises(KeyboardInterrupt):
        LocalBackend().execute(
            [
                sys.executable,
                "-c",
                "import os,pathlib,time,sys; "
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(10)",
                str(pid_path),
            ],
            cwd=tmp_path,
            environment=os.environ,
            policy=ExecutionPolicy(timeout_seconds=5),
            cancel=Interrupt(),  # type: ignore[arg-type]
        )
    pid = int(pid_path.read_text())
    time.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
