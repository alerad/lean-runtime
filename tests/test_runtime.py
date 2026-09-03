from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path, PureWindowsPath

import pytest

from lean_runtime import (
    EnvironmentError,
    EnvironmentLock,
    EnvironmentSpec,
    ExecutionJob,
    LockedPackage,
    ResolutionError,
    Runtime,
    ToolchainError,
)
from lean_runtime.backends import BackendResult, LocalBackend
from lean_runtime.environments import (
    Environment,
    EnvironmentManager,
    _environment_staging_path,
)
from lean_runtime.runtime import _bundled_lock_for_references
from lean_runtime.store import EnvironmentStore


class FakeToolchains:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.full_installs: list[str] = []

    @property
    def environment(self) -> dict[str, str]:
        return os.environ.copy()

    def environment_for(self, _toolchain: str) -> dict[str, str]:
        return self.environment

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


def test_standalone_check_progress_accepts_a_direct_toolchain_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(toolchains=FakeToolchains(tmp_path))  # type: ignore[arg-type]
    monkeypatch.setattr(
        runtime.toolchains,
        "command",
        lambda _toolchain, _executable, source: ["lean", source],
    )
    monkeypatch.setattr(
        runtime.backend,
        "execute",
        lambda _command, **_kwargs: BackendResult(0, "", "", 0.01, False, False, False, ()),
    )

    result = runtime.check("example : True := by trivial", toolchain="4.32.0")

    assert result.ok
    assert result.provenance is not None


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


def test_environment_stage_preserves_windows_artifact_path_budget(tmp_path: Path) -> None:
    store = EnvironmentStore(tmp_path / "runtime")
    stage = _environment_staging_path(store)

    assert stage.parent == store.environments
    assert stage.name.startswith(".staging-")
    assert len(stage.name) == len(".staging-") + 12

    # This is the path that failed at exactly MAX_PATH with the previous
    # `.staging-{pid}-{full_uuid}` name during Mathlib cache extraction.
    windows_stage = (
        PureWindowsPath(r"C:\Users\userx\AppData\Local\lean-runtime\environments") / stage.name
    )
    artifact = windows_stage / (
        "workspace/.lake/packages/mathlib/.lake/build/lib/lean/Mathlib/Analysis/"
        "SpecialFunctions/ContinuousFunctionalCalculus/Rpow/"
        "RingInverseOrder.olean.private.hash"
    )
    previous_stage = windows_stage.parent / (".staging-48876-" + "a" * 32)
    previous_artifact = previous_stage / artifact.relative_to(windows_stage)
    assert len(str(previous_artifact)) >= 260
    assert len(str(artifact)) < 260


def test_source_materialization_observes_hydration_and_traces_verbose_lake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EnvironmentStore(tmp_path / "runtime")
    toolchains = FakeToolchains(tmp_path / "runtime")
    manager = EnvironmentManager(
        store,
        toolchains,  # type: ignore[arg-type]
        LocalBackend(),
        verbose=True,
    )
    package = LockedPackage(
        name="sample",
        url="https://example.test/sample.git",
        revision="a" * 40,
        source_id="source_" + "b" * 64,
        tree_hash="c" * 40,
        artifact_command=("lake", "exe", "cache", "get"),
    )
    store.source_path(package.source_id).mkdir(parents=True)
    lock = EnvironmentLock(
        toolchain="leanprover/lean4:v4.33.1",
        spec_digest="spec_" + "d" * 64,
        root_lakefile='name = "test"\n',
        root_module="",
        manifest={"version": "1.1.0", "packagesDir": ".lake/packages", "packages": []},
        packages=(package,),
    )
    monkeypatch.setattr(
        toolchains,
        "command",
        lambda _toolchain, executable, *args: [f"/resolved/{executable}", *args],
    )
    calls: list[dict[str, object]] = []

    def run_process(**kwargs: object) -> BackendResult:
        calls.append(kwargs)
        return BackendResult(0, "trace output", "", 0.25, False, False, False, ())

    monkeypatch.setattr(manager, "_run_process", run_process)
    destination = store.environment_path("env_" + "e" * 64)
    manager._materialize(
        lock,
        "env_" + "e" * 64,
        destination,
        "release",
        build_timeout=30,
        accelerate=False,
        cancel=None,
    )

    assert [call["logical_command"] for call in calls] == [
        ["lake", "exe", "cache", "get"],
        ["lake", "build"],
    ]
    assert [call["command"] for call in calls] == [
        ["/resolved/lake", "--verbose", "exe", "cache", "get"],
        ["/resolved/lake", "--verbose", "build"],
    ]
    assert [call["phase"] for call in calls] == ["artifact_hydration", "build"]


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


class InstalledToolchains(FakeToolchains):
    def __init__(self, home: Path, installed: str) -> None:
        super().__init__(home)
        self.installed = installed

    def is_available_locally(self, toolchain: str) -> bool:
        return toolchain == self.installed


def _core_lock(toolchain: str) -> EnvironmentLock:
    return EnvironmentLock(
        toolchain=toolchain,
        spec_digest="spec_" + "c" * 64,
        root_lakefile='name = "core"\n',
        root_module="",
        manifest={"packages": []},
        packages=(),
    )


def test_core_lock_is_ready_locally_once_its_toolchain_is_installed(tmp_path: Path) -> None:
    # Core-only checks run straight on the toolchain and never materialize an
    # environment in the store, so the store must not decide their readiness.
    runtime = Runtime(
        home=tmp_path / "home",
        toolchains=InstalledToolchains(tmp_path, "leanprover/lean4:v4.32.2"),  # type: ignore[arg-type]
        libraries=[],
    )
    assert runtime.exact_ready_locally(_core_lock("leanprover/lean4:v4.32.2")) is True
    assert (
        runtime.exact_ready_locally(
            _core_lock("leanprover/lean4:v4.32.2"), import_roots=("Lean.Elab", "Std.Data.HashMap")
        )
        is True
    )
    assert (
        runtime.exact_ready_locally(
            _core_lock("leanprover/lean4:v4.32.2"), import_roots=("Mathlib",)
        )
        is False
    )
    assert runtime.exact_ready_locally(_core_lock("leanprover/lean4:v4.31.0")) is False


def test_core_lock_plan_costs_nothing_once_its_toolchain_is_installed(tmp_path: Path) -> None:
    runtime = Runtime(
        home=tmp_path / "home",
        toolchains=InstalledToolchains(tmp_path, "leanprover/lean4:v4.32.2"),  # type: ignore[arg-type]
        libraries=[],
    )
    plan = runtime.plan_exact(_core_lock("leanprover/lean4:v4.32.2"))
    assert plan["environment_ready"] is True
    assert plan["environment_download_bytes"] == 0
    assert plan["toolchain_download_bytes"] == 0
    assert plan["download_bytes"] == 0
    assert plan["download_bytes_complete"] is True
    assert plan["libraries"] == []

    missing = runtime.plan_exact(_core_lock("leanprover/lean4:v4.31.0"))
    assert missing["environment_ready"] is True
    assert missing["environment_download_bytes"] == 0
    assert missing["toolchain_installed"] is False
    assert missing["download_bytes_complete"] is False
