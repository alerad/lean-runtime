from pathlib import Path

import pytest

from lean_runtime import SpecificationError
from lean_runtime.frontmatter import load_frontmatter, parse_frontmatter


def test_frontmatter_parses_strict_toml_header(tmp_path: Path) -> None:
    source = """-- /// lean-runtime
-- requires = ["mathlib@v4.32.2"]
-- toolchain = "leanprover/lean4:v4.32.0"
-- ///

import Mathlib
"""
    metadata = parse_frontmatter(source)
    assert metadata is not None
    assert metadata.requires == ("mathlib@v4.32.2",)
    assert metadata.toolchain == "leanprover/lean4:v4.32.0"
    path = tmp_path / "Main.lean"
    path.write_text(source)
    assert load_frontmatter(path) == metadata


def test_frontmatter_can_reference_an_exact_relative_lock() -> None:
    metadata = parse_frontmatter(
        '-- /// lean-runtime\n-- lock = "environment.lock.json"\n-- ///\n'
        "example : True := by trivial\n"
    )
    assert metadata is not None and metadata.lock == "environment.lock.json"


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (
            'example : True := by trivial\n-- /// lean-runtime\n-- toolchain = "4.32.0"\n-- ///',
            "must precede",
        ),
        ("-- /// lean-runtime\n-- requires = []", "missing its closing"),
        ("-- /// lean-runtime\nrequires = []\n-- ///", "must be Lean comments"),
        ("-- /// lean-runtime\n-- unknown = true\n-- ///", "unknown.*field"),
        (
            '-- /// lean-runtime\n-- requires = ["mathlib@v1"]\n-- lock = "lock.json"\n-- ///',
            "cannot combine",
        ),
        (
            '-- /// lean-runtime\n-- toolchain = "4.32.0"\n-- lock = "lock.json"\n-- ///',
            "cannot combine",
        ),
    ],
)
def test_invalid_frontmatter_is_actionable(source: str, message: str) -> None:
    with pytest.raises(SpecificationError, match=message):
        parse_frontmatter(source)


def test_source_without_frontmatter_returns_none() -> None:
    assert parse_frontmatter("import Mathlib\n") is None
