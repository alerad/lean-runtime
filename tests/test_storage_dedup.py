from __future__ import annotations

import errno
import json
import os
import shutil
from pathlib import Path

import pytest

from lean_runtime import storage_dedup
from lean_runtime._cow import copy_file, reflink
from lean_runtime.locking import FileLock, LockPaths
from lean_runtime.package_ids import package_directory_id


def _package(home: Path, name: str, payload: bytes) -> Path:
    marker = {"test": name}
    package = home / "project-packages" / package_directory_id(marker)
    output = package / ".lake" / "build" / "lib" / "lean" / "Example.olean"
    output.parent.mkdir(parents=True)
    output.write_bytes(payload)
    (package / ".lean-runtime-package.json").write_text(json.dumps(marker))
    return output


@pytest.fixture
def portable_clone(monkeypatch: pytest.MonkeyPatch) -> None:
    # Exercise the maintenance logic even on CI filesystems without reflinks.
    def clone(source: Path, destination: Path) -> None:
        assert not destination.exists()
        shutil.copy2(source, destination)

    monkeypatch.setattr(storage_dedup, "reflink", clone)


def test_dry_run_never_replaces_or_creates_index(tmp_path: Path) -> None:
    a = _package(tmp_path, "a", b"x" * 100_000)
    b = _package(tmp_path, "b", a.read_bytes())
    before = (a.stat().st_ino, b.stat().st_ino)
    report = storage_dedup.deduplicate(tmp_path)
    assert report.duplicate_bytes == 100_000
    assert report.cloned_bytes == 0
    assert before == (a.stat().st_ino, b.stat().st_ino)
    assert not (tmp_path / "deduplication.sqlite3").exists()


def test_compact_preserves_paths_times_permissions_and_independent_writes(
    tmp_path: Path,
    portable_clone: None,
) -> None:
    a = _package(tmp_path, "a", b"x" * 100_000)
    b = _package(tmp_path, "b", a.read_bytes())
    os.utime(a, ns=(1_700_000_000_000_000_000,) * 2)
    os.utime(b, ns=(1_700_000_001_000_000_000,) * 2)
    a.chmod(0o644)
    b.chmod(0o755)
    before = {p: (p.stat().st_mtime_ns, p.stat().st_mode) for p in (a, b)}
    report = storage_dedup.deduplicate(tmp_path, dry_run=False)
    assert report.cloned_bytes == 100_000
    assert a.stat().st_ino != b.stat().st_ino
    for p in (a, b):
        assert (p.stat().st_mtime_ns, p.stat().st_mode) == before[p]
    second = storage_dedup.deduplicate(tmp_path, dry_run=False)
    assert second.indexed_files == 2
    assert second.hashed_bytes == 0
    b.write_bytes(b"y" * 100_000)
    assert a.read_bytes() == b"x" * 100_000
    third = storage_dedup.deduplicate(tmp_path, dry_run=False)
    assert third.hashed_bytes == 100_000
    assert third.cloned_files == 0


def test_changed_donor_never_replaces_target(tmp_path: Path, portable_clone: None) -> None:
    a = _package(tmp_path, "a", b"a" * 100_000)
    storage_dedup.deduplicate(tmp_path, dry_run=False)
    a.write_bytes(b"b" * 100_000)
    b = _package(tmp_path, "b", b"a" * 100_000)
    report = storage_dedup.deduplicate(
        tmp_path,
        dry_run=False,
        package_ids=(b.parents[4].name,),
    )
    assert report.cloned_files == 0
    assert b.read_bytes() == b"a" * 100_000


def test_symlinks_and_busy_packages_are_not_changed(tmp_path: Path, portable_clone: None) -> None:
    a = _package(tmp_path, "a", b"a" * 100_000)
    b = _package(tmp_path, "b", a.read_bytes())
    link = b.with_name("borrowed.olean")
    link.symlink_to(a)
    package = b.parents[4]
    with FileLock(LockPaths(tmp_path).package_build(package.name)):
        report = storage_dedup.deduplicate(tmp_path, dry_run=False)
    assert report.skipped_busy == 1
    assert report.scanned_files == 1
    assert link.is_symlink()


def test_unsupported_filesystem_does_not_copy_or_hardlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = _package(tmp_path, "a", b"a" * 100_000)
    b = _package(tmp_path, "b", a.read_bytes())

    def unavailable(*args: object) -> None:
        raise OSError(errno.ENOTSUP, "no CoW")

    monkeypatch.setattr(storage_dedup, "reflink", unavailable)
    report = storage_dedup.deduplicate(tmp_path, dry_run=False)
    assert report.supported is False
    assert report.cloned_files == 0
    assert a.stat().st_ino != b.stat().st_ino
    assert not (tmp_path / "deduplication.sqlite3").exists()


def test_reflink_write_isolation_on_real_filesystem(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.write_bytes(b"original" * 20_000)
    try:
        reflink(a, b)
    except OSError:
        pytest.skip("test filesystem does not support CoW cloning")
    assert a.stat().st_ino != b.stat().st_ino
    b.write_bytes(b"new bytes")
    assert a.read_bytes() == b"original" * 20_000


def test_copy_file_falls_back_without_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lean_runtime._cow as cow

    def unavailable(*args: object) -> None:
        raise OSError(errno.ENOTSUP, "no CoW")

    monkeypatch.setattr(cow, "reflink", unavailable)
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"original")
    copy_file(a, b)
    b.write_bytes(b"new bytes")
    assert a.read_bytes() == b"original"


