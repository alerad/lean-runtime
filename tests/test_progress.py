from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lean_runtime.backends import BackendResult, LocalBackend
from lean_runtime.console import ConsoleRenderer
from lean_runtime.events import EventEmitter, RuntimeEvent
from lean_runtime.policies import ExecutionPolicy
from lean_runtime.progress import ObservedProcess, OutputProgress
from lean_runtime.project_sharing import plan_adoption
from lean_runtime.runtime import Runtime
from lean_runtime.shared_projects import SharedProjectManager


def _collect() -> tuple[list[RuntimeEvent], EventEmitter]:
    events: list[RuntimeEvent] = []
    return events, EventEmitter(events.append)


# -- OutputProgress ------------------------------------------------------------


def test_lake_step_lines_become_progress_events_with_rate_limiting() -> None:
    events, emitter = _collect()
    clock = [0.0]
    progress = OutputProgress(emitter.emit, label="lake", clock=lambda: clock[0])
    progress.line("✔ [1/4] Built Demo.Basic (481ms)")
    progress.line("\x1b[32m✔\x1b[0m [2/4] Built Demo.Other (12ms)")  # within 0.1s: held back
    clock[0] = 0.5
    progress.line("[3/4] Building Demo (…)")
    progress.line("✖ [4/4] Building Demo.Broken (6.9s)")  # final step always emitted
    kinds = [event.kind for event in events]
    assert kinds == ["process.progress"] * 3
    assert [event.data["current"] for event in events] == [1, 3, 4]
    assert events[0].data["total"] == 4
    assert events[0].data["detail"] == "Built Demo.Basic (481ms)"
    assert events[0].data["label"] == "lake"
    assert events[0].phase == "execution"
    assert events[0].message == "lake: 1/4 Built Demo.Basic (481ms)"
    assert events[-1].data["detail"] == "Building Demo.Broken (6.9s)"


def test_finish_flushes_the_step_rate_limiting_held_back() -> None:
    events, emitter = _collect()
    progress = OutputProgress(emitter.emit, label="lake", clock=lambda: 0.0)
    progress.line("[1/10] Building A")
    progress.line("[5/10] Building E")
    progress.finish()
    progress.finish()  # idempotent
    assert [event.data["current"] for event in events] == [1, 5]


def test_other_output_becomes_a_throttled_heartbeat() -> None:
    events, emitter = _collect()
    clock = [0.0]
    progress = OutputProgress(emitter.emit, label="lake", clock=lambda: clock[0])
    progress.line("Attempting to download 6931 file(s)")
    progress.line("")
    progress.line("Decompressing 6931 file(s)")  # within 1s: dropped
    clock[0] = 1.5
    progress.line("Unpacked in 1234 ms")
    assert [event.kind for event in events] == ["process.output", "process.output"]
    assert events[0].data["line"] == "Attempting to download 6931 file(s)"
    assert events[1].message == "lake: Unpacked in 1234 ms"


def test_observed_process_emits_command_output_and_completion(tmp_path: Path) -> None:
    events, emitter = _collect()
    observed = ObservedProcess(
        emitter.emit,
        command=["/runtime/toolchain/bin/lake", "build"],
        logical_command=["lake", "build"],
        cwd=tmp_path,
        phase="build",
        verbose=True,
    )
    observed.start()
    observed.output.line("first trace")
    observed.output.line("second trace")
    observed.finish(BackendResult(0, "", "", 1.25, False, False, False, ()))

    assert [event.kind for event in events] == [
        "process.started",
        "process.output",
        "process.output",
        "process.finished",
    ]
    assert events[0].data["logical_command"] == ["lake", "build"]
    assert events[0].data["command"] == ["/runtime/toolchain/bin/lake", "build"]
    assert events[0].data["cwd"] == str(tmp_path)
    assert events[1].phase == "build"
    assert events[-1].data["exit_code"] == 0
    assert events[-1].data["elapsed_seconds"] == 1.25


def test_verbose_observed_process_retains_complete_output_lines(tmp_path: Path) -> None:
    events, emitter = _collect()
    observed = ObservedProcess(
        emitter.emit,
        command=["/runtime/toolchain/bin/lake", "--verbose", "build"],
        logical_command=["lake", "build"],
        cwd=tmp_path,
        phase="build",
        verbose=True,
    )
    line = "trace: " + "x" * 400
    observed.output.line(line)

    assert events[0].kind == "process.output"
    assert events[0].data["line"] == line
    assert events[0].phase == "build"


# -- backend streaming ---------------------------------------------------------


def test_local_backend_streams_lines_including_carriage_return_redraws(tmp_path: Path) -> None:
    lines: list[str] = []
    script = (
        "import sys; print('first'); print('[1/2] Building A'); "
        "sys.stdout.write('25%\\r50%\\r100%\\n'); sys.stderr.write('warn\\n'); "
        "sys.stdout.write('tail without newline')"
    )
    result = LocalBackend().execute(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        environment={},
        policy=ExecutionPolicy(timeout_seconds=30),
        on_output=lines.append,
    )
    assert result.exit_code == 0
    assert "first" in lines
    assert "[1/2] Building A" in lines
    assert {"25%", "50%", "100%"} <= set(lines)
    assert "warn" in lines
    assert "tail without newline" in lines
    assert "first" in result.stdout  # the transcript is unaffected


