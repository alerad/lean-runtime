"""Regressions for reuse keys and migration; no claim about Lean verdicts."""

import json
import shutil

import pytest
from test_projects import ProjectToolchains, _shared_project

from lean_runtime import Runtime, discover_project
from lean_runtime._paths import remove_tree
from lean_runtime._project_identity import compute_package_identity, resolved_path_entries
from lean_runtime.package_ids import package_directory_id


def unlink_directory_link(path):
    # Junctions are directories on Windows; never descend into their targets.
    if path.is_symlink():
        path.unlink()
    else:
        path.rmdir()


def setup_project(tmp_path):
    source, _ = _shared_project(tmp_path / "first", tmp_path / "dependency")
    runtime = Runtime(toolchains=ProjectToolchains(tmp_path / "runtime"), libraries=[])
    context = discover_project(source)
    entries = json.loads(context.current_manifest().read_text())["packages"]
    return runtime, context, entries


def identity(runtime, context, entries):
    effective = {e["name"]: e for e in resolved_path_entries(context, entries)}
    return compute_package_identity(
        context=context,
        entry=entries[0],
        source_package=context.root / ".lake/packages/dep" / (entries[0].get("subDir") or ""),
        effective_entries=effective,
        toolchain_identity=runtime.shared_projects._build_identity(context.toolchain, None),
    )


def test_override_transitive_change_invalidates_both_keys(tmp_path):
    runtime, context, entries = setup_project(tmp_path)
    # A's stored manifest knows only B; the effective overridden B adds C.
    stored_manifest = context.root / ".lake/packages/dep/lake-manifest.json"
    stored_manifest.write_text(json.dumps({"packages": [{"name": "B"}]}))
    entries += [
        {"name": n, "type": "git", "url": f"https://example.invalid/{n}", "rev": "1" * 40}
        for n in ("B", "C")
    ]
    first = identity(runtime, context, entries)
    entries[-1]["rev"] = "2" * 40
    second = identity(runtime, context, entries)
    assert package_directory_id(first) != package_directory_id(second)
    assert first["artifact_key"] != second["artifact_key"]


@pytest.mark.parametrize("manifest_present", [True, False])
def test_graph_order_and_path_content(tmp_path, manifest_present):
    runtime, context, entries = setup_project(tmp_path)
    if not manifest_present:
        (context.root / ".lake/packages/dep/lake-manifest.json").unlink()
    local = tmp_path / "local"
    local.mkdir()
    content = local / "Local.lean"
    content.write_text("def x := 1")
    entries.append({"name": "local", "type": "path", "dir": str(local)})
    first = identity(runtime, context, entries)
    effective = {e["name"]: e for e in reversed(resolved_path_entries(context, entries))}
    reordered = compute_package_identity(
        context=context,
        entry=entries[0],
        source_package=context.root / ".lake/packages/dep",
        effective_entries=effective,
        toolchain_identity=runtime.shared_projects._build_identity(context.toolchain, None),
    )
    assert first == reordered
    content.write_text("def x := 2")
    second = identity(runtime, context, entries)
    assert first["artifact_key"] != second["artifact_key"]
    assert package_directory_id(first) != package_directory_id(second)


