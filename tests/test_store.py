from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from lean_runtime import EnvironmentError, EnvironmentLock, LockedPackage
from lean_runtime.store import EnvironmentStore, environment_identity

RETAINED = "env_" + "a" * 64
CANDIDATE = "env_" + "b" * 64
FIRST = "env_" + "c" * 64
SECOND = "env_" + "d" * 64


def _sample_lock() -> EnvironmentLock:
    return EnvironmentLock(
        toolchain="leanprover/lean4:v4.32.0",
        spec_digest="spec_" + "e" * 64,
        root_lakefile='name = "test"\n',
        root_module="/- root -/\n",
        manifest={"version": "1.1.0", "packages": []},
        packages=(
            LockedPackage(
                name="sample",
                url="https://example.test/sample",
                revision="a" * 40,
                source_id="source_" + "f" * 64,
                tree_hash="b" * 40,
            ),
        ),
    )


def test_aliases_and_garbage_collection(tmp_path: Path) -> None:
    store = EnvironmentStore(tmp_path)
    retained = store.environment_path(RETAINED)
    candidate = store.environment_path(CANDIDATE)
    retained.mkdir()
    candidate.mkdir()
    store.set_alias("research", retained.name)
    old = 1_000_000_000
    os.utime(retained, (old, old))
    os.utime(candidate, (old, old))

    dry = store.gc(dry_run=True, minimum_age_seconds=0)
    assert dry.candidates == (candidate.name,)
    assert candidate.is_dir()

    applied = store.gc(dry_run=False, minimum_age_seconds=0)
    assert applied.removed == (candidate.name,)
    assert retained.is_dir()
    assert not candidate.exists()


def test_alias_update_does_not_mutate_environment(tmp_path: Path) -> None:
    store = EnvironmentStore(tmp_path)
    first = store.environment_path(FIRST)
    second = store.environment_path(SECOND)
    first.mkdir()
    second.mkdir()
    store.set_alias("current", first.name)
    assert store.resolve_identifier("current") == first.name
    store.set_alias("current", second.name)
    assert store.resolve_identifier("current") == second.name
    assert first.is_dir()


def test_recent_usage_retains_an_unnamed_environment(tmp_path: Path) -> None:
    store = EnvironmentStore(tmp_path)
    environment = store.environment_path(CANDIDATE)
    environment.mkdir()
    os.utime(environment, (1_000_000_000, 1_000_000_000))
    store.touch_environment(CANDIDATE)
    report = store.gc(dry_run=True, minimum_age_seconds=60)
    assert CANDIDATE in report.retained


def test_alias_record_is_validated(tmp_path: Path) -> None:
    store = EnvironmentStore(tmp_path)
    store.environment_path(FIRST).mkdir()
    path = store.names / "current.json"
    path.write_text(json.dumps({"name": "current", "environment_id": FIRST}))
    with pytest.raises(EnvironmentError, match="invalid environment alias"):
        store.resolve_identifier("current")


def test_only_implemented_build_profile_is_accepted() -> None:
    with pytest.raises(EnvironmentError, match="only 'release'"):
        environment_identity(_sample_lock(), "debug")