def test_local_backend_survives_a_failing_observer(tmp_path: Path) -> None:
    def broken(_line: str) -> None:
        raise RuntimeError("observer bug")

    result = LocalBackend().execute(
        [sys.executable, "-c", "print('ok')"],
        cwd=tmp_path,
        environment={},
        policy=ExecutionPolicy(timeout_seconds=30),
        on_output=broken,
    )
    assert result.exit_code == 0
    assert "ok" in result.stdout


def test_runtime_only_passes_on_output_to_backends_that_accept_it(tmp_path: Path) -> None:
    runtime = Runtime(home=tmp_path / "runtime")
    observer = OutputProgress(runtime.events.emit, label="lake")
    assert set(runtime._output_observer_arguments(observer)) == {"on_output"}

    class LegacyBackend:
        name = "legacy"

        def execute(self, command: Any, *, cwd: Any, environment: Any, policy: Any, cancel=None):
            raise AssertionError("not called")

    legacy = Runtime(home=tmp_path / "legacy", backend=LegacyBackend())  # type: ignore[arg-type]
    assert legacy._output_observer_arguments(observer) == {}


def test_verbose_runtime_adds_lake_trace_flag_and_records_resolved_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[RuntimeEvent] = []
    runtime = Runtime(home=tmp_path / "runtime", on_event=events.append, verbose=True)
    commands: list[list[str]] = []

    def execute(command: list[str], **_kwargs: Any) -> BackendResult:
        commands.append(command)
        return BackendResult(0, "", "", 0.01, False, False, False, ())

    monkeypatch.setattr(runtime.backend, "execute", execute)
    result = runtime._raw_result(
        ["/runtime/toolchain/bin/lake", "build"],
        cwd=tmp_path,
        toolchain="leanprover/lean4:v4.33.1",
        source_digest="source",
        policy=ExecutionPolicy(),
        logical_command=["lake", "build"],
    )

    assert commands == [["/runtime/toolchain/bin/lake", "--verbose", "build"]]
    assert result.command == tuple(commands[0])
    started = next(event for event in events if event.kind == "process.started")
    assert started.data["logical_command"] == ["lake", "build"]
    assert started.data["command"] == commands[0]


# -- console rendering ---------------------------------------------------------


def _event(kind: str, message: str = "", **data: Any) -> RuntimeEvent:
    return RuntimeEvent(kind=kind, message=message or kind, data=data)


def test_plain_mode_prints_count_checkpoints_only() -> None:
    stream = io.StringIO()
    renderer = ConsoleRenderer(stream=stream, mode="plain", color=False)
    for current in (1, 2000, 2080, 4159, 4160, 6240, 8318):
        renderer(
            _event(
                "process.progress", label="lake", current=current, total=8318, detail="Building X"
            )
        )
    renderer.close()
    assert stream.getvalue().splitlines() == [
        "lake: 25% (2080/8318)",
        "lake: 50% (4159/8318)",
        "lake: 75% (6240/8318)",
        "lake: 100% (8318/8318)",
    ]


def test_tty_mode_redraws_one_bar_line_and_finishes_it() -> None:
    stream = io.StringIO()
    clock = [0.0]
    renderer = ConsoleRenderer(stream=stream, mode="tty", color=False, clock=lambda: clock[0])
    renderer(_event("process.progress", label="lake", current=1, total=4, detail="Building A"))
    clock[0] = 1.0
    renderer(_event("process.progress", label="lake", current=4, total=4, detail="Built D"))
    renderer.close()
    output = stream.getvalue()
    assert output.count("\r") == 2
    assert "lake [█████░░░░░░░░░░░░░░░] 1/4 · Building A" in output
    assert "lake [████████████████████] 4/4 · Built D" in output
    assert output.endswith("\n")


def test_tty_mode_shows_subprocess_output_in_place_and_plain_mode_stays_quiet() -> None:
    tty = io.StringIO()
    ConsoleRenderer(stream=tty, mode="tty", color=False)(
        _event("process.output", line="Decompressing 6931 file(s)")
    )
    assert tty.getvalue() == "\rDecompressing 6931 file(s)"
    plain = io.StringIO()
    ConsoleRenderer(stream=plain, mode="plain", color=False)(
        _event("process.output", line="Decompressing 6931 file(s)")
    )
    assert plain.getvalue() == ""