@pytest.mark.parametrize("compatible", [True, False])
@pytest.mark.parametrize("subdir", ["", "nested/package"])
def test_first_adoption_migrates_only_proven_artifacts(tmp_path, compatible, subdir):
    runtime, context, entries = setup_project(tmp_path)
    local = context.root / ".lake/packages/dep"
    if subdir:
        import subprocess

        from lean_runtime._git import git_command

        nested = local / subdir
        nested.mkdir(parents=True)
        for name in ("lakefile.toml", "lake-manifest.json", "Dep.lean"):
            (local / name).rename(nested / name)
        subprocess.run(git_command("-C", str(local), "add", "."), check=True)
        subprocess.run(
            git_command(
                "-C",
                str(local),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-qm",
                "subdir",
            ),
            check=True,
        )
        entries[0]["subDir"] = subdir
        entries[0]["rev"] = subprocess.check_output(
            git_command("-C", str(local), "rev-parse", "HEAD"), text=True
        ).strip()
        manifest = json.loads(context.current_manifest().read_text())
        manifest["packages"] = entries
        context.current_manifest().write_text(json.dumps(manifest))
    build = local / subdir / ".lake/build/lib/lean"
    build.mkdir(parents=True)
    (build / "Dep.olean").write_bytes(b"compatible fixture")
    (build / "Dep.trace").write_text("old absolute paths")
    unsupported = local / subdir / ".lake/build/local-config.json"
    unsupported.write_text("local configuration")
    (local / ".git/info/exclude").write_text(".lake/\n/.lean-runtime-package.json\n")
    marker = identity(runtime, context, entries)
    if not compatible:
        marker["artifact_key"]["lean_executable_digest"] = "sha256:wrong"
    (local / ".lean-runtime-package.json").write_text(json.dumps(marker))
    assert runtime.attach_projects(context.root).ok
    assert (build / "Dep.olean").exists() == compatible
    assert not (build / "Dep.trace").exists()
    assert not unsupported.exists()


def test_batch_revalidates_links_even_with_existing_plan(tmp_path):
    runtime, context, entries = setup_project(tmp_path)
    shutil.copytree(context.root, tmp_path / "second")
    assert runtime.attach_projects(tmp_path, recursive=True).ok
    plan = runtime.plan_project_adoption(tmp_path, recursive=True)
    link = context.root / ".lake/packages/dep"
    unlink_directory_link(link)
    from lean_runtime._paths import link_directory

    target = tmp_path / "missing"
    target.mkdir()
    link_directory(target, link)
    target.rmdir()
    result = runtime.attach_projects(tmp_path, recursive=True, plan=plan)
    assert result.ok
    assert link.is_dir()


def test_plan_reports_reuse_and_unknown_acquisition(tmp_path):
    runtime, context, entries = setup_project(tmp_path)
    second = tmp_path / "second"
    shutil.copytree(context.root, second)
    assert runtime.attach_projects(context.root).ok
    plan = runtime.plan_project_adoption(second)
    assert plan.shared_bytes_reused > 0
    assert plan.new_shared_bytes == 0
    remove_tree(second / ".lake/packages/dep")
    # Exact retained content still supplies known costs without a local copy.
    assert runtime.plan_project_adoption(second).shared_bytes_reused > 0
    runtime.toolchains.executable_digests["lean"] = "sha256:changed"
    remove_tree(runtime.shared_projects.sources)
    missing = runtime.plan_project_adoption(second)
    assert not missing.storage_estimate_complete
    assert missing.estimated_machine_reclaimable_bytes is None


def test_planning_never_installs_missing_toolchain(tmp_path, monkeypatch):
    runtime, context, entries = setup_project(tmp_path)
    monkeypatch.setattr(runtime.toolchains, "is_available_locally", lambda _: False)

    def forbidden(*args, **kwargs):
        pytest.fail("planning attempted toolchain installation or identity acquisition")

    monkeypatch.setattr(runtime.toolchains, "ensure_full", forbidden)
    monkeypatch.setattr(runtime.toolchains, "executable_digest", forbidden)
    plan = runtime.plan_project_adoption(context.root)
    assert not plan.storage_estimate_complete
    assert plan.unknown_dependencies
    assert plan.new_shared_bytes == 0


def test_legacy_keys_cannot_authorize_artifact_reuse(tmp_path):
    from lean_runtime._project_identity import normalized_package_identity

    runtime, context, entries = setup_project(tmp_path)
    marker = identity(runtime, context, entries)
    marker["schema"] = "lean-runtime-shared-project/3"
    marker["artifact_key"]["schema"] = "lean-runtime-package-artifact-key/2"
    assert normalized_package_identity(marker) is None
    local = context.root / ".lake/packages/dep"
    (local / ".lean-runtime-package.json").write_text(json.dumps(marker))
    assert (
        runtime.shared_projects.artifact_donor_rejection(
            context,
            entries[0],
            local,
            {e["name"]: e for e in entries},
            runtime.shared_projects._build_identity(context.toolchain, None),
        )
        is not None
    )


