from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import make_entry, make_lock

from lean_runtime import Runtime
from lean_runtime.cli import main
from lean_runtime.discovery import Catalog
from lean_runtime.errors import ProjectError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import catalog_update  # noqa: E402


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Runtime:
    selected = Runtime(home=tmp_path / "home")
    catalog = Catalog(
        generated_at="2026-10-01T00:00:00Z",
        entries=(
            make_entry("mathlib-v4.33.1", "a", packages=("mathlib",)),
            # Published after the release: only present through the remote catalog.
            make_entry("mathlib-v4.34.1", "b", packages=("mathlib",)),
        ),
    )
    monkeypatch.setattr(selected, "catalog", lambda: catalog)
    return selected


def test_latest_follows_the_effective_catalog(runtime: Runtime) -> None:
    assert runtime._mathlib_catalog_entry("latest").id == "mathlib-v4.34.1"
    assert runtime._mathlib_catalog_entry("v4.33.1").id == "mathlib-v4.33.1"


def test_uncataloged_release_requires_explicit_opt_in(runtime: Runtime) -> None:
    with pytest.raises(ProjectError, match="--uncataloged"):
        runtime._mathlib_catalog_entry("4.35.0")
    with pytest.raises(ProjectError, match="explicit --mathlib"):
        runtime._mathlib_catalog_entry("latest", uncataloged=True)


def test_uncataloged_release_resolves_once_from_its_tag(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    references: list[object] = []
    lock = make_lock("c", toolchain="leanprover/lean4:v4.35.0", packages=("mathlib",))
    monkeypatch.setattr(runtime, "spec_from_references", lambda refs: references.append(refs))
    monkeypatch.setattr(runtime, "prepare", lambda _spec: lock)

    first = runtime._mathlib_catalog_entry("4.35.0", uncataloged=True)
    second = runtime._mathlib_catalog_entry("4.35.0", uncataloged=True)

    assert first is second
    assert references == [["github:leanprover-community/mathlib4@v4.35.0"]]
    assert (first.id, first.channel, first.toolchain) == (
        "mathlib-v4.35.0",
        "uncataloged",
        "leanprover/lean4:v4.35.0",
    )
    # A cataloged release never takes the uncataloged path.
    assert runtime._mathlib_catalog_entry("4.34.1", uncataloged=True).channel == "stable"


def test_uncataloged_resolution_never_downloads_the_mathlib_cache(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MATHLIB_NO_CACHE_ON_UPDATE", raising=False)
    observed: list[str | None] = []
    lock = make_lock("c", toolchain="leanprover/lean4:v4.35.0", packages=("mathlib",))
    monkeypatch.setattr(runtime, "spec_from_references", lambda refs: refs)

    def prepare(_spec: object) -> object:
        observed.append(os.environ.get("MATHLIB_NO_CACHE_ON_UPDATE"))
        return lock

    monkeypatch.setattr(runtime, "prepare", prepare)
    runtime._mathlib_catalog_entry("4.35.0", uncataloged=True)
    assert observed == ["1"]
    assert "MATHLIB_NO_CACHE_ON_UPDATE" not in os.environ


def test_uncataloged_release_is_refused_offline(tmp_path: Path) -> None:
    offline = Runtime(home=tmp_path / "home", availability="local")
    with pytest.raises(ProjectError, match="offline"):
        offline._uncataloged_mathlib_entry("4.35.0")


@pytest.mark.parametrize(
    "arguments",
    [["--core", "--mathlib", "4.33.0"], ["--core", "--uncataloged"], ["--uncataloged"]],
)
def test_new_rejects_contradictory_mathlib_options(tmp_path: Path, arguments: list[str]) -> None:
    home = str(tmp_path / "home")
    assert main(["--home", home, "new", str(tmp_path / "Proof"), *arguments]) == 2


def test_new_passes_the_selected_release_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "Proof"
    seen: dict[str, object] = {}
    plan = SimpleNamespace(
        action="create",
        root=target,
        project_name="Proof",
        mathlib_version="4.35.0",
        toolchain="leanprover/lean4:v4.35.0",
        toolchain_installed=True,
        seed_root=None,
        download_bytes=None,
        download_bytes_complete=False,
        blockers=(),
        ready=True,
    )

    def plan_project_init(_self: object, *_args: object, **kwargs: object) -> SimpleNamespace:
        seen.update(kwargs)
        return plan

    monkeypatch.setattr("lean_runtime.cli.Runtime.plan_project_init", plan_project_init)
    monkeypatch.setattr(
        "lean_runtime.cli.Runtime.init_project",
        lambda *_a, **_k: SimpleNamespace(root=target, packages=9),
    )
    home = str(tmp_path / "home")
    arguments = ["--home", home, "new", str(target), "--mathlib", "v4.35.0", "--uncataloged"]
    assert main([*arguments, "--yes"]) == 0
    assert seen["mathlib"] == "4.35.0"
    assert seen["uncataloged"] is True


def test_catalog_update_targets_every_release_after_the_newest_cataloged() -> None:
    tags = ["v4.9.0", "v4.33.0", "v4.33.1", "v4.34.0", "v4.34.1"]
    cataloged = {"mathlib-v4.33.0", "mathlib-v4.33.1", "leancert-v4.40.0", "core-v4.32.2"}
    assert catalog_update.missing_tags(tags, cataloged) == ["v4.34.0", "v4.34.1"]
    assert catalog_update.missing_tags(tags, {"mathlib-v4.34.1"}) == []


def test_incomplete_download_estimate_is_not_shown_as_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from lean_runtime.cli import _render_init_plan
    from lean_runtime.project_sharing import ProjectInitPlan

    plan = ProjectInitPlan(
        tmp_path,
        "create",
        "leanprover/lean4:v4.35.0",
        "4.35.0",
        ("mathlib",),
        None,
        0,
        False,
        toolchain_installed=True,
    )
    _render_init_plan(plan)
    output = capsys.readouterr().out
    assert "Download: unknown" in output
    assert "Download: 0 B" not in output
