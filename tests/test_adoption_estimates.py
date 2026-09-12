"""Costs describe operations, with explicit coverage and evidence."""

import hashlib
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_adoption_safety import identity, setup_project
from test_published_estimates import sample

from lean_runtime._adoption_estimates import AdoptionEstimates, CostComponent
from lean_runtime.cli import _render_adoption_plan
from lean_runtime.oci import missing_capsule_frames
from lean_runtime.packs import PackFrame
from lean_runtime.project_sharing import plan_adoption
from lean_runtime.store import tree_usage


def component(category, size, *, action="a", basis="measured_local"):
    return CostComponent(category, (action,), category, "logical_bytes", basis, size)


def test_signed_growth_and_physical_unknown():
    report = AdoptionEstimates(
        (component("local_replacement", 10), component("package_source_trees", 18))
    )
    assert report.net_logical_change == 8
    assert report.to_dict()["physical_disk_change_bytes"] is None
    assert report.to_dict()["additional_working_space_bytes"] is None


def test_no_mixed_quantities_or_overlapping_coverage():
    measured = component("preserved_artifacts", 10)
    for other in (
        replace(measured, basis="related_publication"),
        replace(measured, quantity="payload_bytes"),
    ):
        with pytest.raises(ValueError):
            _ = AdoptionEstimates((measured, other)).new_retained


def test_samples_never_become_known_storage():
    report = AdoptionEstimates((component("restored_artifacts", 999, basis="related_publication"),))
    assert report.new_retained.known_bytes == 0
    assert not report.new_retained.complete


def test_59_mathlib_variants_are_not_a_storage_forecast(tmp_path, capsys):
    runtime, context, _ = setup_project(tmp_path)
    plan = runtime.plan_project_adoption(context.root)
    references = tuple(
        {"requested_package": "mathlib", "sample": sample(artifact_bytes=5 * 1024**3).to_dict()}
        for _ in range(59)
    )
    plan = replace(plan, size_references=references)
    _render_adoption_plan(plan)
    output = capsys.readouterr().out
    assert "295" not in output
    assert "Published check-artifact size reference:" not in output
    assert plan.to_dict()["approximate_artifact_bytes"] is None


def test_source_only_adoption_does_not_consult_artifact_samples(tmp_path):
    runtime, context, _ = setup_project(tmp_path)

    def forbidden():
        raise AssertionError("source-only work has no artifact size requirement")

    plan = plan_adoption(
        context.root, recursive=False, shared=runtime.shared_projects, size_samples=forbidden
    )
    assert plan.actions[0].kind == "import_local"
    assert plan.actions[0].artifact_decision == "none"
    assert not plan.size_references


def test_preservation_prices_only_eligible_outputs(tmp_path):
    import json

    runtime, context, entries = setup_project(tmp_path)
    local = context.root / ".lake/packages/dep"
    build = local / ".lake/build/lib/lean"
    build.mkdir(parents=True)
    (build / "Dep.olean").write_bytes(b"12345")
    (build / "Dep.trace").write_bytes(b"x" * 1000)
    (local / ".git/info/exclude").write_text(".lake/\n/.lean-runtime-package.json\n")
    (local / ".lean-runtime-package.json").write_text(
        json.dumps(identity(runtime, context, entries))
    )
    plan = runtime.plan_project_adoption(context.root)
    assert plan.actions[0].artifact_decision == "preserve"
    assert plan.estimates.summary({"preserved_artifacts"}).known_bytes == 5
    assert runtime.attach_projects(context.root).ok
    assert (build / "Dep.olean").read_bytes() == b"12345"
    assert not (build / "Dep.trace").exists()