def test_failed_preparation_keeps_original_artifacts(tmp_path, monkeypatch):
    from lean_runtime import ProjectError
    from lean_runtime._paths import is_link

    runtime, context, entries = setup_project(tmp_path)
    local = context.root / ".lake/packages/dep"
    build = local / ".lake/build/lib/lean"
    build.mkdir(parents=True)
    (build / "Dep.olean").write_bytes(b"original")
    (local / ".git/info/exclude").write_text("/.lake/\n")
    (local / ".lean-runtime-package.json").write_text(
        json.dumps(identity(runtime, context, entries))
    )

    def reject(*args, **kwargs):
        raise ProjectError("preparation validation failed")

    monkeypatch.setattr(runtime, "_probe_project_graph", reject)
    result = runtime.attach_projects(context.root)
    assert not result.ok
    assert not is_link(local)
    assert (build / "Dep.olean").read_bytes() == b"original"


@pytest.mark.parametrize(
    "damage", ["wrong_target", "missing_target", "dirty_directory", "toolchain"]
)
def test_batch_attachment_damage(tmp_path, damage):
    from lean_runtime._paths import link_directory

    runtime, context, entries = setup_project(tmp_path)
    shutil.copytree(context.root, tmp_path / "second")
    assert runtime.attach_projects(tmp_path, recursive=True).ok
    plan = runtime.plan_project_adoption(tmp_path, recursive=True)
    link = context.root / ".lake/packages/dep"
    expected = link.resolve()
    if damage == "toolchain":
        (context.root / "lean-toolchain").write_text("leanprover/lean4:v4.33.0\n")
    else:
        unlink_directory_link(link)
        if damage == "dirty_directory":
            shutil.copytree(expected, link)
            (link / "Dep.lean").write_text("def userWork := 42")
        else:
            target = tmp_path / "wrong"
            target.mkdir()
            link_directory(target, link)
            if damage == "missing_target":
                target.rmdir()
    result = runtime.attach_projects(tmp_path, recursive=True, plan=plan)
    if damage == "dirty_directory":
        assert not result.ok
        assert (link / "Dep.lean").read_text() == "def userWork := 42"
    else:
        assert result.ok
        assert link.is_dir()
        assert (link.resolve() != expected) == (damage == "toolchain")


def test_new_storage_is_deduplicated_by_effective_identity(tmp_path):
    runtime, context, entries = setup_project(tmp_path)
    one = runtime.plan_project_adoption(context.root)
    shutil.copytree(context.root, tmp_path / "second")
    both = runtime.plan_project_adoption(tmp_path, recursive=True)
    assert both.new_shared_bytes == one.new_shared_bytes
    assert both.checkout_bytes_removed == 2 * one.checkout_bytes_removed


def test_real_local_toolchain_lookup_never_calls_installers(tmp_path, monkeypatch):
    import hashlib

    from lean_runtime.toolchains import ToolchainManager

    manager = ToolchainManager(tmp_path / "runtime")
    full = tmp_path / "full"
    monkeypatch.setattr(manager, "_full_toolchain_dir", lambda _: full)

    def forbidden(*args, **kwargs):
        pytest.fail("local lookup called an installing API")

    for name in ("ensure", "ensure_full", "executable_digest"):
        monkeypatch.setattr(manager, name, forbidden)
    assert manager.local_build_identity("v4.33.0") is None
    for executable in ("lean", "lake"):
        binary = manager._binary(full, executable)
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(executable.encode())
    result = manager.local_build_identity("v4.33.0")
    assert result is not None
    assert result.lean_executable_digest == "sha256:" + hashlib.sha256(b"lean").hexdigest()
    assert result.lake_executable_digest == "sha256:" + hashlib.sha256(b"lake").hexdigest()
    assert not manager.home.exists()


