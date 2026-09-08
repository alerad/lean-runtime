"""Lifecycle guarantees: publication, cleanup and cancellation across processes."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest
from _lockholder import ForeignLockHolder
from conftest import make_lock

from lean_runtime import Runtime
from lean_runtime.errors import EnvironmentError, ProjectError
from lean_runtime.health import diagnose, repair
from lean_runtime.matrix import MatrixContext, run_matrix
from lean_runtime.project_sharing import ProjectAdopter
from lean_runtime.store import EnvironmentStore


def test_repair_keeps_a_staging_tree_another_process_is_building(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = Runtime(home=tmp_path)
    store = runtime.store
    live = store.environments / ".staging-aaaaaaaaaaaa"
    live.mkdir()
    dead = store.environments / ".staging-bbbbbbbbbbbb"
    dead.mkdir()
    monkeypatch.setattr(runtime.toolchains, "elan_path", lambda *, bootstrap: tmp_path / "elan")
    with ForeignLockHolder(store.lock_paths.staging("aaaaaaaaaaaa")):
        report = diagnose(runtime.toolchains, store)
        staging = next(check for check in report.checks if check.name == "staging")
        assert "1 incomplete" in staging.message
        repair(runtime.toolchains, store)
        assert live.is_dir()
    assert not dead.exists()


def test_scratch_cleanup_retains_a_workspace_leased_by_another_process(tmp_path: Path) -> None:
    store = EnvironmentStore(tmp_path / "runtime")
    workspace = store.jobs / ("execution_" + "c" * 64)
    workspace.mkdir()
    (workspace / "payload").write_bytes(b"busy")
    with ForeignLockHolder(store.lock_paths.workspace(workspace)):
        report = store.clean_scratch(dry_run=False, minimum_age_seconds=0)
    assert f"jobs/{workspace.name}" in report.retained
    assert workspace.is_dir()
    report = store.clean_scratch(dry_run=False, minimum_age_seconds=0)
    assert f"jobs/{workspace.name}" in report.removed


def test_scratch_cleanup_loses_a_race_with_a_lease_taken_after_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EnvironmentStore(tmp_path / "runtime")
    workspace = store.jobs / ("execution_" + "d" * 64)
    workspace.mkdir()
    lease: list[object] = []
    original = store._workspace_active

    def active_then_leased(path: Path) -> bool:
        result = original(path)
        # The lease arrives after the cleanup decided the workspace is free.
        lease.append(store.lease_workspace(path, "test"))
        return result

    monkeypatch.setattr(store, "_workspace_active", active_then_leased)
    report = store.clean_scratch(dry_run=False, minimum_age_seconds=0)
    assert f"jobs/{workspace.name}" in report.retained
    assert workspace.is_dir()
    lease[0].close()  # type: ignore[attr-defined]


def test_legacy_package_cleanup_honours_the_shared_project_package_lock(tmp_path: Path) -> None:
    store = EnvironmentStore(tmp_path)
    runtime = Runtime(home=tmp_path)
    package = tmp_path / "project-packages" / ("pkg_" + "a" * 32)
    package.mkdir(parents=True)
    (package / ".lean-runtime-package.json").write_text(
        json.dumps(
            {
                "schema": "lean-runtime-shared-project/2",
                "artifact_key": {"schema": "lean-runtime-package-artifact-key/1"},
            }
        )
    )
    # The shared-project manager's lock for this package and the store's lock
    # for it are the same file, so a build in another process blocks cleanup.
    assert runtime.shared_projects.lock_paths.package(package.name) == store.lock_paths.package(
        package.name
    )
    with ForeignLockHolder(runtime.shared_projects.lock_paths.package_build(package.name)):
        report = store.clean_legacy_project_artifacts(dry_run=False)
    assert report.retained == (package.name,)
    assert package.is_dir()


def test_lock_packages_dir_is_validated_at_construction(tmp_path: Path) -> None:
    from lean_runtime import EnvironmentLock

    good = make_lock("a", packages=("dep",))
    with pytest.raises(EnvironmentError, match="packagesDir"):
        EnvironmentLock(
            toolchain=good.toolchain,
            spec_digest=good.spec_digest,
            root_lakefile=good.root_lakefile,
            root_module=good.root_module,
            manifest={**good.manifest, "packagesDir": "../escape"},
            packages=good.packages,
        )
    with pytest.raises(EnvironmentError, match="packagesDir"):
        EnvironmentLock.from_dict({**good.to_dict(), "manifest": {"packagesDir": "C:/x"}})


def test_sparse_acquisition_receives_the_cancel_event(tmp_path: Path) -> None:
    runtime = Runtime(home=tmp_path, libraries=[])
    observed: list[threading.Event | None] = []

    class Library:
        class repository:  # noqa: N801 - mimics OCIRepository.display
            display = "oci://example/cache"

        def pull_capsule(self, *_args: object, **kwargs: object) -> str:
            observed.append(kwargs.get("cancel"))  # type: ignore[arg-type]
            return "env_" + "e" * 64

    runtime.libraries = (Library(),)  # type: ignore[assignment]
    cancel = threading.Event()
    runtime._acquire_sparse_modules(make_lock("e"), ("Sample",), frozenset({"check"}), cancel)
    assert observed == [cancel]


def test_matrix_failure_cancels_running_siblings_without_a_caller_event(tmp_path: Path) -> None:
    started = threading.Event()
    seen: dict[str, threading.Event | None] = {}

    class FakeRuntime:
        def check(self, source: str, *, toolchain: str, filename: str, cancel: threading.Event):
            seen[toolchain] = cancel
            if toolchain == "fail":
                started.wait(5)
                raise RuntimeError("boom")
            started.set()
            assert cancel.wait(5), "sibling was never signalled"
            raise EnvironmentError("cancelled")

    contexts = (
        MatrixContext(name="a", toolchain="fail"),
        MatrixContext(name="b", toolchain="slow"),
    )
    with pytest.raises(RuntimeError, match="boom"):
        run_matrix(
            FakeRuntime(),
            "theorem t : True := trivial",
            filename="Main.lean",
            contexts=contexts,
            base=tmp_path,
            concurrency=2,
        )
    assert seen["fail"] is seen["slow"] and seen["fail"].is_set()


def test_attach_rollback_restores_previous_records(tmp_path: Path) -> None:
    from test_projects import ProjectToolchains, _shared_project

    source, _revision = _shared_project(tmp_path / "project", tmp_path / "dependency")
    runtime = Runtime(toolchains=ProjectToolchains(tmp_path / "runtime"), libraries=[])  # type: ignore[arg-type]
    from lean_runtime.projects import discover_project

    context = discover_project(source)
    from lean_runtime.project_sharing import _CONFIG_CONTENT

    config = context.root / "lean-runtime.toml"
    config.write_text(_CONFIG_CONTENT, encoding="utf-8")
    marker = context.root / ".lake" / "lean-runtime-attachment.json"
    marker.parent.mkdir(exist_ok=True)
    marker.write_text('{"schema": "stale"}', encoding="utf-8")

    def reject(overrides: Path | None) -> None:
        if overrides is None:
            raise ProjectError("plain Lake rejected the graph")

    adopter = ProjectAdopter(runtime.shared_projects)
    with pytest.raises(ProjectError, match="rejected"):
        adopter.attach(context, probe=reject)
    assert config.read_text(encoding="utf-8") == _CONFIG_CONTENT
    assert marker.read_text(encoding="utf-8") == '{"schema": "stale"}'


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only fixture helper")
def test_lock_wait_is_cancellable(tmp_path: Path) -> None:
    store = EnvironmentStore(tmp_path)
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    from lean_runtime.locking import FileLock

    with (
        ForeignLockHolder(store.lock_paths.environment("env_x")),
        pytest.raises(EnvironmentError, match="cancelled"),
        FileLock(store.lock_paths.environment("env_x"), timeout=30, cancel=cancel),
    ):
        pass
