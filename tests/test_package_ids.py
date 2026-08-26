from pathlib import Path

from lean_runtime.package_ids import (
    is_package_id,
    package_directories,
    package_directory_id,
    package_id_matches,
)
from lean_runtime.serialization import sha256_id

IDENTITY = {"schema": "lean-runtime-shared-project/2", "package": {"name": "mathlib"}}


def test_new_directory_names_are_short_and_content_addressed() -> None:
    package_id = package_directory_id(IDENTITY)
    assert is_package_id(package_id)
    assert len(package_id) == len("pkg_") + 32
    assert package_id == package_directory_id(dict(IDENTITY))
    assert package_id != package_directory_id({**IDENTITY, "package": {"name": "aesop"}})
    # The truncated digest is the legacy digest's prefix, so both name one identity.
    legacy = sha256_id("project_package", IDENTITY)
    assert legacy.removeprefix("project_package_").startswith(package_id.removeprefix("pkg_"))


def test_legacy_and_new_ids_both_validate_the_same_marker() -> None:
    legacy = sha256_id("project_package", IDENTITY)
    assert is_package_id(legacy)
    assert package_id_matches(IDENTITY, legacy)
    assert package_id_matches(IDENTITY, package_directory_id(IDENTITY))
    assert not package_id_matches(IDENTITY, "pkg_" + "0" * 32)
    assert not is_package_id("pkg_" + "0" * 31)
    assert not is_package_id("project_package_" + "0" * 63)
    assert not is_package_id("../pkg_" + "0" * 32)


def test_package_directories_lists_both_schemes_sorted(tmp_path: Path) -> None:
    (tmp_path / ("project_package_" + "b" * 64)).mkdir()
    (tmp_path / ("pkg_" + "a" * 32)).mkdir()
    (tmp_path / ("pkg_" + "c" * 32)).write_text("not a directory")
    (tmp_path / "unrelated").mkdir()
    assert [path.name for path in package_directories(tmp_path)] == [
        "pkg_" + "a" * 32,
        "project_package_" + "b" * 64,
    ]
