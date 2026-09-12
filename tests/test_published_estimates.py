"""Different-version size samples must never become artifact compatibility."""

from dataclasses import replace
from types import SimpleNamespace

from test_adoption_safety import setup_project

from lean_runtime._published_estimates import PublishedPackageSize, select_size_reference
from lean_runtime.cli import _render_adoption_plan
from lean_runtime.project_sharing import plan_adoption


def sample(**changes):
    return replace(
        PublishedPackageSize(
            "dep",
            "https://example.org/dep.git",
            "a" * 40,
            None,
            "leanprover/lean4:v4.30.0-rc2",
            12345,
            4321,
            "oci://example.org/cache",
            "lock_sample",
            "sha256:" + "b" * 64,
        ),
        **changes,
    )


def test_prefer_exact_revision_and_never_match_unrelated_repository():
    old = sample()
    exact = sample(revision="c" * 40)
    entry = {"url": "https://example.org/dep", "rev": "c" * 40}
    assert select_size_reference((old, exact), entry, old.toolchain) == exact
    assert select_size_reference((old,), entry, old.toolchain) == old
    assert (
        select_size_reference((old,), {**entry, "url": "https://other.org/dep"}, old.toolchain)
        is None
    )
    assert select_size_reference((old,), {**entry, "subDir": "different"}, old.toolchain) is None


def test_reference_does_not_authorize_reuse_or_complete_estimate(tmp_path, capsys, monkeypatch):
    runtime, context, entries = setup_project(tmp_path)
    build = context.root / ".lake/packages/dep/.lake/build/lib/lean"
    build.mkdir(parents=True)
    (build / "Dep.olean").write_bytes(b"unknown provenance")
    monkeypatch.setattr(runtime.shared_projects, "local_toolchain_build_identity", lambda _: None)
    baseline = plan_adoption(context.root, recursive=False, shared=runtime.shared_projects, jobs=1)
    reference = sample(url=entries[0]["url"])
    calls = []

    def load():
        calls.append(True)
        return (reference,)

    estimated = plan_adoption(
        context.root, recursive=False, shared=runtime.shared_projects, jobs=1, size_samples=load
    )
    assert calls == [True]
    assert estimated.shared_bytes_reused == baseline.shared_bytes_reused
    assert estimated.new_shared_bytes == baseline.new_shared_bytes
    assert estimated.storage_estimate_complete == baseline.storage_estimate_complete
    assert estimated.download_bytes == baseline.download_bytes
    assert estimated.unknown_dependencies == baseline.unknown_dependencies
    assert estimated.size_references[0]["different_revision"]
    assert estimated.to_dict()["approximate_artifact_bytes"] is None
    _render_adoption_plan(estimated)
    short = capsys.readouterr().out
    assert "excluded from storage and transfer totals" in short
    assert reference.revision not in short
    _render_adoption_plan(estimated, verbose=True)
    assert reference.revision in capsys.readouterr().out


def test_metadata_lookup_never_downloads_packs(monkeypatch):
    from lean_runtime import _published_estimates as estimates

    package = SimpleNamespace(
        name="dep", url="https://example.org/dep", revision="a" * 40, subdir=None
    )
    lock = SimpleNamespace(packages=(package,), lock_id="lock_sample")
    entry = SimpleNamespace(lock=lock, toolchain="lean4:v1", created_timestamp=1)
    artifact = SimpleNamespace(path="Dep.olean", capability="check", size=20)
    module = SimpleNamespace(package="dep", artifacts=(artifact,))
    pack = SimpleNamespace(package="dep", capability="check", size=10)

    class Library:
        repository = SimpleNamespace(display="oci://example.org/cache")

        def plan_capsule(self, selected, roots, *, capabilities):
            assert selected is lock
            assert roots == () and capabilities == frozenset()
            return SimpleNamespace(
                capsule=SimpleNamespace(modules=(module,)),
                packs=((pack, {}),),
                config_descriptor={"digest": "sha256:x"},
            )

    monkeypatch.setattr(estimates, "_catalog", lambda: SimpleNamespace(entries=(entry,)))
    sizes = estimates.published_package_sizes((Library(),))
    assert sizes[0].artifact_bytes == 20
    assert sizes[0].packed_bytes == 10


def test_repeated_requested_graph_counts_reference_once(tmp_path, monkeypatch):
    import shutil

    runtime, context, entries = setup_project(tmp_path)
    build = context.root / ".lake/packages/dep/.lake/build/lib/lean"
    build.mkdir(parents=True)
    (build / "Dep.olean").write_bytes(b"unknown provenance")
    monkeypatch.setattr(runtime.shared_projects, "local_toolchain_build_identity", lambda _: None)
    second = context.root.parent / "copy"
    shutil.copytree(context.root, second)
    reference = sample(url=entries[0]["url"])
    plan = plan_adoption(
        context.root.parent,
        recursive=True,
        shared=runtime.shared_projects,
        jobs=2,
        size_samples=lambda: (reference,),
    )
    assert len(plan.size_references) == 1
    assert len(plan.size_references[0]["projects"]) == 2
    assert plan.to_dict()["approximate_artifact_bytes"] is None


def test_local_only_planning_does_not_query_registry(tmp_path, monkeypatch):
    from lean_runtime import _published_estimates as estimates

    runtime, context, _ = setup_project(tmp_path)
    runtime.availability = "local"
    runtime.libraries = (object(),)

    def forbidden(*args):
        raise AssertionError("local planning must not query registry")

    monkeypatch.setattr(estimates, "published_package_sizes", forbidden)
    assert runtime.plan_project_adoption(context.root).size_references == ()
