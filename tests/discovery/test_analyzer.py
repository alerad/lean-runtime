from lean_runtime.discovery import analyze_source


def test_extracts_multiple_imports_and_roots() -> None:
    evidence = analyze_source("import Mathlib.Data.Nat.Basic Batteries.Data.List\n")
    assert evidence.imports == ("Mathlib.Data.Nat.Basic", "Batteries.Data.List")
    assert evidence.root_namespaces == ("Mathlib", "Batteries")
    assert not evidence.appears_core_only


def test_ignores_nested_comments_strings_and_line_comments() -> None:
    source = """/- import Fake.One /- import Fake.Two -/ -/
def text := "import Fake.String"
-- import Fake.Line
import Mathlib
"""
    assert analyze_source(source).imports == ("Mathlib",)


def test_parses_runtime_frontmatter() -> None:
    source = """-- /// lean-runtime
-- requires = ["mathlib@v4.32.2"]
-- toolchain = "leanprover/lean4:v4.32.2"
-- ///
import Mathlib
"""
    evidence = analyze_source(source)
    assert evidence.package_hints == ("mathlib@v4.32.2",)
    assert evidence.toolchain_hint == "leanprover/lean4:v4.32.2"


def test_malformed_frontmatter_becomes_warning() -> None:
    evidence = analyze_source("-- /// lean-runtime\n-- requires = []\n")
    assert evidence.warnings


def test_core_only_is_conservative() -> None:
    assert analyze_source("example : True := trivial\n").appears_core_only
    assert analyze_source("import Init\n").appears_core_only
    assert not analyze_source("import Batteries\n").appears_core_only


def test_lock_hint_is_preserved() -> None:
    source = """-- /// lean-runtime
-- lock = "environment.lock.json"
-- ///
example : True := trivial
"""
    assert analyze_source(source).lock_hint == "environment.lock.json"
