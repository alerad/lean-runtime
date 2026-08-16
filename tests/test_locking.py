from __future__ import annotations

import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lean_runtime.errors import EnvironmentError
from lean_runtime.events import EventEmitter, RuntimeEvent
from lean_runtime.locking import FileLock
from lean_runtime.shared_projects import SharedProjectManager


def test_waiters_learn_the_lock_holder_and_release_clears_it(tmp_path: Path) -> None:
    lock_path = tmp_path / "shared.lock"
    holders: list[dict[str, Any] | None] = []
    with FileLock(lock_path, owner={"operation": "shared build", "packages": ["mathlib"]}):
        waiter = FileLock(lock_path, timeout=0.2, on_wait=holders.append)
        with pytest.raises(EnvironmentError, match="timed out"):
            waiter.__enter__()
    assert holders == [{"pid": os.getpid(), "operation": "shared build", "packages": ["mathlib"]}]
    assert FileLock(lock_path).holder() is None


def test_on_wait_fires_once_and_cancellation_still_raises(tmp_path: Path) -> None:
    lock_path = tmp_path / "shared.lock"
    observed: list[dict[str, Any] | None] = []
    cancelled = threading.Event()
    cancelled.set()
    with FileLock(lock_path):
        waiter = FileLock(lock_path, cancel=cancelled, on_wait=observed.append)
        with pytest.raises(EnvironmentError, match="cancelled"):
            waiter.__enter__()
    assert observed == [None]  # holder wrote no owner description


def test_build_lock_waits_announce_the_holding_operation(tmp_path: Path) -> None:
    events: list[RuntimeEvent] = []
    manager = SharedProjectManager(tmp_path, EventEmitter(events.append))
    workspace = SimpleNamespace(packages=("mathlib", "batteries"), package_ids=("pkg-1",))
    cancelled = threading.Event()
    cancelled.set()
    with (
        FileLock(
            tmp_path / "locks" / "pkg-1-build.lock",
            owner={"operation": "shared build", "packages": ["mathlib", "batteries"]},
        ),
        pytest.raises(EnvironmentError, match="cancelled"),
        manager.build_lock(workspace, cancel=cancelled),  # type: ignore[arg-type]
    ):
        raise AssertionError("cancelled waiter must not run")
    assert [event.kind for event in events] == ["project.workspace_lock_wait"]
    message = events[0].message
    assert "Waiting for shared workspace lock held by" in message
    assert f"PID {os.getpid()}" in message
    assert "shared build of mathlib, batteries" in message