def test_inventory_hardlinks_links_and_failed_scan(tmp_path, monkeypatch):
    root = tmp_path / "tree"
    root.mkdir()
    original = root / "a"
    original.write_bytes(b"abc")
    os.link(original, root / "b")
    (root / "external").symlink_to(tmp_path, target_is_directory=True)
    usage = tree_usage(root)
    assert usage.logical_bytes == 6 and usage.files == 2 and usage.complete
    assert usage.allocated_bytes == original.stat().st_blocks * 512
    original_scan = os.scandir

    def scan(path):
        if path == root:
            raise PermissionError("unreadable")
        return original_scan(path)

    monkeypatch.setattr(os, "scandir", scan)
    assert not tree_usage(root).complete


def test_missing_sparse_frame_selection_deduplicates_and_revalidates(tmp_path):
    data = b"compiled"
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    artifact = SimpleNamespace(digest=digest, size=len(data))
    frame = PackFrame(32, 15, len(data), "sha256:frame", ("Dep",), ("Dep.olean",))
    descriptor = {"digest": "sha256:blob"}
    frames = ((None, descriptor, frame),) * 2
    assert len(missing_capsule_frames(frames, {"Dep.olean": artifact}, tmp_path)) == 1
    target = tmp_path / digest.removeprefix("sha256:")
    target.write_bytes(data)
    assert missing_capsule_frames(frames, {"Dep.olean": artifact}, tmp_path) == ()
    target.write_bytes(b"corrupt!")
    assert len(missing_capsule_frames(frames, {"Dep.olean": artifact}, tmp_path)) == 1


@pytest.mark.parametrize("detach", [False, True])
def test_backup_cleanup_failure_does_not_rollback_committed_transaction(
    tmp_path, monkeypatch, detach
):
    from lean_runtime import project_sharing as sharing

    runtime, context, _ = setup_project(tmp_path)
    if detach:
        assert runtime.attach_projects(context.root).ok
    remove = sharing._remove_path

    def fail_backup(path):
        if "backup" in path.name:
            # Simulate a partially deleted backup; restoration would be unsafe.
            remove(path)
            raise OSError("cleanup failed after deletion")
        remove(path)

    monkeypatch.setattr(sharing, "_remove_path", fail_backup)
    if detach:
        runtime.project_adopter.detach(context, probe=lambda _: None)
        assert not (context.root / ".lake/packages/dep").is_symlink()
        assert not (context.root / ".lake/lean-runtime-attachment.json").exists()
    else:
        assert runtime.attach_projects(context.root).ok
        assert (context.root / ".lake/packages/dep").is_symlink()
        assert (context.root / ".lake/lean-runtime-attachment.json").exists()


def test_component_coverage_refers_to_real_operations(tmp_path):
    runtime, context, _ = setup_project(tmp_path)
    plan = runtime.plan_project_adoption(context.root)
    actions = {a.action_id for a in plan.actions}
    assert all(set(c.action_ids) <= actions for c in plan.estimates.components)


@pytest.mark.parametrize("requested", [True, False, 1.5, "2"])
def test_jobs_rejects_non_integer_python_arguments(requested):
    from lean_runtime._adoption_workers import adoption_jobs
    from lean_runtime.errors import ProjectError

    with pytest.raises(ProjectError):
        adoption_jobs(requested)


def test_source_object_shared_across_distinct_compatibility_variants(tmp_path):
    import json
    import shutil

    runtime, context, _ = setup_project(tmp_path)
    one = runtime.plan_project_adoption(context.root)
    second = tmp_path / "second"
    shutil.copytree(context.root, second)
    # Same revision can carry a different Git object inventory. Execution uses
    # the first source object for both package trees, not both local inventories.
    (second / ".lake/packages/dep/.git/extra-inventory").write_bytes(b"x" * 10000)
    path_dep = tmp_path / "extra"
    path_dep.mkdir()
    (path_dep / "Extra.lean").write_text("def extra := 1")
    manifest_path = second / "lake-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["packages"].append({"name": "extra", "type": "path", "dir": str(path_dep)})
    manifest_path.write_text(json.dumps(manifest))
    both = runtime.plan_project_adoption(tmp_path, recursive=True)
    assert both.new_source_bytes == one.new_source_bytes
    assert both.estimates.summary({"package_source_trees"}).known_bytes == (
        2 * one.estimates.summary({"package_source_trees"}).known_bytes
    )
    assert len([a for a in both.actions if a.package == "dep"]) == 2
    components = [c for c in both.estimates.components if c.category == "source_objects"]
    assert len(components) == 1 and len(components[0].action_ids) == 2


