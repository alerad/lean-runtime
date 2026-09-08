"""Content-addressed objects keep their identity after construction."""

from __future__ import annotations

import copy
import json

import pytest
from conftest import make_lock

from lean_runtime import EnvironmentLock, EnvironmentSpec, GitPackage
from lean_runtime.lake import generate_root_module
from lean_runtime.serialization import FrozenDict, freeze_json, thaw_json


def test_lock_manifest_cannot_drift_after_construction() -> None:
    manifest = {"version": "1.1.0", "packagesDir": ".lake/packages", "packages": [{"name": "a"}]}
    lock = make_lock("a")
    lock = EnvironmentLock(
        toolchain=lock.toolchain,
        spec_digest=lock.spec_digest,
        root_lakefile=lock.root_lakefile,
        root_module=lock.root_module,
        manifest=manifest,
        packages=(),
    )
    before = lock.lock_id
    manifest["packagesDir"] = "elsewhere"  # the caller's own dict is detached
    manifest["packages"][0]["name"] = "b"
    assert lock.lock_id == before
    with pytest.raises(TypeError):
        lock.manifest["packagesDir"] = "elsewhere"
    with pytest.raises(TypeError):
        lock.manifest["packages"].append({})
    exported = lock.to_dict()
    exported["manifest"]["packagesDir"] = "mutated-copy"
    assert lock.lock_id == before
    assert json.loads(json.dumps(lock.to_dict()))["manifest"]["packagesDir"] == ".lake/packages"


def test_frozen_json_stays_equal_to_plain_json_and_copies_are_mutable() -> None:
    value = {"a": [1, {"b": 2}], "c": "d"}
    frozen = freeze_json(value)
    assert frozen == value and isinstance(frozen, dict) and isinstance(frozen["a"], list)
    assert thaw_json(frozen) == value and type(thaw_json(frozen)) is dict
    duplicate = copy.deepcopy(frozen)
    duplicate["a"].append(3)
    assert frozen["a"] == [1, {"b": 2}]
    assert type(FrozenDict()) is FrozenDict


def test_spec_package_order_does_not_change_identity_or_generated_root() -> None:
    first = GitPackage.git("zeta", "https://example.invalid/z.git", "a" * 40)
    second = GitPackage.git("alpha", "https://example.invalid/a.git", "b" * 40)
    forward = EnvironmentSpec("leanprover/lean4:v4.32.2", (first, second))
    reverse = EnvironmentSpec("leanprover/lean4:v4.32.2", (second, first))
    assert forward.spec_digest == reverse.spec_digest
    assert generate_root_module(forward) == generate_root_module(reverse)
    assert generate_root_module(forward).startswith("import Alpha\nimport Zeta\n")


@pytest.mark.parametrize("subdir", ["../x", "/abs", "C:/x", "a\\b", ""])
def test_lock_and_spec_reject_unsafe_subdirs(subdir: str) -> None:
    from lean_runtime.errors import EnvironmentError, SpecificationError
    from lean_runtime.lockfiles import LockedPackage

    with pytest.raises(SpecificationError):
        GitPackage.git("dep", "https://example.invalid/d.git", "a" * 40, subdir=subdir)
    with pytest.raises(EnvironmentError):
        LockedPackage(
            name="dep",
            url="https://example.invalid/d.git",
            revision="a" * 40,
            tree_hash="a" * 40,
            source_id="source_" + "a" * 64,
            subdir=subdir,
        )


def test_lock_rejects_malformed_types_instead_of_coercing() -> None:
    from lean_runtime.errors import EnvironmentError

    good = make_lock("a").to_dict()
    for broken in (
        {**good, "manifest": []},
        {**good, "packages": "nope"},
        {**good, "toolchain": 3},
    ):
        with pytest.raises(EnvironmentError):
            EnvironmentLock.from_dict(broken)