def test_retained_workspace_cannot_substitute_another_graph(tmp_path):
    from pathlib import Path

    runtime, context, entries = setup_project(tmp_path)
    assert runtime.attach_projects(context.root).ok
    first_link = context.root / ".lake/packages/dep"
    first_target = first_link.resolve()
    first_attachment = json.loads((context.root / ".lake/lean-runtime-attachment.json").read_text())
    record_dir = runtime.shared_projects.root / first_attachment["workspace_id"]
    second_root = tmp_path / "second"
    shutil.copytree(context.root, second_root, symlinks=True)
    (second_root / "lean-toolchain").write_text("leanprover/lean4:v4.33.0\n")
    assert runtime.attach_projects(second_root).ok
    second_attachment = json.loads((second_root / ".lake/lean-runtime-attachment.json").read_text())
    wrong_dir = runtime.shared_projects.root / second_attachment["workspace_id"]
    # Keep the first workspace's identity but replace its package inventory and overrides.
    record = json.loads((record_dir / "workspace.json").read_text())
    record["package_ids"] = json.loads((wrong_dir / "workspace.json").read_text())["package_ids"]
    (record_dir / "workspace.json").write_text(json.dumps(record))
    (record_dir / "package-overrides.json").write_bytes(
        (wrong_dir / "package-overrides.json").read_bytes()
    )
    plan = runtime.plan_project_adoption(context.root)
    assert not plan.projects[0].attached
    assert runtime.attach_projects(context.root).ok
    assert first_link.resolve() == first_target
    overrides = json.loads((record_dir / "package-overrides.json").read_text())
    assert Path(overrides["packages"][0]["dir"]) == first_target


def test_plan_prices_retained_source_without_local_checkout(tmp_path):
    runtime, context, entries = setup_project(tmp_path)
    assert runtime.attach_projects(context.root).ok
    runtime.toolchains.executable_digests["lean"] = "sha256:changed"
    local = context.root / ".lake/packages/dep"
    unlink_directory_link(local)
    plan = runtime.plan_project_adoption(context.root)
    assert not plan.storage_estimate_complete  # Generated workspace metadata is unpriced.
    assert plan.estimates.summary({"package_source_trees"}).complete
    assert plan.new_shared_bytes > 0
    assert plan.new_source_bytes == 0
    assert plan.download_bytes == 0


def test_project_verification_uses_attachment_validator(tmp_path):
    runtime, context, entries = setup_project(tmp_path)
    assert runtime.attach_projects(context.root).ok
    report = runtime.verify(context.root, offline=True)
    assert report.ok
    assert report.subject_kind == "project"
    assert report.lock_id is None
    link = context.root / ".lake/packages/dep"
    unlink_directory_link(link)
    broken = runtime.verify(context.root, offline=True)
    assert not broken.ok
    assert broken.failures[0].code == "project_attachment_current"
    assert not link.exists()  # Verification does not repair or acquire anything.


def test_source_cache_does_not_authorize_artifact_migration(tmp_path):
    runtime, context, entries = setup_project(tmp_path)
    runtime.prepare_shared_project(context.root)
    source = runtime.shared_projects.cached_source(entries[0])
    assert source is not None
    stale = source / ".lake/build/lib/lean"
    stale.mkdir(parents=True)
    (stale / "Dep.olean").write_bytes(b"unproven source-cache artifact")
    (source / ".git/info/exclude").write_text("/.lake/\n")
    remove_tree(runtime.shared_projects.packages)
    remove_tree(runtime.shared_projects.root)
    assert runtime.attach_projects(context.root).ok
    assert not (context.root / ".lake/packages/dep/.lake/build/lib/lean/Dep.olean").exists()


def test_modified_managed_package_is_not_reused_or_overwritten(tmp_path):
    runtime, context, entries = setup_project(tmp_path)
    assert runtime.attach_projects(context.root).ok
    modified = context.root / ".lake/packages/dep/Dep.lean"
    modified.write_text("def userChange := 99")
    assert not runtime.verify(context.root, offline=True).ok
    result = runtime.attach_projects(context.root)
    assert not result.ok
    assert modified.read_text() == "def userChange := 99"
