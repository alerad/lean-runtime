from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

from lean_runtime import (
    EnvironmentError,
    EnvironmentLock,
    EnvironmentSpec,
    ExecutionJob,
    ResolutionError,
    Runtime,
    ToolchainError,
)
from lean_runtime.backends import LocalBackend
from lean_runtime.environments import Environment, EnvironmentManager
from lean_runtime.runtime import _bundled_lock_for_references
from lean_runtime.store import EnvironmentStore


class FakeToolchains:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.full_installs: list[str] = []

    @property
    def environment(self) -> dict[str, str]:
        return os.environ.copy()

    def ensure(self, toolchain: str, **_kwargs: object) -> str:
        return toolchain

    def ensure_full(self, toolchain: str, **_kwargs: object) -> str:
        self.full_installs.append(toolchain)
        return toolchain

    def command(self, toolchain: str, executable: str, *args: str) -> list[str]:
        if executable == "lean":
            source = args[-1]
            script = (
                "import pathlib,sys; "
                "text=pathlib.Path(sys.argv[1]).read_text(); "
                "bad='BAD' in text; "
                "print(f'{sys.argv[1]}:1:1: error: rejected') if bad else None; "
                "raise SystemExit(1 if bad else 0)"
            )
            return [sys.executable, "-c", script, source]
        return [sys.executable, "-c", "raise SystemExit(0)"]


def test_exact_mathlib_reference_matches_bundled_catalog_lock() -> None:
    lock = _bundled_lock_for_references(["mathlib@v4.32.2"], None)
    assert lock is not None
    assert lock.toolchain == "leanprover/lean4:v4.32.2"
    assert any(package.name == "mathlib" for package in lock.packages)


@pytest.mark.parametrize(
    ("reference", "toolchain"),
    [
        ("leancert@v4.30.0.5", "leanprover/lean4:v4.30.0"),
        ("leancert@v4.31.0", "leanprover/lean4:v4.31.0"),
        ("leancert@v4.32.2.4", "leanprover/lean4:v4.32.2"),
        ("leancert@v4.33.0", "leanprover/lean4:v4.33.0"),
    ],
)
def test_exact_leancert_reference_matches_bundled_catalog_lock(
    reference: str, toolchain: str
) -> None:
    lock = _bundled_lock_for_references([reference], None)
    assert lock is not None
    assert lock.toolchain == toolchain
    assert {package.name for package in lock.packages} >= {"LeanCert", "mathlib"}


def test_catalog_reference_match_respects_context_and_toolchain() -> None:
    assert _bundled_lock_for_references(["mathlib@main"], None) is None
    assert _bundled_lock_for_references(["mathlib@v4.32.2", "leancert@main"], None) is None
    assert _bundled_lock_for_references(["mathlib@v4.32.2"], "v4.31.0") is None


def test_environment_check_does_not_clone_published_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EnvironmentStore(tmp_path / "runtime")
    environment_id = "env_" + "a" * 64
    root = store.environment_path(environment_id)
    (root / "workspace" / ".lake" / "build" / "lib" / "lean").mkdir(parents=True)
    lock = EnvironmentLock(
        toolchain="leanprover/lean4:v4.32.0",
        spec_digest="spec_" + "b" * 64,
        root_lakefile='name = "test"\n',
        root_module="",
        manifest={"packages": []},
        packages=(),
    )
    manager = EnvironmentManager(
        store,
        FakeToolchains(tmp_path / "runtime"),  # type: ignore[arg-type]
        LocalBackend(),
    )
    environment = Environment(
        manager,
        environment_id,
        lock,
        root,
        {"platform": {}, "build_profile": "release", "created_at": "2026-08-12T00:00:00Z"},
    )

    def unexpected_clone(_source: Path, _destination: Path) -> None:
        raise AssertionError("checks must not clone a published workspace")

    monkeypatch.setattr("lean_runtime.environments.clone_tree", unexpected_clone)
    assert environment.check("example : True := by trivial").ok


def test_check_accepts_source(tmp_path: Path) -> None:
    runtime = Runtime(toolchains=FakeToolchains(tmp_path))  # type: ignore[arg-type]
    result = runtime.check("example : True := by trivial", toolchain="4.32.0")
    assert result.ok
    assert result.exit_code == 0
    assert result.toolchain == "leanprover/lean4:v4.32.0"


def test_core_environment_needs_neither_lake_nor_a_full_build(tmp_path: Path) -> None:
    runtime = Runtime(
        toolchains=FakeToolchains(tmp_path),  # type: ignore[arg-type]
        libraries=[],
        availability="local",
    )
    environment = runtime.open_toolchain("v4.32.2")

    assert environment.lock.packages == ()
    metadata = json.loads((environment.root / "metadata.json").read_text())
    assert metadata["build"]["performed"] is False
    assert environment.check("example : True := by trivial").ok


