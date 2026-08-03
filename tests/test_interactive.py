from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from lean_runtime import EnvironmentLock, ExecutionPolicy
from lean_runtime.backends import LocalBackend
from lean_runtime.environments import Environment, EnvironmentManager
from lean_runtime.store import EnvironmentStore


class PassthroughToolchains:
    @property
    def environment(self) -> dict[str, str]:
        return os.environ.copy()

    def command(self, _toolchain: str, executable: str, *args: str) -> list[str]:
        return [executable, *args]


def _environment(tmp_path: Path) -> Environment:
    store = EnvironmentStore(tmp_path / "runtime")
    environment_id = "env_" + "a" * 64
    root = store.environment_path(environment_id)
    (root / "workspace").mkdir(parents=True)
    (root / "workspace" / "published.txt").write_text("immutable source")
    lock = EnvironmentLock(
        toolchain="leanprover/lean4:v4.32.0",
        spec_digest="spec_" + "b" * 64,
        root_lakefile='name = "interactive_test"\n',
        root_module="",
        manifest={"packages": []},
        packages=(),
    )
    manager = EnvironmentManager(
        store,
        PassthroughToolchains(),  # type: ignore[arg-type]
        LocalBackend(),
    )
    return Environment(
        manager,
        environment_id,
        lock,
        root,
        {
            "platform": {},
            "build_profile": "release",
            "status": "ready",
            "created_at": "2026-08-03T00:00:00+00:00",
        },
    )


def test_execute_runs_command_in_disposable_instance(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    result = environment.execute(
        [sys.executable, "-c", "import pathlib; print(pathlib.Path('published.txt').read_text())"]
    )
    assert result.ok
    assert result.stdout.strip() == "immutable source"
    assert not Path(result.cwd).exists()
    assert result.execution_id is not None
    record = json.loads(
        (environment.manager.store.executions / f"{result.execution_id}.json").read_text()
    )
    assert record["operation"] == "execute"
    assert record["command"][0] == sys.executable


def test_interactive_session_round_trips_and_records_transcript(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    script = "import sys\nfor line in sys.stdin:\n print(line.strip().upper(), flush=True)"
    with environment.spawn_interactive([sys.executable, "-u", "-c", script]) as session:
        execution_id = session.execution_id
        assert session.running
        assert session.poll() is None
        session.stdin.write("hello lean\n")
        session.stdin.flush()
        assert session.stdout.readline() == "HELLO LEAN\n"
        assert any(environment.manager.store.jobs.iterdir())

    result = session.close()
    assert result.ok
    assert not session.running
    assert session.poll() == 0
    assert result.execution_id == execution_id
    assert result.stdout == "HELLO LEAN\n"
    assert not Path(result.cwd).exists()
    assert not any(environment.manager.store.jobs.iterdir())
    assert session.close() is result
    record = json.loads((environment.manager.store.executions / f"{execution_id}.json").read_text())
    assert record["operation"] == "interactive"


def test_interactive_timeout_is_enforced_and_instance_is_removed(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    session = environment.spawn_interactive(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        policy=ExecutionPolicy(timeout_seconds=0.1),
    )
    time.sleep(0.2)
    result = session.close()
    assert result.timed_out
    assert result.exit_code == 124
    assert not result.ok
    assert not Path(result.cwd).exists()


def test_close_cancels_process_that_does_not_exit_on_eof(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    session = environment.spawn_interactive(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        policy=ExecutionPolicy(timeout_seconds=30),
    )
    result = session.close()
    assert result.cancelled
    assert result.exit_code == 130
    assert not result.ok


def test_interactive_transcript_obeys_output_budget(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    session = environment.spawn_interactive(
        [sys.executable, "-u", "-c", "print('x' * 1000)"],
        policy=ExecutionPolicy(max_output_bytes=25),
    )
    observed = session.stdout.readline()
    result = session.close()
    assert len(observed) == 1001
    assert result.output_truncated
    assert len(result.stdout.encode()) <= 25


def test_context_exception_still_finalizes_session(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    with (
        pytest.raises(RuntimeError, match="caller failed"),
        environment.spawn_interactive(
            [sys.executable, "-c", "import sys; sys.stdin.read()"]
        ) as session,
    ):
        raise RuntimeError("caller failed")

    result = session.close()
    assert result.ok
    assert result.execution_id == session.execution_id
    assert not Path(result.cwd).exists()
