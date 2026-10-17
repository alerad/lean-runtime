"""Exact source compositions: immutable pins, bounded capture and shared projects."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from lean_runtime import Runtime
from lean_runtime.composition_sources import inventory
from lean_runtime.errors import EnvironmentError, ProjectError

TOOLCHAIN = "leanprover/lean4:v4.33.1"


def project(root: Path, name: str, *, toolchain: str = TOOLCHAIN, packages=None) -> Path:
    root.mkdir(parents=True)
    (root / "lean-toolchain").write_text(toolchain + "\n")
    (root / "lakefile.toml").write_text(f'name = "{name.lower()}"\n[[lean_lib]]\nname = "{name}"\n')
    (root / "lake-manifest.json").write_text(
        json.dumps({"version": "1.1.0", "packages": packages or []})
    )
    (root / f"{name}.lean").write_text(f"def {name.lower()} : Nat := 1\n")
    return root


@pytest.fixture
def manager(tmp_path):
    return Runtime(
        home=tmp_path / "runtime", availability="local", initialize_store=False
    ).compositions


def test_preview_capture_alias_and_stale_sources(manager, tmp_path):
    a = project(tmp_path / "a", "Alpha")
    before = inventory(a)
    plan = manager.plan([a])
    assert plan["ready"]
    assert not manager.root.exists()
    result = manager.compose(plan, name="research")
    assert result["state"] == "sources_available"
    assert inventory(a) == before
    assert manager.resolve("research")[0] == plan["environment_id"]
    (a / "Alpha.lean").write_text("def alpha := 2\n")
    with pytest.raises(ProjectError, match="changed|stale"):
        manager.compose(plan)
    second = manager.compose(manager.plan([a]), name="research")
    assert second["environment_id"] != result["environment_id"]
    assert manager.resolve(result["environment_id"])[0] == result["environment_id"]


def test_exact_conflicts_and_ambiguity(manager, tmp_path):
    a = project(tmp_path / "a", "Alpha")
    b = project(tmp_path / "b", "Beta", toolchain="leanprover/lean4:v4.35.0")
    (b / "Alpha.lean").write_text("def other := 1\n")
    (b / "lakefile.toml").write_text(
        'name="beta"\n[[lean_lib]]\nname="Beta"\nroots=["Alpha", "Beta"]\n'
    )
    plan = manager.plan([a, b])
    assert {c["kind"] for c in plan["conflicts"]} == {"toolchain_conflict", "module_ambiguity"}
    with pytest.raises(ProjectError):
        manager.compose(plan)


def test_path_capture_and_cycle(manager, tmp_path):
    b = project(tmp_path / "b", "Beta")
    a = project(tmp_path / "a", "Alpha", packages=[{"type": "path", "name": "beta", "dir": "../b"}])
    plan = manager.plan([a])
    assert len(plan["lock"]["projects"]) == 2
    assert plan["ready"]
    (b / "lake-manifest.json").write_text(
        json.dumps(
            {"version": "1.1.0", "packages": [{"type": "path", "name": "alpha", "dir": "../a"}]}
        )
    )
    assert any(c["kind"] == "dependency_cycle" for c in manager.plan([a])["conflicts"])


def test_bounds_symlinks_and_git_policy(manager, tmp_path):
    a = project(tmp_path / "a", "Alpha")
    with pytest.raises(ProjectError, match="allowance"):
        manager.plan([a], max_source_bytes=10)
    subprocess.run(["git", "init", "-q", str(a)], check=True)
    (a / ".gitignore").write_text("ignored.bin\n")
    (a / "ignored.bin").write_bytes(b"x" * 100)
    assert not any(e["path"] == "ignored.bin" for e in inventory(a)["entries"])
    (a / "bad").symlink_to(a / "Alpha.lean")
    with pytest.raises(ProjectError, match="links|symlink"):
        manager.plan([a])


def test_portable_roundtrip_and_retained_references(manager, tmp_path):
    a = project(tmp_path / "a", "Alpha")
    env = manager.compose(manager.plan([a]), name="research")
    archive = tmp_path / "environment.zip"
    manager.export("research", archive)
    other = Runtime(
        home=tmp_path / "other", availability="local", initialize_store=False
    ).compositions
    assert not other.import_sources(archive)["apply"]
    assert not other.root.exists()
    imported = other.import_sources(archive, name="restored", apply=True)
    assert imported["environment_id"] == env["environment_id"]
    assert not other.clean()["paths"]
    other.references(alias="restored", apply=True)
    preview = other.clean()
    assert preview["paths"]
    assert other.list()
    other.clean(apply=True)
    assert not other.list()
    archive_bytes = archive.read_bytes()
    with pytest.raises(FileExistsError):
        manager.export("research", archive)
    assert archive.read_bytes() == archive_bytes


def test_corrupt_snapshot_and_registry_fail_closed(manager, tmp_path):
    a = project(tmp_path / "a", "Alpha")
    env = manager.compose(manager.plan([a]), name="research")
    source = manager._source(env["projects"][0]["source"]["source_id"])
    (source / "Alpha.lean").write_text("changed")
    with pytest.raises(ProjectError, match="snapshot|allowance"):
        manager.export("research", tmp_path / "bad.zip")
    (manager.root / "references.json").write_text("bad")
    with pytest.raises(EnvironmentError):
        manager.clean(apply=True)
    assert source.exists()


@pytest.mark.integration
def test_real_shared_build_init_checks_and_add_member(tmp_path, monkeypatch):
    elan = os.environ.get("LEAN_RUNTIME_TEST_ELAN")
    if not elan:
        pytest.skip("set LEAN_RUNTIME_TEST_ELAN to an existing full 4.33.1 toolchain root")
    import lean_runtime.storage_limits as accounting
    from lean_runtime.storage_limits import StorageLimits

    original_inventory = accounting.owned_bytes
    home = tmp_path / "runtime"

    def scoped_inventory(roots, **kwargs):
        assert home / "project-packages" not in roots, "raw build re-entered whole-store budgeting"
        assert home / "project-workspaces" not in roots, "prepare re-entered whole-store budgeting"
        return original_inventory(roots, **kwargs)

    monkeypatch.setattr(accounting, "owned_bytes", scoped_inventory)
    runtime = Runtime(
        home=home,
        availability="local",
        borrowed_elan_roots=[elan],
        storage_limits=StorageLimits(build_bytes=16_000_000, dependency_bytes=32_000_000),
    )
    manager = runtime.compositions
    roots = [project(tmp_path / name, name) for name in ["Alpha", "Beta", "Gamma"]]
    originals = [inventory(root) for root in roots]
    first = manager.compose(manager.plan(roots), name="research")
    assert manager.warm("research", imports=["Alpha", "Beta", "Gamma"]).ok
    artifacts = list((manager.root / "packages").rglob("*.olean"))
    assert artifacts
    times = {str(path): path.stat().st_mtime_ns for path in artifacts}
    assert manager.warm("research", imports=["Alpha", "Beta", "Gamma"]).ok
    assert times == {str(path): path.stat().st_mtime_ns for path in artifacts}
    target = tmp_path / "new"
    assert not manager.init_project("research", target)["apply"]
    assert not target.exists()
    manager.init_project("research", target, apply=True)
    (target / "Research.lean").write_text(
        "import Alpha\nimport Beta\nexample : alpha + beta = 2 := rfl\n"
    )
    assert runtime.build(target).ok
    assert runtime.check_file(target / "Research.lean").ok
    assert runtime.check_project(target).ok
    roots.append(project(tmp_path / "Delta", "Delta"))
    second = manager.compose(manager.plan(roots), name="research")
    assert second["environment_id"] != first["environment_id"]
    assert manager.warm("research", all_libraries=True).ok
    assert times == {str(path): path.stat().st_mtime_ns for path in artifacts}
    assert (
        json.loads((target / "lean-runtime.environment.json").read_text())["environment_id"]
        == first["environment_id"]
    )
    assert [inventory(root) for root in roots[:3]] == originals
    assert manager.warm("research", all_targets=True).ok
    assert manager.inspect("research")["state"] == "build_ready"
    from concurrent.futures import ThreadPoolExecutor

    sources = ["import Alpha\n#eval (11111 : Nat)", "import Beta\n#eval (22222 : Nat)"]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda source: manager.check("research", source), sources))
    assert all(result.ok for result in results)
    assert "11111" in results[0].stdout and "22222" not in results[0].stdout
    assert "22222" in results[1].stdout and "11111" not in results[1].stdout


def test_storage_scope_excludes_unrelated_objects_but_counts_staging(tmp_path):
    from lean_runtime.composition_storage import CompositionStorageScope
    from lean_runtime.storage_limits import StorageGuard, StorageLimitExceeded

    runtime = Runtime(home=tmp_path / "runtime", availability="local", initialize_store=False)
    unrelated = runtime.home / "project-packages" / "unrelated"
    unrelated.mkdir(parents=True)
    (unrelated / "existing").write_bytes(b"x" * 1000)
    scope = CompositionStorageScope(runtime, runtime.home / "compositions", {})
    assert not scope.covers(unrelated / "existing")
    stage = runtime.home / "project-packages" / ".new-stage"
    assert scope.covers(stage)
    with pytest.raises(StorageLimitExceeded), StorageGuard("dependency", scope, 10):
        stage.mkdir()
        (stage / "output").write_bytes(b"x" * 11)


def test_storage_inventory_cancel_and_overlapping_hardlinks(tmp_path):
    import threading

    from lean_runtime.storage_limits import StorageInventoryCancelled, owned_bytes

    root = tmp_path / "root"
    root.mkdir()
    source = root / "a"
    source.write_bytes(b"abc")
    os.link(source, root / "b")
    assert owned_bytes([root, root, source]) == 3
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(StorageInventoryCancelled):
        owned_bytes([root], cancel=cancel)


def test_executable_main_is_not_a_duplicate_library(manager, tmp_path):
    roots = [project(tmp_path / name, name) for name in ["Alpha", "Beta"]]
    for root in roots:
        (root / "Main.lean").write_text("def main : IO Unit := pure ()\n")
    assert manager.plan(roots)["ready"]


def test_manifest_and_configuration_disagree_without_upgrading(manager, tmp_path):
    root = project(tmp_path / "a", "Alpha")
    with (root / "lakefile.toml").open("a") as handle:
        handle.write(
            '\n[[require]]\nname="missing"\ngit="https://example.invalid/missing"\nrev="main"\n'
        )
    plan = manager.plan([root])
    assert not plan["ready"]
    assert any(c["kind"] == "unlocked_configuration_dependency" for c in plan["conflicts"])


def test_comparison_reports_source_change_without_repinning(manager, tmp_path):
    root = project(tmp_path / "a", "Alpha")
    before = manager.compose(manager.plan([root]), name="first")["environment_id"]
    (root / "Alpha.lean").write_text("def alpha := 2\n")
    after = manager.compose(manager.plan([root]), name="second")["environment_id"]
    comparison = manager.compare(before, after)
    assert not comparison.identity_equal
    assert any(change.path == "projects.alpha" for change in comparison.changes)
    assert manager.compare(before, "first").equal


def test_gc_reclaims_unreferenced_views_while_retaining_live_environment(manager, tmp_path):
    root = project(tmp_path / "a", "Alpha")
    env = manager.compose(manager.plan([root]), name="keep")
    _, directory, _ = manager.resolve(env["environment_id"])
    kept, orphan = "composition_package_" + "a" * 64, "composition_package_" + "b" * 64
    for name in [kept, orphan]:
        path = manager.root / "packages" / name
        path.mkdir(parents=True)
        (path / "output").write_bytes(b"bytes")
    (directory / "artifact-references.json").write_text(
        json.dumps(
            {
                "schema": "lean-runtime-composition-artifacts/1",
                "legacy_untracked": False,
                "buckets": {"environment": [kept]},
            }
        )
    )
    preview = manager.clean()
    assert str(manager.root / "packages" / orphan) in preview["paths"]
    assert str(manager.root / "packages" / kept) not in preview["paths"]
    manager.clean(apply=True)
    assert (manager.root / "packages" / kept).exists()
    assert not (manager.root / "packages" / orphan).exists()
    (directory / "artifact-references.json").write_text("bad")
    with pytest.raises(ProjectError, match="references"):
        manager.clean(apply=True)
    assert (manager.root / "packages" / kept).exists()


def test_composition_guard_covers_nested_build_and_dependency_budgets(tmp_path):
    from lean_runtime.storage_limits import StorageGuard, StorageLimits, budgeted

    class Owner:
        storage_limits = StorageLimits(build_bytes=1, dependency_bytes=1)

        @budgeted("build", lambda *_: pytest.fail("nested build must not scan whole store"))
        def build(self):
            return True

        @budgeted("dependency", lambda *_: pytest.fail("nested prepare must not scan whole store"))
        def prepare(self):
            return True

    with StorageGuard("build", [tmp_path], 1, categories=("dependency",)):
        assert Owner().build()
        assert Owner().prepare()


def test_selected_nested_package_scope_counts_custom_outputs(tmp_path):
    from lean_runtime.composition_storage import CompositionStorageScope
    from lean_runtime.storage_limits import owned_bytes

    runtime = Runtime(home=tmp_path / "runtime", availability="local", initialize_store=False)
    package = runtime.home / "project-packages" / "pkg_fixture"
    nested = package / "lean"
    nested.mkdir(parents=True)
    (package / "custom-output").write_bytes(b"12345")
    scope = CompositionStorageScope(runtime, runtime.home / "compositions", {"lib": nested})
    assert scope.covers(package / "custom-output")
    assert owned_bytes(scope()) == 5


@pytest.mark.integration
def test_real_captured_path_dependencies_survive_original_relocation(tmp_path):
    from lean_runtime.storage_limits import StorageLimits

    elan = os.environ.get("LEAN_RUNTIME_TEST_ELAN")
    if not elan:
        pytest.skip("set LEAN_RUNTIME_TEST_ELAN to an existing full 4.33.1 toolchain root")
    runtime = Runtime(
        home=tmp_path / "runtime",
        availability="local",
        borrowed_elan_roots=[elan],
        storage_limits=StorageLimits(build_bytes=16_000_000, dependency_bytes=32_000_000),
    )
    beta = project(tmp_path / "b", "Beta")
    alpha = project(
        tmp_path / "a", "Alpha", packages=[{"type": "path", "name": "beta", "dir": "../b"}]
    )
    with (alpha / "lakefile.toml").open("a") as handle:
        handle.write('\n[[require]]\nname="beta"\npath="../b"\n')
    (alpha / "Alpha.lean").write_text("import Beta\ndef alpha : Nat := beta + 1\n")
    manager = runtime.compositions
    manager.compose(manager.plan([alpha]), name="paths")
    alpha.rename(tmp_path / "original-a")
    beta.rename(tmp_path / "original-b")
    target = tmp_path / "new"
    manager.init_project("paths", target, apply=True)
    (target / "Research.lean").write_text("import Alpha\nexample : alpha = 2 := rfl\n")
    result = runtime.build(target)
    assert result.ok, result.stdout + result.stderr
    assert runtime.check_file(target / "Research.lean").ok
