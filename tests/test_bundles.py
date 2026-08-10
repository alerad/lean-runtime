from __future__ import annotations

import hashlib
import http.server
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import urllib.parse
from pathlib import Path

import pytest

from lean_runtime import EnvironmentError, EnvironmentLock, LockedPackage, Runtime
from lean_runtime.bundles import _extract_layer, _oci_archive
from lean_runtime.environments import ENVIRONMENT_SCHEMA
from lean_runtime.events import EventEmitter
from lean_runtime.oci import OCIEnvironmentPublisher, OCIRegistryClient, OCIRepository
from lean_runtime.store import (
    EnvironmentStore,
    environment_identity,
    platform_record,
    source_snapshot_digest,
)


def _git_package(path: Path) -> tuple[str, str]:
    path.mkdir(parents=True)
    (path / "Sample.lean").write_text("def sampleValue : Nat := 41\n")
    (path / ".gitignore").write_text("generated.hash\n")
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "add", "Sample.lean", ".gitignore"], cwd=path, check=True)
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


def _layer(*members: tuple[str, str, bytes | str]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        for kind, name, value in members:
            info = tarfile.TarInfo(name)
            info.mode = 0o644
            if kind == "file":
                assert isinstance(value, bytes)
                info.size = len(value)
                archive.addfile(info, io.BytesIO(value))
            else:
                assert kind == "symlink" and isinstance(value, str)
                info.type = tarfile.SYMTYPE
                info.linkname = value
                archive.addfile(info)
    return output.getvalue()


@pytest.mark.skipif(os.name == "nt", reason="Windows runners may not permit symlink creation")
def test_layer_extract_accepts_internal_parent_symlink(tmp_path: Path) -> None:
    layer = _layer(
        ("file", "README.md", b"Batteries\n"),
        ("symlink", "docs/README.md", "../README.md"),
    )

    _extract_layer(layer, tmp_path / "output")

    link = tmp_path / "output" / "docs" / "README.md"
    assert link.is_symlink()
    assert link.read_text() == "Batteries\n"


@pytest.mark.skipif(os.name == "nt", reason="Windows runners may not permit symlink creation")
def test_layer_extract_rejects_symlink_outside_destination(tmp_path: Path) -> None:
    layer = _layer(("symlink", "docs/README.md", "../../outside"))

    with pytest.raises(EnvironmentError, match="unsafe bundle symlink"):
        _extract_layer(layer, tmp_path / "output")


@pytest.mark.skipif(os.name == "nt", reason="Windows runners may not permit symlink creation")
def test_layer_extract_rejects_later_write_through_symlink(tmp_path: Path) -> None:
    layer = _layer(
        ("symlink", "redirect", "inside"),
        ("file", "redirect/payload", b"must not be written"),
    )

    with pytest.raises(EnvironmentError, match="traverses an extracted symlink"):
        _extract_layer(layer, tmp_path / "output")


def test_bundle_export_is_deterministic_and_imports_into_fresh_store(tmp_path: Path) -> None:
    producer, environment_id, lock = _published_runtime(tmp_path / "producer")
    first = tmp_path / "first.oci.tar.gz"
    second = tmp_path / "second.oci.tar.gz"
    info = producer.save_portable_copy(environment_id, first)
    producer.save_portable_copy(environment_id, second)

    assert first.read_bytes() == second.read_bytes()
    assert info.exact_environment_id == lock.lock_id
    assert info.copy_id.startswith("sha256:")

    consumer = Runtime(home=tmp_path / "consumer")
    imported = consumer.open_portable_copy(first, name="portable", probe=False)
    assert imported.id == environment_id
    assert imported.lock.lock_id == lock.lock_id
    assert consumer.environment("portable").id == environment_id
    assert not consumer.store.source_path(lock.packages[0].source_id).exists()
    metadata = json.loads((imported.root / "metadata.json").read_text())
    assert metadata["origin"]["kind"] == "portable_copy"
    bundled_package = imported.workspace / ".lake" / "packages" / "sample"
    assert (bundled_package / ".lake" / "build" / "lib" / "lean" / "Sample.olean").is_file()


def test_bundle_import_rejects_a_corrupted_blob(tmp_path: Path) -> None:
    producer, environment_id, _lock = _published_runtime(tmp_path / "producer")
    bundle = tmp_path / "environment.oci.tar.gz"
    producer.save_portable_copy(environment_id, bundle)
    entries = _archive_entries(bundle)
    blob_name = next(name for name in entries if name.startswith("blobs/sha256/"))
    entries[blob_name] += b"corruption"
    corrupted = tmp_path / "corrupted.oci.tar.gz"
    corrupted.write_bytes(_oci_archive(entries))

    consumer = Runtime(home=tmp_path / "consumer")
    with pytest.raises(EnvironmentError, match="digest mismatch"):
        consumer.open_portable_copy(corrupted, probe=False)
    assert not any(consumer.store.environments.glob("env_*"))


def test_bundle_bytes_have_stable_sha256(tmp_path: Path) -> None:
    producer, environment_id, _lock = _published_runtime(tmp_path / "producer")
    bundle = tmp_path / "environment.oci.tar.gz"
    producer.save_portable_copy(environment_id, bundle)
    first = hashlib.sha256(bundle.read_bytes()).digest()
    producer.save_portable_copy(environment_id, bundle)
    assert hashlib.sha256(bundle.read_bytes()).digest() == first


def test_bundle_export_rejects_workspace_content_that_diverges_from_lock(tmp_path: Path) -> None:
    producer, environment_id, _lock = _published_runtime(tmp_path / "producer")
    workspace = producer.store.environment_path(environment_id) / "workspace"
    (workspace / "lakefile.toml").write_text('name = "tampered"\n')
    with pytest.raises(EnvironmentError, match="root workspace does not match lock"):
        producer.save_portable_copy(environment_id, tmp_path / "tampered.oci.tar.gz")


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
        producer.save_portable_copy(environment_id, tmp_path / "tampered.oci.tar.gz")


def test_bundle_export_accepts_gitignored_generated_build_artifact(tmp_path: Path) -> None:
    producer, environment_id, _lock = _published_runtime(tmp_path / "producer")
    package = (
        producer.store.environment_path(environment_id)
        / "workspace"
        / ".lake"
        / "packages"
        / "sample"
    )
    (package / "generated.hash").write_text("derived build state\n")

    bundle = tmp_path / "generated.oci.tar.gz"
    producer.save_portable_copy(environment_id, bundle)

    assert bundle.is_file()


def test_bundle_export_rejects_untracked_nonignored_package_content(tmp_path: Path) -> None:
    producer, environment_id, _lock = _published_runtime(tmp_path / "producer")
    package = (
        producer.store.environment_path(environment_id)
        / "workspace"
        / ".lake"
        / "packages"
        / "sample"
    )
    (package / "unexpected.txt").write_text("not a declared source or ignored build artifact\n")

    with pytest.raises(EnvironmentError, match="checked-out content mismatch"):
        producer.save_portable_copy(environment_id, tmp_path / "tampered.oci.tar.gz")


class _FakeToolchains:
    def __init__(self, home: Path) -> None:
        self.home = home

    @property
    def environment(self) -> dict[str, str]:
        return os.environ.copy()

    def command(self, _toolchain: str, _executable: str, *_args: str) -> list[str]:
        return [sys.executable, "-c", "raise SystemExit(0)"]


def test_transparent_authenticated_oci_pull_and_blob_reuse(tmp_path: Path) -> None:
    producer, environment_id, lock = _published_runtime(tmp_path / "producer")
    bundle = tmp_path / "environment.oci.tar.gz"
    producer.save_portable_copy(environment_id, bundle)
    entries = _archive_entries(bundle)
    index = json.loads(entries["index.json"])
    manifest_descriptor = index["manifests"][0]
    manifest_digest = manifest_descriptor["digest"]
    manifest = entries["blobs/sha256/" + manifest_digest.removeprefix("sha256:")]
    requests: list[str] = []
    ranges: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            if self.path.startswith("/token"):
                self._send(json.dumps({"token": "test-token"}).encode(), "application/json")
                return
            if self.headers.get("Authorization") != "Bearer test-token":
                realm = f"http://127.0.0.1:{server.server_port}/token"
                self.send_response(401)
                self.send_header(
                    "WWW-Authenticate",
                    f'Bearer realm="{realm}",service="fixture",scope="repository:owner/cache:pull"',
                )
                self.end_headers()
                return
            prefix = "/v2/owner/cache/manifests/"
            if self.path == prefix + lock.lock_id:
                data = entries["index.json"]
                self._send(data, "application/vnd.oci.image.index.v1+json")
                return
            if self.path == prefix + urllib.parse.quote(manifest_digest, safe=":"):
                self._send(manifest, "application/vnd.oci.image.manifest.v1+json")
                return
            blob_prefix = "/v2/owner/cache/blobs/"
            if self.path.startswith(blob_prefix):
                digest = self.path.removeprefix(blob_prefix)
                blob_data = entries.get("blobs/sha256/" + digest.removeprefix("sha256:"))
                if blob_data is not None:
                    range_header = self.headers.get("Range")
                    if range_header:
                        ranges.append(range_header)
                        offset = int(range_header.removeprefix("bytes=").removesuffix("-"))
                        self._send(
                            blob_data[offset:],
                            "application/octet-stream",
                            status=206,
                            content_range=f"bytes {offset}-{len(blob_data) - 1}/{len(blob_data)}",
                            digest_source=blob_data,
                        )
                    else:
                        self._send(blob_data, "application/octet-stream")
                    return
            self.send_response(404)
            self.end_headers()

        def _send(
            self,
            data: bytes,
            content_type: str,
            *,
            status: int = 200,
            content_range: str | None = None,
            digest_source: bytes | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            if content_range:
                self.send_header("Content-Range", content_range)
            digest_data = digest_source if digest_source is not None else data
            self.send_header(
                "Docker-Content-Digest", "sha256:" + hashlib.sha256(digest_data).hexdigest()
            )
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cache = f"oci+http://127.0.0.1:{server.server_port}/owner/cache"
    try:
        consumer_home = tmp_path / "consumer"
        runtime = Runtime(
            toolchains=_FakeToolchains(consumer_home),  # type: ignore[arg-type]
            availability="required",
            libraries=[cache],
        )
        manifest_value = json.loads(manifest)
        resumed_descriptor = manifest_value["layers"][0]
        resumed_data = entries[
            "blobs/sha256/" + resumed_descriptor["digest"].removeprefix("sha256:")
        ]
        partial = runtime.store.oci_blobs / (
            "." + resumed_descriptor["digest"].removeprefix("sha256:") + ".partial"
        )
        partial.write_bytes(resumed_data[: len(resumed_data) // 2])
        imported = runtime.open_exact(lock)
        assert imported.id == environment_id
        imported_metadata = json.loads((imported.root / "metadata.json").read_text())
        assert imported_metadata["origin"]["library"] == cache
        first_blob_requests = len([path for path in requests if "/blobs/" in path])
        assert ranges

        shutil.rmtree(runtime.store.environment_path(environment_id))
        runtime.open_exact(lock)
        assert len([path for path in requests if "/blobs/" in path]) == first_blob_requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_oci_blob_integrity_failure_retries_once_from_scratch(tmp_path: Path) -> None:
    data = b"verified layer contents"
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    requests = 0
    cache_controls: list[str | None] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            nonlocal requests
            requests += 1
            cache_controls.append(self.headers.get("Cache-Control"))
            response = b"x" * len(data) if requests == 1 else data
            self.send_response(200)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    events = []
    try:
        repository = OCIRepository.parse(f"oci+http://127.0.0.1:{server.server_port}/owner/cache")
        path = OCIRegistryClient(repository).download_blob(
            {"digest": digest, "size": len(data)},
            EnvironmentStore(tmp_path / "consumer"),
            EventEmitter(events.append),
        )
        assert path.read_bytes() == data
        assert requests == 2
        assert cache_controls == [None, "no-cache"]
        assert [event.kind for event in events].count("library.layer_download_retry") == 1
        starts = [event for event in events if event.kind == "library.layer_download_started"]
        assert [event.data["attempt"] for event in starts] == [1, 2]
        assert starts[1].data["resumed_bytes"] == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_oci_publisher_uploads_blobs_before_manifests(tmp_path: Path) -> None:
    producer, environment_id, lock = _published_runtime(tmp_path / "producer")
    publisher = OCIEnvironmentPublisher(
        OCIRepository.parse("oci://registry.example/owner/cache"),
        producer.store,
        producer.bundles,
        producer.events,
    )
    operations: list[tuple[str, str]] = []

    class FakeClient:
        def manifest_exists(self, _digest: str) -> bool:
            return True

        def blob_exists(self, digest: str) -> bool:
            operations.append(("exists", digest))
            return False

        def upload_blob(self, path: Path, digest: str) -> None:
            assert path.is_file()
            assert "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() == digest
            operations.append(("blob", digest))

        def publish_manifest(self, reference: str, data: bytes, _media_type: str) -> str:
            operations.append(("manifest", reference))
            return "sha256:" + hashlib.sha256(data).hexdigest()

    publisher.client = FakeClient()  # type: ignore[assignment]
    result = publisher.publish(environment_id, tags=("v1",))
    kinds = [kind for kind, _value in operations]
    first_manifest = kinds.index("manifest")
    assert all(kind in {"exists", "blob"} for kind in kinds[:first_manifest])
    assert result.exact_environment_id == lock.lock_id
    assert result.uploaded_files == 3
    assert operations[-2:] == [("manifest", lock.lock_id), ("manifest", "v1")]