def test_missing_inventory_is_not_measured_as_zero(tmp_path):
    assert not tree_usage(tmp_path / "vanished").complete
    assert tree_usage(tmp_path / "absent-local-directory", missing_ok=True).complete


def test_git_inventory_is_scoped_to_one_plan(monkeypatch, tmp_path):
    from lean_runtime import shared_projects as shared

    state = ["first"]
    calls = []

    def head(path):
        calls.append(path)
        return state[0]

    monkeypatch.setattr(shared, "git_head", head)
    first = shared.SourceSelectionInventory()
    assert first.head(tmp_path) == "first"
    state[0] = "second"
    assert first.head(tmp_path) == "first"
    assert len(calls) == 1
    assert shared.SourceSelectionInventory().head(tmp_path) == "second"
    assert len(calls) == 2


def test_unrelated_registered_source_is_not_hashed(tmp_path, monkeypatch):
    from test_projects import ProjectToolchains, _remembered_package_pair

    from lean_runtime import Runtime, discover_project, shared_projects
    from lean_runtime.project_sharing import _manifest_packages

    producer, consumer_source, _ = _remembered_package_pair(tmp_path)
    runtime = Runtime(toolchains=ProjectToolchains(tmp_path / "runtime"), libraries=[])
    runtime.shared_projects.remember_project(discover_project(producer))
    context = discover_project(consumer_source)
    entries = _manifest_packages(context)
    entries[0]["rev"] = "f" * 40

    def forbidden(*args, **kwargs):
        raise AssertionError("unrelated source must be rejected before path hashing")

    monkeypatch.setattr(shared_projects, "resolved_path_entries", forbidden)
    assert (
        runtime.shared_projects.registered_package_seeds(context, entries, effective_entries={})
        == {}
    )


def test_duplicate_package_action_keeps_first_donor_and_decision(tmp_path):
    import shutil

    runtime, context, _ = setup_project(tmp_path)
    second = tmp_path / "second"
    shutil.copytree(context.root, second)
    local = second / ".lake/packages/dep"
    build = local / ".lake/build/lib/lean"
    build.mkdir(parents=True)
    (build / "Dep.olean").write_bytes(b"unprovenanced second donor")
    (local / ".git/info/exclude").write_text(".lake/\n")
    plan = runtime.plan_project_adoption(tmp_path, recursive=True)
    packages = [a for a in plan.actions if a.package == "dep"]
    assert len(packages) == 1
    assert packages[0].kind == "import_local"
    assert packages[0].artifact_decision == "none"
    assert packages[0].source_path == str(context.root / ".lake/packages/dep")
    assert len(packages[0].projects) == 2


def test_unresolved_earlier_action_does_not_promise_a_source(tmp_path, monkeypatch):
    import shutil

    runtime, context, _ = setup_project(tmp_path)
    second = tmp_path / "second"
    shutil.copytree(context.root, second)
    (second / "lean-toolchain").write_text("leanprover/lean4:v4.33.1\n")
    shutil.rmtree(second / ".lake/packages")
    lookup = runtime.shared_projects.local_toolchain_build_identity
    monkeypatch.setattr(
        runtime.shared_projects,
        "local_toolchain_build_identity",
        lambda tc: None if tc == context.toolchain else lookup(tc),
    )
    plan = runtime.plan_project_adoption(tmp_path, recursive=True)
    action = next(a for a in plan.actions if a.package == "dep" and str(second) in a.projects)
    assert action.kind == "fetch_source"
    assert any(
        c.category == "git_transfer" and action.action_id in c.action_ids
        for c in plan.estimates.components
    )
    assert not plan.estimates.network.complete
