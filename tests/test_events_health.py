from __future__ import annotations

import os
from pathlib import Path

from lean_runtime import Runtime
from lean_runtime.events import EventEmitter
from lean_runtime.health import repair


def test_event_emitter_is_structured() -> None:
    events = []
    EventEmitter(events.append).emit("build.started", "Building", package="sample")
    assert events[0].kind == "build.started"
    assert events[0].data == {"package": "sample"}
    assert events[0].to_dict()["message"] == "Building"


def test_doctor_and_empty_store_status_do_not_install_tools(tmp_path: Path) -> None:
    runtime = Runtime(home=tmp_path)
    report = runtime.doctor()
    assert {check.name for check in report.checks} == {
        "git",
        "store",
        "disk",
        "elan",
        "staging",
        "scratch",
        "project-artifacts-v1",
        "cleanup",
    }
    elan = next(check for check in report.checks if check.name == "elan")
    if os.name == "nt":
        assert not report.ok
        assert elan.status == "fail"
    else:
        assert report.ok
        assert elan.status in {"pass", "warning"}
    status = runtime.store_status()
    assert status.environments == 0
    assert status.sources == 0


def test_doctor_repair_removes_legacy_abandoned_scratch(monkeypatch, tmp_path: Path) -> None:
    runtime = Runtime(home=tmp_path)
    abandoned = runtime.store.home / "resolution" / "resolve-legacy"
    abandoned.mkdir(parents=True)
    (abandoned / "payload").write_text("old")
    os.utime(abandoned, (1_000_000_000, 1_000_000_000))
    recent = runtime.store.home / "resolution" / "resolve-recent-legacy"
    recent.mkdir(parents=True)
    monkeypatch.setattr(runtime.toolchains, "elan_path", lambda *, bootstrap: tmp_path / "elan")

    repair(runtime.toolchains, runtime.store)

    assert not abandoned.exists()
    assert recent.exists()
