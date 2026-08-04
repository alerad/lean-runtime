from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from lean_runtime import EnvironmentError, EnvironmentLock, LockedPackage, Runtime
from lean_runtime.bundles import _oci_archive
from lean_runtime.environments import ENVIRONMENT_SCHEMA
from lean_runtime.store import environment_identity, platform_record, source_snapshot_digest


def _git_package(path: Path) -> tuple[str, str]:
    path.mkdir(parents=True)
    (path / "Sample.lean").write_text("def sampleValue : Nat := 41\n")
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "add", "Sample.lean"], cwd=path, check=True)
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
        cwd=path,
        check=True,
    )
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=path, text=True).strip()
    (path / ".lake" / "build" / "lib" / "lean").mkdir(parents=True)
    (path / ".lake" / "build" / "lib" / "lean" / "Sample.olean").write_bytes(b"built")
    return revision, tree


def _published_runtime(home: Path) -> tuple[Runtime, str, EnvironmentLock]:
    runtime = Runtime(home=home)
    package_root = home / "fixture-package"
    revision, tree = _git_package(package_root)
    package = LockedPackage(
        name="sample",
        url="https://example.test/sample.git",
        revision=revision,
        tree_hash=tree,
        source_id="source_" + "a" * 64,
        root_module="Sample",
    )
    (package_root / ".lean-runtime-source.json").write_text(
        json.dumps(
            {
                "source_id": package.source_id,
                "url": package.url,
                "revision": package.revision,
                "tree_hash": package.tree_hash,
                "content_hash": source_snapshot_digest(package_root),
            }
        )
    )
    lock = EnvironmentLock(
        toolchain="leanprover/lean4:v4.32.0",
        spec_digest="spec_" + "b" * 64,
        root_lakefile='name = "test"\n',
        root_module="import Sample\n",
        manifest={"version": "1.1.0", "packagesDir": ".lake/packages", "packages": []},
        packages=(package,),
    )
    runtime.store.publish_lock(lock)
    environment_id = environment_identity(lock)
    root = runtime.store.environment_path(environment_id)
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "lean-toolchain").write_text(lock.toolchain + "\n")
    (workspace / "lakefile.toml").write_text(lock.root_lakefile)
    (workspace / "LeanRuntimeEnvironment.lean").write_text(lock.root_module)
    (workspace / "lake-manifest.json").write_text(json.dumps(lock.manifest))
    package_destination = workspace / ".lake" / "packages" / "sample"
    shutil.copytree(package_root, package_destination)
    (workspace / ".lake" / "build").mkdir(parents=True)
    (root / "metadata.json").write_text(
        json.dumps(
            {
                "schema": ENVIRONMENT_SCHEMA,
                "environment_id": environment_id,
                "lock_id": lock.lock_id,
                "toolchain": lock.toolchain,
                "platform": platform_record(),
                "build_profile": "release",
                "status": "ready",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        )
    )
    return runtime, environment_id, lock


def _archive_entries(path: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            source = archive.extractfile(member)
            assert source is not None
            result[member.name] = source.read()
    return result


def test_bundle_export_is_deterministic_and_imports_into_fresh_store(tmp_path: Path) -> None:
    producer, environment_id, lock = _published_runtime(tmp_path / "producer")
    first = tmp_path / "first.oci.tar.gz"
    second = tmp_path / "second.oci.tar.gz"
    info = producer.export_environment(environment_id, first)
    producer.export_environment(environment_id, second)

    assert first.read_bytes() == second.read_bytes()
    assert info.lock_id == lock.lock_id
    assert info.manifest_digest.startswith("sha256:")

    consumer = Runtime(home=tmp_path / "consumer")
    imported = consumer.import_environment(first, name="portable", probe=False)
    assert imported.id == environment_id
    assert imported.lock.lock_id == lock.lock_id
    assert consumer.open("portable").id == environment_id
    assert not consumer.store.source_path(lock.packages[0].source_id).exists()
    metadata = json.loads((imported.root / "metadata.json").read_text())
    assert metadata["origin"]["kind"] == "prebuilt"
    bundled_package = imported.workspace / ".lake" / "packages" / "sample"
    assert (bundled_package / ".lake" / "build" / "lib" / "lean" / "Sample.olean").is_file()


def test_bundle_import_rejects_a_corrupted_blob(tmp_path: Path) -> None:
    producer, environment_id, _lock = _published_runtime(tmp_path / "producer")
    bundle = tmp_path / "environment.oci.tar.gz"
    producer.export_environment(environment_id, bundle)
    entries = _archive_entries(bundle)
    blob_name = next(name for name in entries if name.startswith("blobs/sha256/"))
    entries[blob_name] += b"corruption"
    corrupted = tmp_path / "corrupted.oci.tar.gz"
    corrupted.write_bytes(_oci_archive(entries))

    consumer = Runtime(home=tmp_path / "consumer")
    with pytest.raises(EnvironmentError, match="digest mismatch"):
        consumer.import_environment(corrupted, probe=False)
    assert not any(consumer.store.environments.glob("env_*"))


def test_bundle_bytes_have_stable_sha256(tmp_path: Path) -> None:
    producer, environment_id, _lock = _published_runtime(tmp_path / "producer")
    bundle = tmp_path / "environment.oci.tar.gz"
    producer.export_environment(environment_id, bundle)
    first = hashlib.sha256(bundle.read_bytes()).digest()
    producer.export_environment(environment_id, bundle)
    assert hashlib.sha256(bundle.read_bytes()).digest() == first


def test_bundle_export_rejects_workspace_content_that_diverges_from_lock(tmp_path: Path) -> None:
    producer, environment_id, _lock = _published_runtime(tmp_path / "producer")
    workspace = producer.store.environment_path(environment_id) / "workspace"
    (workspace / "lakefile.toml").write_text('name = "tampered"\n')
    with pytest.raises(EnvironmentError, match="root workspace does not match lock"):
        producer.export_environment(environment_id, tmp_path / "tampered.oci.tar.gz")


def test_bundle_export_rejects_modified_checked_out_package(tmp_path: Path) -> None:
    producer, environment_id, _lock = _published_runtime(tmp_path / "producer")
    package = (
        producer.store.environment_path(environment_id)
        / "workspace"
        / ".lake"
        / "packages"
        / "sample"
        / "Sample.lean"
    )
    package.write_text("def sampleValue : Nat := 666\n")
    with pytest.raises(EnvironmentError, match="checked-out content mismatch"):
        producer.export_environment(environment_id, tmp_path / "tampered.oci.tar.gz")
