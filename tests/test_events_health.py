from __future__ import annotations

import os
from pathlib import Path

import pytest

from lean_runtime import Runtime, ToolchainError
from lean_runtime.events import EventEmitter
from lean_runtime.health import repair


def test_event_emitter_is_structured() -> None:
    events = []
    EventEmitter(events.append).emit("build.started", "Building", package="sample")
    assert events[0].kind == "build.started"
    assert events[0].data == {"package": "sample"}
    assert events[0].to_dict()["message"] == "Building"


@pytest.mark.parametrize("installed", [False, True])
def test_doctor_and_empty_store_status_do_not_install_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installed: bool
) -> None:
    runtime = Runtime(home=tmp_path)

    def elan_path(*, bootstrap: bool = True) -> Path:
        assert not bootstrap, "doctor must not install Elan"
        if not installed:
            raise ToolchainError("Elan is not installed")
        return tmp_path / ("elan.exe" if os.name == "nt" else "elan")

    monkeypatch.setattr(runtime.toolchains, "elan_path", elan_path)
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
    # POSIX can bootstrap Elan later; Windows requires an existing executable.
    expected = "pass" if installed else "fail" if os.name == "nt" else "warning"
    assert elan.status == expected
    assert all(check.status != "fail" for check in report.checks if check.name != "elan")
    assert report.ok is (expected != "fail")
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
