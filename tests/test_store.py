from __future__ import annotations

import os
from pathlib import Path

from lean_runtime.store import EnvironmentStore


def test_aliases_and_garbage_collection(tmp_path: Path) -> None:
    store = EnvironmentStore(tmp_path)
    retained = store.environment_path("env_retained")
    candidate = store.environment_path("env_candidate")
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
    first = store.environment_path("env_first")
    second = store.environment_path("env_second")
    first.mkdir()
    second.mkdir()
    store.set_alias("current", first.name)
    assert store.resolve_identifier("current") == first.name
    store.set_alias("current", second.name)
    assert store.resolve_identifier("current") == second.name
    assert first.is_dir()
