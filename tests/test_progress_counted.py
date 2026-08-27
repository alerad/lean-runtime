from __future__ import annotations

import io
from pathlib import Path

from lean_runtime import events
from lean_runtime.console import ConsoleRenderer
from lean_runtime.events import EventEmitter, RuntimeEvent
from lean_runtime.progress import CountedProgress, observer_arguments
from lean_runtime.runtime import Runtime
from lean_runtime.store import source_snapshot_digest


def _collect() -> tuple[list[RuntimeEvent], EventEmitter]:
    collected: list[RuntimeEvent] = []
    return collected, EventEmitter(collected.append)


def test_counted_progress_rate_limits_and_always_emits_the_last_step() -> None:
    collected, emitter = _collect()
    clock = [0.0]
    progress = CountedProgress(
        emitter.emit, "demo.count", "Hashing demo", 5, phase="p", clock=lambda: clock[0]
    )
    progress.advance("a")  # first: emitted
    progress.advance("b")  # within 0.2s: held
    clock[0] = 0.5
    progress.advance("c")  # due
    progress.advance("d")  # held
    progress.advance("e")  # last: always emitted
    assert [event.data["current"] for event in collected] == [1, 3, 5]
    assert collected[0].kind == "demo.count"
    assert collected[0].phase == "p"
    assert collected[0].message == "Hashing demo: 1/5 a"
    assert collected[-1].data == {
        "label": "Hashing demo",
        "current": 5,
        "total": 5,
        "detail": "e",
    }


def test_counted_progress_advance_to_and_zero_total() -> None:
    collected, emitter = _collect()
    progress = CountedProgress(emitter.emit, "demo.count", "Parsing", 10, clock=lambda: 0.0)
    progress.advance(to=4)
    progress.advance(to=10)
    assert [event.data["current"] for event in collected] == [4, 10]
    empty = CountedProgress(emitter.emit, "demo.count", "Nothing", 0)
    empty.advance()
    assert collected[-1].data["current"] == 0 and collected[-1].data["total"] == 0


def test_counted_progress_can_announce_zero_before_work() -> None:
    collected, emitter = _collect()
    progress = CountedProgress(emitter.emit, "demo.count", "Working", 2)
    progress.start("starting")
    progress.advance("first")
    progress.advance("second")
    assert [event.data["current"] for event in collected] == [0, 2]
    assert collected[0].message == "Working: 0/2 starting"


def test_renderer_formats_byte_counted_progress() -> None:
    stream = io.StringIO()
    renderer = ConsoleRenderer(stream=stream, mode="plain", color=False)
    renderer(
        RuntimeEvent(
            kind="bundle.archive_write",
            message="x",
            data={
                "label": "Writing archive",
                "current": 512,
                "total": 1024,
                "unit": "bytes",
            },
        )
    )
    renderer.close()
    assert stream.getvalue().splitlines() == [
        "Writing archive: 25% (512 B/1 KiB)",
        "Writing archive: 50% (512 B/1 KiB)",
    ]


def test_current_emitter_is_null_until_activated() -> None:
    assert events.current().callback is None or isinstance(events.current(), EventEmitter)
    collected, emitter = _collect()
    token = events.activate(emitter)
    try:
        events.current().emit("demo.kind", "hello", phase="x", value=1)
        assert [event.kind for event in collected] == ["demo.kind"]
    finally:
        events._current_emitter.reset(token)


def test_runtime_activates_its_emitter(tmp_path: Path) -> None:
    runtime = Runtime(home=tmp_path / "runtime")
    assert events.current() is runtime.events


def test_source_snapshot_digest_reports_counted_progress(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "a.lean").write_text("a")
    (root / "sub" / "b.lean").write_text("b")
    (root / ".git").mkdir()
    (root / ".git" / "ignored").write_text("x")
    collected, emitter = _collect()
    token = events.activate(emitter)
    try:
        digest = source_snapshot_digest(root)
    finally:
        events._current_emitter.reset(token)
    assert digest.startswith("sha256:")
    counted = [event for event in collected if event.kind == "source.snapshot_digest"]
    assert [event.data["current"] for event in counted] == [0, 2]
    assert counted[-1].data["current"] == counted[-1].data["total"] == 2
    assert counted[-1].data["label"] == "Hashing tree"
    assert counted[-1].phase == "fingerprint"


def test_renderer_draws_any_counted_event_without_a_dedicated_handler() -> None:
    stream = io.StringIO()
    renderer = ConsoleRenderer(stream=stream, mode="plain", color=False)
    for current in (1, 25, 50, 100):
        renderer(
            RuntimeEvent(
                kind="verification.inventory",
                message="x",
                data={"label": "Hashing build artifacts", "current": current, "total": 100},
            )
        )
    renderer(RuntimeEvent(kind="unrelated.kind", message="y", data={"current": 3}))
    renderer.close()
    assert stream.getvalue().splitlines() == [
        "Hashing build artifacts: 25% (25/100)",
        "Hashing build artifacts: 50% (50/100)",
        "Hashing build artifacts: 75% (100/100)",
        "Hashing build artifacts: 100% (100/100)",
    ]


def test_observer_arguments_detects_on_output_support() -> None:
    class Modern:
        def execute(self, command, *, cwd, environment, policy, cancel=None, on_output=None): ...

    class Legacy:
        def execute(self, command, *, cwd, environment, policy, cancel=None): ...

    from lean_runtime.progress import OutputProgress

    observer = OutputProgress(lambda *a, **k: None, label="x")
    assert set(observer_arguments(Modern(), observer)) == {"on_output"}
    assert observer_arguments(Legacy(), observer) == {}
