from __future__ import annotations

import pytest

from lean_runtime import EnvironmentError, EnvironmentLock, LockedPackage
from lean_runtime.environments import (
    _check_lean_paths,
    _package_import_targets,
    _source_files,
    _support_order,
)


def test_nested_lean_sources_are_normalized() -> None:
    assert _source_files({"Support\\Defs.lean": "def value := 1"}) == {
        "Support/Defs.lean": "def value := 1"
    }


@pytest.mark.parametrize("name", ["../Main.lean", "/tmp/Main.lean", "Main.txt", ""])
def test_unsafe_multifile_paths_are_rejected(name: str) -> None:
    with pytest.raises(EnvironmentError, match="unsafe Lean source path"):
        _source_files({name: ""})


def test_support_modules_are_topologically_ordered() -> None:
    files = {
        "Base.lean": "def base := 1",
        "Support/Defs.lean": "import Base\ndef value := base",
        "Main.lean": "import Support.Defs\n#check value",
    }
    assert _support_order(files, "Main.lean") == ("Base.lean", "Support/Defs.lean")


def test_multifile_import_cycles_are_rejected() -> None:
    files = {"A.lean": "import B", "B.lean": "import A"}
    with pytest.raises(EnvironmentError, match="import cycle"):
        _support_order(files, "A.lean")


def test_package_import_targets_exclude_core_and_submitted_modules() -> None:
    lock = EnvironmentLock(
        toolchain="leanprover/lean4:v4.32.0",
        spec_digest="spec_" + "a" * 64,
        root_lakefile='name = "test"\n',
        root_module="",
        manifest={"packages": []},
        packages=(
            LockedPackage(
                name="proofwidgets",
                url="https://example.test/proofwidgets",
                revision="b" * 40,
                source_id="source_" + "c" * 64,
                tree_hash="d" * 40,
            ),
        ),
    )
    files = {
        "Local.lean": "def local := 1",
        "Main.lean": "import Init\nimport Local\nimport ProofWidgets.Util",
    }
    assert _package_import_targets(files, lock) == ("ProofWidgets",)


def test_check_lean_paths_include_scratch_root_and_compiled_packages(tmp_path) -> None:
    lock = EnvironmentLock(
        toolchain="leanprover/lean4:v4.32.0",
        spec_digest="spec_" + "a" * 64,
        root_lakefile='name = "test"\n',
        root_module="",
        manifest={"packages": [], "packagesDir": "vendor"},
        packages=(
            LockedPackage(
                name="sample",
                url="https://example.test/sample",
                revision="b" * 40,
                source_id="source_" + "c" * 64,
                tree_hash="d" * 40,
                subdir="lean",
            ),
        ),
    )
    workspace = tmp_path / "workspace"
    root_build = workspace / ".lake" / "build" / "lib" / "lean"
    package_build = workspace / "vendor" / "sample" / "lean" / ".lake" / "build" / "lib" / "lean"
    root_build.mkdir(parents=True)
    package_build.mkdir(parents=True)

    assert _check_lean_paths(workspace, tmp_path / "scratch", lock) == (
        tmp_path / "scratch" / ".lake" / "build" / "lib" / "lean",
        package_build,
        root_build,
    )