def test_repeated_requests_have_unique_execution_history_ids(tmp_path: Path) -> None:
    runtime = Runtime(toolchains=FakeToolchains(tmp_path))  # type: ignore[arg-type]
    first = runtime.check("example : True := by trivial", toolchain="4.32.0")
    second = runtime.check("example : True := by trivial", toolchain="4.32.0")
    assert first.execution_id != second.execution_id
    assert first.provenance is not None and second.provenance is not None
    assert first.provenance.request_digest == second.provenance.request_digest


def test_finished_job_cannot_be_cancelled() -> None:
    job = ExecutionJob(lambda _cancel: 42)
    assert job.result() == 42
    assert not job.cancel()


def test_check_returns_structured_rejection(tmp_path: Path) -> None:
    runtime = Runtime(toolchains=FakeToolchains(tmp_path))  # type: ignore[arg-type]
    result = runtime.check("BAD", toolchain="4.32.0")
    assert not result.ok
    assert result.exit_code == 1
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0].message == "rejected"


def test_check_requires_version_without_project(tmp_path: Path) -> None:
    runtime = Runtime(toolchains=FakeToolchains(tmp_path))  # type: ignore[arg-type]
    with pytest.raises(ToolchainError):
        runtime.check("example : True := by trivial")


def test_build_infers_project_toolchain(tmp_path: Path) -> None:
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.32.0\n")
    (tmp_path / "lakefile.toml").write_text('name = "sample"\n')
    runtime = Runtime(toolchains=FakeToolchains(tmp_path / "cache"))  # type: ignore[arg-type]
    result = runtime.build(tmp_path, targets=["Example"])
    assert result.ok
    assert result.toolchain == "leanprover/lean4:v4.32.0"


def test_resolution_cancellation_stops_lake_before_lock_publication(tmp_path: Path) -> None:
    runtime = Runtime(
        home=tmp_path / "runtime",
        toolchains=FakeToolchains(tmp_path / "runtime"),  # type: ignore[arg-type]
        libraries=[],
    )
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ResolutionError, match="cancelled") as captured:
        runtime.prepare(
            EnvironmentSpec("leanprover/lean4:v4.32.0", ()),
            cancel=cancel,
        )
    assert captured.value.exit_code == 130
    assert not list(runtime.store.locks.glob("lock_*.json"))


def test_local_runtime_never_falls_through_to_elan_install(tmp_path: Path) -> None:
    runtime = Runtime(home=tmp_path / "home", availability="local", libraries=())

    with pytest.raises(ToolchainError, match="offline mode does not permit"):
        runtime.toolchains.ensure("v99.99.99")


def test_core_environment_never_installs_the_full_toolchain(tmp_path: Path) -> None:
    toolchains = FakeToolchains(tmp_path)
    runtime = Runtime(
        toolchains=toolchains,  # type: ignore[arg-type]
        libraries=[],
        availability="local",
    )
    environment = runtime.open_toolchain("v4.32.2")
    assert environment.check("example : True := by trivial").ok
    assert toolchains.full_installs == []


def test_capsule_rejects_build_and_execute_before_running_lean(tmp_path: Path) -> None:
    toolchains = FakeToolchains(tmp_path)
    runtime = Runtime(
        toolchains=toolchains,  # type: ignore[arg-type]
        libraries=[],
        availability="local",
    )
    environment = runtime.open_toolchain("v4.32.2")
    marker = environment.workspace / ".lean-runtime" / "capsule.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}", encoding="utf-8")

    assert environment.sparse
    with pytest.raises(EnvironmentError, match="cannot run lake build"):
        environment.build(("Demo",))
    with pytest.raises(EnvironmentError, match="cannot run lake exe demo"):
        environment.execute(["lake", "exe", "demo"])
    assert toolchains.full_installs == []


def test_full_environment_accepts_native_capability_requests(tmp_path: Path) -> None:
    runtime = Runtime(
        toolchains=FakeToolchains(tmp_path),  # type: ignore[arg-type]
        libraries=[],
        availability="local",
    )
    environment = runtime.open_toolchain("v4.32.2")
    assert not environment.sparse
    environment.require_capabilities(["native", "development"], imports=["Init"])


def test_capsule_still_rejects_native_capability_requests(tmp_path: Path) -> None:
    runtime = Runtime(
        toolchains=FakeToolchains(tmp_path),  # type: ignore[arg-type]
        libraries=[],
        availability="local",
    )
    environment = runtime.open_toolchain("v4.32.2")
    marker = environment.workspace / ".lean-runtime" / "capsule.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}", encoding="utf-8")
    with pytest.raises(EnvironmentError, match="native/development build inputs"):
        environment.require_capabilities(["native"], imports=["Init"])


def test_full_environment_build_installs_a_lake_capable_toolchain(tmp_path: Path) -> None:
    toolchains = FakeToolchains(tmp_path)
    runtime = Runtime(
        toolchains=toolchains,  # type: ignore[arg-type]
        libraries=[],
        availability="local",
    )
    environment = runtime.open_toolchain("v4.32.2")
    environment.build(("Demo",))
    assert toolchains.full_installs == ["leanprover/lean4:v4.32.2"]
