from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from lean_runtime import EnvironmentError, EnvironmentLock, LockedPackage
from lean_runtime.store import EnvironmentStore, environment_identity, platform_compatibility

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

    dry = store.clean(dry_run=True, minimum_age_seconds=0)
    assert dry.candidates == (candidate.name,)
    assert candidate.is_dir()

    applied = store.clean(dry_run=False, minimum_age_seconds=0)
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
    report = store.clean(dry_run=True, minimum_age_seconds=60)
    assert CANDIDATE in report.retained


def test_execution_lease_prevents_collection(tmp_path: Path) -> None:
    store = EnvironmentStore(tmp_path)
    environment = store.environment_path(CANDIDATE)
    environment.mkdir()
    with store.execution_lease(CANDIDATE):
        report = store.clean(dry_run=True, minimum_age_seconds=0)
        assert CANDIDATE in report.retained
        assert CANDIDATE not in report.candidates
    report = store.clean(dry_run=True, minimum_age_seconds=0)
    assert CANDIDATE in report.candidates


def test_oci_blob_gc_retains_environment_references_and_active_leases(tmp_path: Path) -> None:
    store = EnvironmentStore(tmp_path)
    referenced = "1" * 64
    leased = "2" * 64
    candidate = "3" * 64
    for digest in (referenced, leased, candidate):
        (store.oci_blobs / digest).write_bytes(digest.encode())
    environment = store.environment_path(RETAINED)
    environment.mkdir()
    (environment / "metadata.json").write_text(
        json.dumps({"origin": {"blob_digests": [f"sha256:{referenced}"]}})
    )

    with store.oci_blob_lease([f"sha256:{leased}"]):
        report = store.clean_downloads(dry_run=False, minimum_age_seconds=0)
        assert report.removed == (candidate,)
        assert referenced in report.retained
        assert leased in report.retained
    assert store.clean_downloads(dry_run=False, minimum_age_seconds=0).removed == (leased,)


def test_oci_blob_gc_reports_reclaimed_bytes(tmp_path: Path) -> None:
    store = EnvironmentStore(tmp_path)
    digest = "4" * 64
    (store.oci_blobs / digest).write_bytes(b"payload")
    report = store.clean_downloads(dry_run=False, minimum_age_seconds=0)
    assert report.reclaimed_bytes == len(b"payload")


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


def test_environment_identity_ignores_informational_platform_details(monkeypatch) -> None:
    before = environment_identity(_sample_lock())
    monkeypatch.setattr("lean_runtime.store.platform.platform", lambda: "different-patch-release")
    assert environment_identity(_sample_lock()) == before
    assert "python_platform" not in platform_compatibility()


def test_source_snapshot_is_shallow_and_detects_content_changes(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "Sample.lean").write_text("def value := 1\n")
    subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
    subprocess.run(["git", "add", "."], cwd=checkout, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-q",
            "-m",
            "snapshot",
        ],
        cwd=checkout,
        check=True,
    )
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=checkout, text=True
    ).strip()
    tree_hash = subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=checkout, text=True
    ).strip()
    source_id = "source_" + "1" * 64
    metadata = {
        "source_id": source_id,
        "url": checkout.as_uri(),
        "revision": revision,
        "tree_hash": tree_hash,
    }
    store = EnvironmentStore(tmp_path / "store")
    source = store.publish_source(checkout, source_id, metadata)
    assert (
        subprocess.check_output(
            ["git", "remote", "get-url", "origin"], cwd=source, text=True
        ).strip()
        == checkout.as_uri()
    )
    assert (
        store.validate_source(
            source_id, **{k: metadata[k] for k in ("url", "revision", "tree_hash")}
        )
        == source
    )
    (source / "Sample.lean").write_text("def value := 2\n")
    with pytest.raises(EnvironmentError, match="content was modified"):
        store.validate_source(
            source_id,
            url=metadata["url"],
            revision=revision,
            tree_hash=tree_hash,
        )
