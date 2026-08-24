from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lean_runtime.errors import EnvironmentError
from lean_runtime.events import EventEmitter, RuntimeEvent
from lean_runtime.header_cache import ENABLE_VARIABLE, LeanHeaderCache
from lean_runtime.locking import FileLock
from lean_runtime.models import ExecutionResult
from lean_runtime.project_execution import ProjectExecutor


class SnapshotToolchains:
    @property
    def environment(self) -> dict[str, str]:
        return os.environ.copy()

    def executable_digest(self, _toolchain: str, executable: str) -> str:
        return f"sha256:{executable}"

    def command(self, _toolchain: str, _executable: str, *args: str) -> list[str]:
        assert args == ("--help",)
        return [sys.executable, "-c", "print('--incr-header-save --incr-load')"]


BASE = ["lake", "env", "lean", "Main.lean"]


def _cache(tmp_path: Path, events: EventEmitter | None = None) -> LeanHeaderCache:
    cache = LeanHeaderCache(tmp_path, SnapshotToolchains(), events)  # type: ignore[arg-type]
    cache.enabled = True
    return cache


def _saved_path(command: list[str]) -> Path:
    argument = next(item for item in command if item.startswith("--incr-header-save="))
    return Path(argument.split("=", 1)[1])


def _publish(cache: LeanHeaderCache, source: str, module: str = "Main.lean") -> None:
    with cache.command("v4.33.0", "workspace", module, source, BASE) as command:
        saved = _saved_path(command)
        saved.write_bytes(b"snapshot")
        Path(str(saved) + ".deps").write_text("{}")


def test_snapshots_are_disabled_unless_opted_in(tmp_path: Path) -> None:
    cache = LeanHeaderCache(tmp_path, SnapshotToolchains())  # type: ignore[arg-type]
    assert cache.enabled is False
    source = "import Mathlib\nexample : True := by trivial\n"
    with cache.command("v4.33.0", "workspace", "Main.lean", source, BASE) as command:
        assert command == BASE


