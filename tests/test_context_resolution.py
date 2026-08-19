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
