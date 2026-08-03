from __future__ import annotations

from pathlib import Path

from lean_runtime import Runtime
from lean_runtime.events import EventEmitter


def test_event_emitter_is_structured() -> None:
    events = []
    EventEmitter(events.append).emit("build.started", "Building", package="sample")
    assert events[0].kind == "build.started"
    assert events[0].data == {"package": "sample"}
    assert events[0].to_dict()["message"] == "Building"


def test_doctor_and_empty_store_status_do_not_install_tools(tmp_path: Path) -> None:
    runtime = Runtime(home=tmp_path)
    report = runtime.doctor()
    assert report.ok
    assert {check.name for check in report.checks} == {
        "git",
        "store",
        "disk",
        "elan",
        "staging",
    }
    status = runtime.store_status()
    assert status.environments == 0
    assert status.sources == 0