def test_shared_builds_automatically_compact_new_outputs(
    tmp_path: Path,
    portable_clone: None,
) -> None:
    from lean_runtime.events import EventEmitter
    from lean_runtime.shared_projects import SharedProjectManager, SharedProjectWorkspace

    manager = SharedProjectManager(tmp_path, EventEmitter())
    for name in ("a", "b"):
        output = _package(tmp_path, name, b"x" * 100_000)
        workspace = SharedProjectWorkspace(
            "test",
            tmp_path,
            tmp_path / "overrides.json",
            False,
            (name,),
            (output.parents[4].name,),
        )
        with manager.build_lock(workspace):
            output.write_bytes(b"x" * 100_000)
    report = storage_dedup.deduplicate(tmp_path, dry_run=False)
    assert report.indexed_files == 2
    assert report.hashed_bytes == 0
    assert report.cloned_files == 0


def test_compaction_handles_composition_builds_without_following_borrowed_sources(
    tmp_path: Path,
    portable_clone: None,
) -> None:
    output = _package(tmp_path, "ordinary", b"x" * 100_000)
    package = tmp_path / "compositions" / "packages" / ("composition_package_" + "a" * 64)
    target = package / "view" / "dependency_view_test" / ".lake" / "build" / "lib" / "A.olean"
    target.parent.mkdir(parents=True)
    target.write_bytes(output.read_bytes())
    borrowed = tmp_path / "outside"
    borrowed.mkdir()
    (borrowed / "B.olean").write_bytes(output.read_bytes())
    (target.parent / "borrowed").symlink_to(borrowed)
    report = storage_dedup.deduplicate(tmp_path, dry_run=False)
    assert report.scanned_files == 2
    assert report.cloned_files == 1
    assert (target.parent / "borrowed").is_symlink()


def test_cli_defaults_to_preview_and_requires_yes_to_apply(
    tmp_path: Path,
    portable_clone: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from lean_runtime.cli import main

    _package(tmp_path, "a", b"x" * 100_000)
    _package(tmp_path, "b", b"x" * 100_000)
    args = ["--home", str(tmp_path), "storage", "deduplicate", "--json"]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert not (tmp_path / "deduplication.sqlite3").exists()
    assert main([*args, "--yes"]) == 0
    assert json.loads(capsys.readouterr().out)["cloned_files"] == 1
    assert main([*args, "--yes", "--dry-run"]) != 0


def test_unverified_clone_is_discarded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a = _package(tmp_path, "a", b"a" * 100_000)
    b = _package(tmp_path, "b", a.read_bytes())

    def bad_clone(source: Path, destination: Path) -> None:
        destination.write_bytes(b"corrupt clone")

    monkeypatch.setattr(storage_dedup, "reflink", bad_clone)
    report = storage_dedup.deduplicate(tmp_path, dry_run=False)
    assert report.cloned_files == 0
    assert a.read_bytes() == b.read_bytes() == b"a" * 100_000
    assert not list(tmp_path.rglob("*.dedup-tmp"))


def test_automatic_index_failure_does_not_fail_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3

    from lean_runtime.events import EventEmitter
    from lean_runtime.shared_projects import SharedProjectManager, SharedProjectWorkspace

    events = []
    manager = SharedProjectManager(tmp_path, EventEmitter(events.append))
    output = _package(tmp_path, "a", b"x" * 100_000)
    workspace = SharedProjectWorkspace(
        "test",
        tmp_path,
        tmp_path / "overrides.json",
        False,
        ("a",),
        (output.parents[4].name,),
    )

    def broken(*args: object, **kwargs: object) -> None:
        raise sqlite3.DatabaseError("broken disposable index")

    monkeypatch.setattr(storage_dedup, "deduplicate", broken)
    with manager.build_lock(workspace):
        pass
    assert output.read_bytes() == b"x" * 100_000
    assert any(event.kind == "storage.deduplication_failed" for event in events)


def test_compaction_preserves_native_macos_acls(tmp_path: Path, portable_clone: None) -> None:
    import subprocess
    import sys

    if sys.platform != "darwin":
        pytest.skip("macOS ACLs")
    a = _package(tmp_path, "a", b"x" * 100_000)
    b = _package(tmp_path, "b", a.read_bytes())
    subprocess.run(["chmod", "+a", "everyone allow read", str(a)], check=True)

    def acl(path: Path) -> list[str]:
        result = subprocess.run(
            ["ls", "-le", str(path)], capture_output=True, text=True, check=True
        )
        return result.stdout.splitlines()[1:]

    before = {p: acl(p) for p in (a, b)}
    assert before[a] != before[b]
    report = storage_dedup.deduplicate(tmp_path, dry_run=False)
    assert report.cloned_files == 1
    assert {p: acl(p) for p in (a, b)} == before


def test_build_compaction_does_not_wait_for_maintenance(
    tmp_path: Path,
    portable_clone: None,
) -> None:
    output = _package(tmp_path, "a", b"x" * 100_000)
    with FileLock(LockPaths(tmp_path).collector("deduplication")):
        report = storage_dedup.deduplicate(
            tmp_path,
            dry_run=False,
            already_locked=True,
            package_ids=(output.parents[4].name,),
        )
    assert report.skipped_busy == 1
    assert report.scanned_files == 0
