from __future__ import annotations

import os
import signal
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path

import pytest

from lean_runtime._process import ResourceLimits, run_git, run_process

PY = sys.executable


def test_captures_bounded_output_and_reports_truncation() -> None:
    outcome = run_process(
        [PY, "-c", "import sys; sys.stdout.write('x' * 300000); sys.stderr.write('e')"],
        max_output_bytes=100_000,
        timeout=30,
    )
    assert outcome.ok
    assert outcome.output_truncated
    assert len(outcome.stdout) + len(outcome.stderr) == 100_000


def test_observer_sees_lines_and_partial_lines_are_bounded() -> None:
    seen: list[str] = []
    outcome = run_process(
        [
            PY,
            "-c",
            "import sys; sys.stdout.write('a\\nb\\r'); sys.stdout.write('y' * 200000);"
            " sys.stdout.flush()",
        ],
        on_output=seen.append,
        timeout=30,
    )
    assert outcome.ok
    assert seen[:2] == ["a", "b"]
    assert "".join(seen[2:]) == "y" * 200000
    assert max(len(item) for item in seen) <= 65_536 + 65_536


def test_observer_exceptions_do_not_break_execution() -> None:
    def explode(_: str) -> None:
        raise RuntimeError("observer bug")

    outcome = run_process([PY, "-c", "print('hi')"], on_output=explode, timeout=30)
    assert outcome.ok and outcome.stdout == "hi\n"


def test_timeout_stops_the_process_tree() -> None:
    started = time.monotonic()
    outcome = run_process([PY, "-c", "import time; time.sleep(30)"], timeout=0.3)
    assert outcome.timed_out and not outcome.cancelled
    assert outcome.exit_code == 124
    assert time.monotonic() - started < 10


def test_cancel_event_stops_the_process() -> None:
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    outcome = run_process([PY, "-c", "import time; time.sleep(30)"], cancel=cancel, timeout=30)
    assert outcome.cancelled and outcome.exit_code == 130


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_grandchild_holding_the_pipe_does_not_hang_the_caller() -> None:
    script = (
        "import subprocess, sys;"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        "print('parent done')"
    )
    started = time.monotonic()
    outcome = run_process([PY, "-c", script], timeout=30)
    assert outcome.ok and "parent done" in outcome.stdout
    assert time.monotonic() - started < 20


@pytest.mark.skipif(os.name == "nt", reason="POSIX detached process groups")
@pytest.mark.parametrize("timeout", [None, 0.5])
def test_detached_grandchild_cannot_block_pipe_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout: float | None
) -> None:
    monkeypatch.setattr("lean_runtime._process._READER_GRACE_SECONDS", 0.1)
    pid_file = tmp_path / "child.pid"
    script = (
        "import pathlib, subprocess, sys, time;"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(8)'],"
        " start_new_session=True);"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid));"
        "print('parent ready', flush=True);"
        + ("time.sleep(30)" if timeout is not None else "")
    )
    started = time.monotonic()
    try:
        outcome = run_process([PY, "-c", script, str(pid_file)], timeout=timeout)
        assert time.monotonic() - started < 4
        assert outcome.timed_out is (timeout is not None)
        assert outcome.output_truncated
        assert outcome.stdout == "parent ready\n"
    finally:
        if pid_file.exists():
            with suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)


@pytest.mark.skipif(os.name == "nt", reason="POSIX resource limits")
def test_limits_apply_from_many_threads_at_once() -> None:
    results: list[int] = []

    def worker() -> None:
        outcome = run_process(
            [PY, "-c", "import resource; print(resource.getrlimit(resource.RLIMIT_CPU)[0])"],
            limits=ResourceLimits(cpu_seconds=7),
            timeout=30,
        )
        results.append(int(outcome.stdout.strip()))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [7] * 8


@pytest.mark.skipif(os.name == "nt", reason="signal exit codes are POSIX")
def test_signal_death_is_distinguishable() -> None:
    outcome = run_process([PY, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"])
    assert outcome.signalled and outcome.exit_code == -9


def test_git_helper_reports_failure_without_raising(tmp_path: Path) -> None:
    outcome = run_git("-C", str(tmp_path), "rev-parse", "HEAD", timeout=30)
    assert not outcome.ok and outcome.exit_code != 0