def test_environment_variable_opts_in(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(ENABLE_VARIABLE, "1")
    cache = LeanHeaderCache(tmp_path, SnapshotToolchains())  # type: ignore[arg-type]
    assert cache.enabled is True


def test_header_snapshots_are_reused_for_the_same_import_block(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    first_source = "import Mathlib\nexample : True := by trivial\n"
    _publish(cache, first_source)

    changed_body = "import Mathlib\nexample : 1 = 1 := by rfl\n"
    with cache.command("v4.33.0", "workspace", "Main.lean", changed_body, BASE) as second:
        assert any(argument.startswith("--incr-load=") for argument in second)

    changed_import = "import Mathlib.Data.Nat.Basic\nexample : True := by trivial\n"
    with cache.command("v4.33.0", "workspace", "Main.lean", changed_import, BASE) as third:
        assert any(argument.startswith("--incr-header-save=") for argument in third)


def test_distinct_modules_never_share_a_snapshot(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    source = "import Mathlib\nexample : True := by trivial\n"
    _publish(cache, source, module="Main.lean")
    with cache.command("v4.33.0", "workspace", "Other.lean", source, BASE) as command:
        assert any(argument.startswith("--incr-header-save=") for argument in command)


def test_discard_quarantines_a_published_snapshot(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    source = "import Mathlib\nexample : True := by trivial\n"
    _publish(cache, source)
    cache.discard("v4.33.0", "workspace", "Main.lean", source)
    with cache.command("v4.33.0", "workspace", "Main.lean", source, BASE) as command:
        assert any(argument.startswith("--incr-header-save=") for argument in command)


def _only_lock(tmp_path: Path) -> Path:
    locks = list((tmp_path / "header-snapshots").glob("*/*.lock"))
    assert len(locks) == 1
    return locks[0]


def test_existing_snapshots_load_without_taking_the_creation_lock(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    source = "import Mathlib\nexample : True := by trivial\n"
    _publish(cache, source)
    commands: list[list[str]] = []

    def hit() -> None:
        with cache.command("v4.33.0", "workspace", "Main.lean", source, BASE) as command:
            commands.append(command)

    with FileLock(_only_lock(tmp_path), timeout=5):
        worker = threading.Thread(target=hit)
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive(), "cache hit blocked on the creation lock"
    assert any(argument.startswith("--incr-load=") for argument in commands[0])


def test_waiters_cancel_promptly_and_announce_the_wait(tmp_path: Path) -> None:
    events: list[RuntimeEvent] = []
    cache = _cache(tmp_path, EventEmitter(events.append))
    source = "import Mathlib\nexample : True := by trivial\n"
    with cache.command("v4.33.0", "workspace", "Main.lean", source, BASE):
        pass  # creates the lock file without publishing a snapshot
    cancelled = threading.Event()
    cancelled.set()
    started = time.monotonic()
    with (
        FileLock(_only_lock(tmp_path), timeout=5),
        pytest.raises(EnvironmentError, match="cancelled"),
        cache.command("v4.33.0", "workspace", "Main.lean", source, BASE, cancel=cancelled),
    ):
        raise AssertionError("cancelled waiter must not run")
    assert time.monotonic() - started < 5
    assert [event.kind for event in events] == ["check.header_wait"]
    assert "Main.lean" in events[0].message


def test_snapshots_published_mid_creation_are_seen_after_the_lock(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    source = "import Mathlib\nexample : True := by trivial\n"
    with cache.command("v4.33.0", "workspace", "Main.lean", source, BASE):
        pass
    lock = _only_lock(tmp_path)
    commands: list[list[str]] = []

    def waiter() -> None:
        with cache.command("v4.33.0", "workspace", "Main.lean", source, BASE) as command:
            commands.append(command)

    with FileLock(lock, timeout=5):
        worker = threading.Thread(target=waiter)
        worker.start()
        time.sleep(0.3)
        assert worker.is_alive(), "missing snapshot must serialize behind creation"
        snapshot = Path(str(lock)[: -len(".lock")] + ".snap")
        snapshot.write_bytes(b"snapshot")
        Path(str(snapshot) + ".deps").write_text("{}")
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert any(argument.startswith("--incr-load=") for argument in commands[0])


def _execution(ok: bool, *, timed_out: bool = False, stdout: str = "") -> ExecutionResult:
    return ExecutionResult(
        ok=ok,
        exit_code=0 if ok else 1,
        toolchain="v4.33.0",
        command=tuple(BASE),
        cwd="/tmp",
        stdout=stdout,
        stderr="",
        elapsed_seconds=0.5,
        timed_out=timed_out,
    )


def _executor(cache: LeanHeaderCache, events: EventEmitter) -> ProjectExecutor:
    runtime = SimpleNamespace(header_cache=cache, events=events)
    return ProjectExecutor(runtime)  # type: ignore[arg-type]


def test_timed_out_snapshot_checks_are_retried_without_the_snapshot(tmp_path: Path) -> None:
    events: list[RuntimeEvent] = []
    cache = _cache(tmp_path)
    source = "import Mathlib\nexample : True := by trivial\n"
    _publish(cache, source)
    executor = _executor(cache, EventEmitter(events.append))
    context = SimpleNamespace(toolchain="v4.33.0")
    calls: list[list[str]] = []

    def execute(command: list[str]) -> ExecutionResult:
        calls.append(command)
        if any(argument.startswith("--incr-load=") for argument in command):
            return _execution(False, timed_out=True)
        return _execution(True)

    result = executor._checked_with_header_snapshots(
        context,
        "workspace",
        "Main.lean",
        source,
        BASE,
        execute,
        None,  # type: ignore[arg-type]
    )
    assert result.ok
    assert result.elapsed_seconds == pytest.approx(1.0)
    assert result.timings[0].phase == "header_snapshot"
    assert len(calls) == 2 and calls[1] == BASE
    assert [event.kind for event in events] == ["project.header_snapshot_discarded"]
    with cache.command("v4.33.0", "workspace", "Main.lean", source, BASE) as command:
        assert any(argument.startswith("--incr-header-save=") for argument in command)


def test_snapshot_diagnostics_in_output_also_trigger_the_fallback(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    source = "import Mathlib\nexample : True := by trivial\n"
    _publish(cache, source)
    executor = _executor(cache, EventEmitter())
    context = SimpleNamespace(toolchain="v4.33.0")
    calls: list[list[str]] = []

    def execute(command: list[str]) -> ExecutionResult:
        calls.append(command)
        loading = [argument for argument in command if argument.startswith("--incr-load=")]
        if loading:
            snapshot = loading[0].split("=", 1)[1]
            return _execution(False, stdout=f"error: invalid header snapshot {snapshot}")
        return _execution(True)

    result = executor._checked_with_header_snapshots(
        context,
        "workspace",
        "Main.lean",
        source,
        BASE,
        execute,
        None,  # type: ignore[arg-type]
    )
    assert result.ok and len(calls) == 2


def test_ordinary_failures_are_not_retried(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    source = "import Mathlib\nexample : True := by trivial\n"
    _publish(cache, source)
    executor = _executor(cache, EventEmitter())
    context = SimpleNamespace(toolchain="v4.33.0")
    calls: list[list[str]] = []

    def execute(command: list[str]) -> ExecutionResult:
        calls.append(command)
        return _execution(False, stdout="error: unsolved goals")

    result = executor._checked_with_header_snapshots(
        context,
        "workspace",
        "Main.lean",
        source,
        BASE,
        execute,
        None,  # type: ignore[arg-type]
    )
    assert not result.ok and len(calls) == 1
    with cache.command("v4.33.0", "workspace", "Main.lean", source, BASE) as command:
        assert any(argument.startswith("--incr-load=") for argument in command)


def test_disabled_cache_never_triggers_the_fallback(tmp_path: Path) -> None:
    cache = LeanHeaderCache(tmp_path, SnapshotToolchains())  # type: ignore[arg-type]
    executor = _executor(cache, EventEmitter())
    context = SimpleNamespace(toolchain="v4.33.0")
    calls: list[list[str]] = []

    def execute(command: list[str]) -> ExecutionResult:
        calls.append(command)
        return _execution(False, timed_out=True)

    source = "import Mathlib\nexample : True := by trivial\n"
    result = executor._checked_with_header_snapshots(
        context,
        "workspace",
        "Main.lean",
        source,
        BASE,
        execute,
        None,  # type: ignore[arg-type]
    )
    assert not result.ok and len(calls) == 1 and calls[0] == BASE


def test_shared_builds_record_workspace_lock_timing(tmp_path: Path) -> None:
    from contextlib import contextmanager

    @contextmanager
    def build_lock(_workspace, *, cancel=None):
        yield

    runtime = SimpleNamespace(
        toolchains=SimpleNamespace(
            command=lambda _toolchain, *args: list(args),
            ensure_full=lambda _toolchain, cancel=None: None,
        ),
        lake_cache=SimpleNamespace(environment=lambda _context: None),
        shared_projects=SimpleNamespace(
            prepare=lambda _context, cancel=None: SimpleNamespace(
                overrides_file=tmp_path / "overrides.json"
            ),
            build_lock=build_lock,
        ),
        _raw_result=lambda *_args, **_kwargs: _execution(True),
    )
    executor = ProjectExecutor(runtime)  # type: ignore[arg-type]
    context = SimpleNamespace(
        toolchain="v4.33.0",
        root=tmp_path,
        provenance=lambda: None,
        package_provenance=lambda: (),
    )
    built = executor.build(
        context,  # type: ignore[arg-type]
        targets=("Demo:leanArts",),
        policy=None,  # type: ignore[arg-type]
        shared=True,
    )
    assert built.ok
    assert built.timings[0].phase == "workspace_lock"
