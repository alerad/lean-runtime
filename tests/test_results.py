from __future__ import annotations

from pathlib import Path

import pytest

from lean_runtime._results import after_preparation, combine_invocations, from_backend
from lean_runtime.backends import BackendResult
from lean_runtime.console import Styler, verdict_line
from lean_runtime.events import EventEmitter
from lean_runtime.profiling import run_profile


def _result(raw: BackendResult, command: tuple[str, ...] = ("lean",)):
    return from_backend(raw, command=command, cwd=Path("."), toolchain="t", provenance=None)


def _raw(exit_code: int = 0, **overrides: object) -> BackendResult:
    values: dict[str, object] = {
        "exit_code": exit_code,
        "stdout": "",
        "stderr": "",
        "elapsed_seconds": 0.5,
        "timed_out": False,
        "cancelled": False,
        "output_truncated": False,
        "enforced_policy_fields": (),
    }
    values.update(overrides)
    return BackendResult(**values)  # type: ignore[arg-type]


def test_signal_death_is_a_crash_not_a_rejection(tmp_path: Path) -> None:
    result = _result(_raw(-9), ("lean",))
    assert result.verdict == "crashed" and not result.ok
    assert any("signal 9" in item.message for item in result.diagnostics)
    rejected = _result(_raw(1), ("lean",))
    assert rejected.verdict == "rejected"
    cut = _result(_raw(124, timed_out=True), ("lean",))
    assert cut.verdict == "not_run"


@pytest.mark.parametrize("exit_code", [-11, 0xC0000005])
def test_console_identifies_signal_and_windows_crashes(exit_code: int) -> None:
    result = _result(_raw(exit_code))
    line = verdict_line(result, style=Styler(False))
    assert "crashed (no verdict)" in line
    assert "cancelled" not in line


def test_combined_invocations_keep_every_transcript_and_interruption() -> None:
    first = _raw(0, stdout="a", elapsed_seconds=1.0)
    second = _raw(0, stderr="b", elapsed_seconds=2.0, output_truncated=True)
    final = _raw(1, stdout="c", elapsed_seconds=0.5)
    combined = combine_invocations([first, second], final)
    assert (combined.stdout, combined.stderr) == ("ac", "b")
    assert combined.elapsed_seconds == 3.5
    assert combined.exit_code == 1 and combined.output_truncated
    assert combine_invocations([], final) is final


def test_preparation_phases_are_exclusive_and_do_not_change_the_verdict(tmp_path: Path) -> None:
    attempt = from_backend(
        _raw(1, elapsed_seconds=1.0),
        command=("lean",),
        cwd=tmp_path,
        toolchain="t",
        provenance=None,
    )
    build = from_backend(
        _raw(0, elapsed_seconds=2.0),
        command=("lake",),
        cwd=tmp_path,
        toolchain="t",
        provenance=None,
    )
    retry = from_backend(
        _raw(0, elapsed_seconds=0.25),
        command=("lean",),
        cwd=tmp_path,
        toolchain="t",
        provenance=None,
    )
    result = after_preparation(
        retry, preparation=(("preliminary_check", attempt), ("build", build))
    )
    assert result.ok and result.command == ("lean",)
    assert [t.phase for t in result.timings] == ["preliminary_check", "build", "execution"]
    assert [t.duration_ms for t in result.timings] == [1000, 2000, 250]
    assert result.elapsed_seconds == 3.25


def test_profile_reports_the_failing_warmup() -> None:
    class Environment:
        def check(self, source: str, *, filename: str = "Main.lean"):
            return from_backend(
                _raw(1, stderr="boom"),
                command=("lean",),
                cwd=Path("."),
                toolchain="t",
                provenance=None,
            )

    report = run_profile(Environment(), "x", filename="Main.lean", warmup=1, repeat=3)
    assert not report.ok and len(report.results) == 1
    assert report.results[0].stderr == "boom"


def test_observer_failures_never_reach_the_operation() -> None:
    def explode(_event: object) -> None:
        raise RuntimeError("renderer bug")

    emitter = EventEmitter(explode)
    emitter.emit("a", "first")
    emitter.emit("b", "second")
    assert emitter.observer_failures == 2
    assert isinstance(emitter.first_observer_failure, RuntimeError)
