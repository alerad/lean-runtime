from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from lean_runtime._relpath import packages_directory, safe_relative_name, safe_relative_posix

REJECTED = [
    None,
    3,
    "",
    "/etc/passwd",
    "//server/share",
    "C:/x",
    "c:x",
    "a\\b",
    "..",
    "../a",
    "a/../b",
    "a/..",
    "a\x00b",
    ".",
    "./",
]

ACCEPTED = {
    "a": "a",
    "a/b.lean": "a/b.lean",
    "./a/b": "a/b",
    "a//b": "a/b",
    ".lake/packages": ".lake/packages",
    "a/./b": "a/b",
    "..a/b..": "..a/b..",
}


@pytest.mark.parametrize("value", REJECTED, ids=repr)
def test_rejected_forms(value: object) -> None:
    with pytest.raises(ValueError):
        safe_relative_posix(value)


@pytest.mark.parametrize(("value", "expected"), ACCEPTED.items(), ids=repr)
def test_accepted_forms(value: str, expected: str) -> None:
    assert safe_relative_posix(value) == PurePosixPath(expected)


def test_root_only_with_permission() -> None:
    assert safe_relative_posix(".", allow_root=True) == PurePosixPath(".")


@pytest.mark.parametrize("value", ["a:b", "con.", "a b ", "a?b", "a<b"])
def test_portable_names_reject_windows_hostile_components(value: str) -> None:
    with pytest.raises(ValueError):
        safe_relative_name(value)


def test_packages_directory_policy() -> None:
    assert packages_directory({}) == PurePosixPath(".lake/packages")
    assert packages_directory({"packagesDir": "deps"}) == PurePosixPath("deps")
    for manifest in ([], {"packagesDir": 1}, {"packagesDir": "."}, {"packagesDir": "../x"}):
        with pytest.raises(ValueError):
            packages_directory(manifest)
