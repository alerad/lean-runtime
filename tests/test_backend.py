from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

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
