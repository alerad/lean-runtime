from __future__ import annotations

from pathlib import Path

import pytest
from _lockholder import ForeignLockHolder, is_held

from lean_runtime._transaction import (
    abandoned_staging,
    publish_tree,
    remove_abandoned_staging,
    staged_tree,
)
from lean_runtime.errors import EnvironmentError
from lean_runtime.locking import LockPaths


def test_staged_tree_is_owned_while_building_and_removed_after(tmp_path: Path) -> None:
    locks = LockPaths(tmp_path)
    with staged_tree(tmp_path / "objects", locks) as stage:
        assert stage.is_dir() and stage.name.startswith(".staging-")
        nonce = stage.name.removeprefix(".staging-")
        assert is_held(locks.staging(nonce))
        assert abandoned_staging(tmp_path / "objects", locks) == []
        (stage / "file").write_text("x")
    assert not stage.exists()
    assert not is_held(locks.staging(nonce))


def test_publish_commits_by_rename_and_loses_races_gracefully(tmp_path: Path) -> None:
    locks = LockPaths(tmp_path)
    destination = tmp_path / "objects" / "env_1"
    with staged_tree(tmp_path / "objects", locks) as stage:
        (stage / "a").write_text("new")
        assert publish_tree(stage, destination) is True
        assert not stage.exists()
    assert (destination / "a").read_text() == "new"
    with staged_tree(tmp_path / "objects", locks) as stage:
        (stage / "a").write_text("other")
        assert publish_tree(stage, destination) is False
    assert (destination / "a").read_text() == "new"


def test_replace_swaps_and_restores_the_old_tree_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locks = LockPaths(tmp_path)
    destination = tmp_path / "objects" / "env_1"
    destination.mkdir(parents=True)
    (destination / "a").write_text("old")
    with staged_tree(tmp_path / "objects", locks) as stage:
        (stage / "a").write_text("new")
        assert publish_tree(stage, destination, replace=True) is True
    assert (destination / "a").read_text() == "new"
    assert not list((tmp_path / "objects").glob(".trash-*"))

    original = Path.replace

    def failing(self: Path, target: Path) -> Path:
        if self.name.startswith(".staging-"):
            raise OSError("disk says no")
        return original(self, target)

    monkeypatch.setattr(Path, "replace", failing)
    with staged_tree(tmp_path / "objects", locks) as stage:
        (stage / "a").write_text("newer")
        with pytest.raises(EnvironmentError, match="could not publish"):
            publish_tree(stage, destination, replace=True)
    assert (destination / "a").read_text() == "new"
    assert not list((tmp_path / "objects").glob(".trash-*"))


def test_repair_skips_staging_held_by_another_process(tmp_path: Path) -> None:
    locks = LockPaths(tmp_path)
    root = tmp_path / "objects"
    root.mkdir()
    live = root / ".staging-aaaaaaaaaaaa"
    live.mkdir()
    dead = root / ".staging-bbbbbbbbbbbb"
    dead.mkdir()
    legacy = root / ".staging-12345-deadbeef"
    legacy.mkdir()
    with ForeignLockHolder(locks.staging("aaaaaaaaaaaa")):
        assert abandoned_staging(root, locks) == [legacy, dead]
        assert remove_abandoned_staging(root, locks) == 2
    assert live.is_dir() and not dead.exists() and not legacy.exists()
    assert remove_abandoned_staging(root, locks) == 1
