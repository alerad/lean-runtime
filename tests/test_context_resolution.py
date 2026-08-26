from pathlib import Path

import pytest
from conftest import make_entry

from lean_runtime import Runtime
from lean_runtime.context_resolution import resolve_file_context
from lean_runtime.discovery import Catalog
from lean_runtime.errors import SpecificationError, ToolchainError
from lean_runtime.frontmatter import LeanFrontmatter


def test_context_resolution_uses_explicit_precedence(tmp_path: Path) -> None:
    source = tmp_path / "Main.lean"
    source.write_text("example : True := trivial\n")
    resolution = resolve_file_context(
        source,
        LeanFrontmatter(lock="environment.lock.json"),
        discover=True,
    )
    assert resolution.kind == "lock"


def test_context_resolution_distinguishes_absence_from_broken_project(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Main.lean"
    source.write_text("example : True := trivial\n")
    assert resolve_file_context(source, LeanFrontmatter(), discover=True).kind == "discovery"
    with pytest.raises(SpecificationError, match="no explicit context"):
        resolve_file_context(source, LeanFrontmatter(), discover=False)

    (tmp_path / "lakefile.toml").write_text('name = "fixture"\n')
    (tmp_path / "lean-toolchain").write_text("\n")
    with pytest.raises(ToolchainError):
        # A malformed project must not silently fall through to discovery.
        resolve_file_context(source, LeanFrontmatter(), discover=True)


def test_declarative_project_only_claims_files_under_declared_targets(tmp_path: Path) -> None:
    (tmp_path / "lakefile.toml").write_text('name = "fixture"\n[[lean_lib]]\nname = "Fixture"\n')
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n")
    owned = tmp_path / "Fixture" / "Main.lean"
    owned.parent.mkdir()
    owned.write_text("example : True := trivial\n")
    scratch = tmp_path / "scratch" / "Main.lean"
    scratch.parent.mkdir()
    scratch.write_text("example : True := trivial\n")

    owned_resolution = resolve_file_context(owned, LeanFrontmatter(), discover=True)
    scratch_resolution = resolve_file_context(scratch, LeanFrontmatter(), discover=True)

    assert owned_resolution.kind == "project"
    assert "owns" in owned_resolution.reasons[0]
    assert scratch_resolution.kind == "discovery"
    assert scratch_resolution.project is not None
    assert "not owned" in scratch_resolution.reasons[0]


def test_library_roots_do_not_exclude_sibling_modules_from_project_ownership(
    tmp_path: Path,
) -> None:
    (tmp_path / "lakefile.toml").write_text(
        'name = "fixture"\n[[lean_lib]]\nname = "Fixture"\nroots = ["Fixture.Defs"]\n'
    )
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n")
    source = tmp_path / "Fixture" / "Main.lean"
    source.parent.mkdir()
    source.write_text("import Fixture.Defs\n")

    resolution = resolve_file_context(source, LeanFrontmatter(), discover=True)

    assert resolution.kind == "project"
    assert "owns" in resolution.reasons[0]


def test_imperative_project_preserves_ancestry_behavior_when_ownership_is_ambiguous(
    tmp_path: Path,
) -> None:
    (tmp_path / "lakefile.lean").write_text("import Lake\nopen Lake DSL\npackage fixture\n")
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n")
    source = tmp_path / "scratch" / "Main.lean"
    source.parent.mkdir()
    source.write_text("example : True := trivial\n")

    resolution = resolve_file_context(source, LeanFrontmatter(), discover=True)

    assert resolution.kind == "project"
    assert "ambiguous" in resolution.reasons[0]


def test_standalone_override_ignores_an_owned_parent_project(tmp_path: Path) -> None:
    (tmp_path / "lakefile.toml").write_text('name = "fixture"\n[[lean_lib]]\nname = "Fixture"\n')
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n")
    source = tmp_path / "Fixture.lean"
    source.write_text("example : True := trivial\n")

    resolution = resolve_file_context(source, LeanFrontmatter(), discover=True, standalone=True)

    assert resolution.kind == "discovery"
    assert "explicitly requested" in resolution.reasons[0]


def test_runtime_exposes_analysis_plan_and_context_layers(tmp_path: Path) -> None:
    source = tmp_path / "Main.lean"
    source.write_text("import Mathlib\n")
    catalog = Catalog(
        generated_at="2026-08-19T00:00:00Z",
        entries=(make_entry("mathlib", "d", modules=("Mathlib",), packages=("mathlib",)),),
    )
    runtime = Runtime(home=tmp_path / "runtime", libraries=())

    assert runtime.analyze_file(source).imports == ("Mathlib",)
    assert runtime.plan_file(source, catalog=catalog).candidates[0].entry.id == "mathlib"
    assert runtime.resolve_file(source).kind == "discovery"