def test_process_lifecycle_is_announced_and_verbose_mode_keeps_output() -> None:
    plain = io.StringIO()
    renderer = ConsoleRenderer(stream=plain, mode="plain", color=False)
    renderer(
        _event(
            "process.started",
            logical_command=["lake", "build"],
            command=["/toolchain/bin/lake", "build"],
            cwd="/workspace",
        )
    )
    renderer(
        _event(
            "process.finished",
            logical_command=["lake", "build"],
            exit_code=0,
            elapsed_seconds=1.25,
        )
    )
    assert plain.getvalue().splitlines() == [
        "Running: lake build",
        "Working directory: /workspace",
        "Finished: lake build (exit 0) in 1.25s",
    ]

    verbose = io.StringIO()
    detailed = ConsoleRenderer(stream=verbose, mode="plain", color=False, verbose=True)
    detailed(
        _event(
            "process.started",
            logical_command=["lake", "build"],
            command=["/toolchain/bin/lake", "--verbose", "build"],
            cwd="/workspace",
        )
    )
    detailed(_event("process.output", line="trace: lean -o Demo.olean Demo.lean"))
    assert verbose.getvalue().splitlines() == [
        "Running: lake build",
        "Resolved: /toolchain/bin/lake --verbose build",
        "Working directory: /workspace",
        "trace: lean -o Demo.olean Demo.lean",
    ]


def test_adopt_and_detach_events_render_as_counted_progress() -> None:
    stream = io.StringIO()
    renderer = ConsoleRenderer(stream=stream, mode="plain", color=False)
    renderer(_event("adopt.inspect_started", name="alchemy", current=45, total=89))
    renderer(_event("adopt.inspect_started", name="zeta", current=89, total=89))
    renderer(_event("adopt.attach_started", name="alchemy", current=81, total=81))
    renderer(_event("project.detach.package_started", package="mathlib", current=9, total=9))
    renderer.close()
    assert stream.getvalue().splitlines() == [
        "Inspecting projects: 25% (45/89)",
        "Inspecting projects: 50% (45/89)",
        "Inspecting projects: 75% (89/89)",
        "Inspecting projects: 100% (89/89)",
        "Attaching projects: 25% (81/81)",
        "Attaching projects: 50% (81/81)",
        "Attaching projects: 75% (81/81)",
        "Attaching projects: 100% (81/81)",
        "Materializing packages: 25% (9/9)",
        "Materializing packages: 50% (9/9)",
        "Materializing packages: 75% (9/9)",
        "Materializing packages: 100% (9/9)",
    ]


def test_heartbeat_reports_the_last_activity_when_events_stop() -> None:
    stream = io.StringIO()
    renderer = ConsoleRenderer(stream=stream, mode="tty", color=False, heartbeat_seconds=0.05)
    renderer(RuntimeEvent(kind="adopt.inspect_started", message="Inspecting alchemy (1/89)"))
    deadline = time.monotonic() + 3
    while "⋯" not in stream.getvalue() and time.monotonic() < deadline:
        time.sleep(0.05)
    renderer.close()
    assert "⋯ no new events for " in stream.getvalue()
    assert "· last: Inspecting alchemy (1/89)" in stream.getvalue()


def test_heartbeat_is_off_by_default_and_quiet_mode_never_renders() -> None:
    stream = io.StringIO()
    renderer = ConsoleRenderer(stream=stream, mode="tty", color=False)
    renderer(RuntimeEvent(kind="adopt.inspect_started", message="Inspecting alchemy (1/89)"))
    time.sleep(0.2)
    renderer.close()
    assert "⋯" not in stream.getvalue()
    quiet = io.StringIO()
    quiet_renderer = ConsoleRenderer(stream=quiet, mode="quiet", heartbeat_seconds=0.01)
    quiet_renderer(RuntimeEvent(kind="process.progress", message="x"))
    quiet_renderer.close()
    assert quiet.getvalue() == ""


# -- adoption planning events --------------------------------------------------


def test_adoption_planning_announces_each_project(tmp_path: Path) -> None:
    for name in ("alpha", "beta"):
        root = tmp_path / name
        root.mkdir()
        (root / "lean-toolchain").write_text("leanprover/lean4:v4.32.0\n")
        (root / "lakefile.toml").write_text(f'name = "{name}"\n[[lean_lib]]\nname = "Lib"\n')
        (root / "lake-manifest.json").write_text(
            json.dumps({"version": "1.2.0", "packagesDir": ".lake/packages", "packages": []})
        )
    events, emitter = _collect()
    manager = SharedProjectManager(tmp_path / "home", emitter)
    plan_adoption(tmp_path, recursive=True, shared=manager)
    inspect = [event for event in events if event.kind == "adopt.inspect_started"]
    assert [
        (event.data["name"], event.data["current"], event.data["total"]) for event in inspect
    ] == [
        ("alpha", 1, 2),
        ("beta", 2, 2),
    ]
    assert inspect[0].message == "Inspecting alpha (1/2)"
    identity = [event for event in events if event.kind == "adopt.identity_started"]
    assert [event.data["current"] for event in identity] == [1, 2]
    assert SimpleNamespace(**identity[0].data).total == 2
