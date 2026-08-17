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

from lean_runtime import (
    DownloadLimitExceeded,
    EnvironmentError,
    EnvironmentLock,
    LockedPackage,
    PublicationError,
    Runtime,
)
from lean_runtime.bundles import (
    SOURCE_TREE_INVENTORY,
    EnvironmentBundles,
    _capsule_config_object,
    _extract_layer,
    _oci_archive,
)
from lean_runtime.capsules import build_manifest
from lean_runtime.environments import ENVIRONMENT_SCHEMA
from lean_runtime.errors import CredentialAcquisitionError, RegistryRequestError
from lean_runtime.events import EventEmitter, RuntimeEvent
from lean_runtime.oci import (
    OCIEnvironmentPublisher,
    OCIRegistryClient,
    OCIRepository,
    PublicationAccess,
    RegistryCredential,
)
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
    package = (
        producer.store.environment_path(environment_id)
        / "workspace"
        / ".lake"
        / "packages"
        / "sample"
    )
    (package / ".lake" / "build" / "lib" / "lean" / "Sample.trace").write_text("/tmp/build-one\n")
    (package / ".lake" / "build" / "ir").mkdir(parents=True, exist_ok=True)
    (package / ".lake" / "build" / "ir" / "Sample.setup.json").write_text(
        '{"workspace":"/tmp/build-one"}\n'
    )
    (package / ".lake" / "build" / "bin").mkdir(parents=True, exist_ok=True)
    (package / ".lake" / "build" / "bin" / "sample.rsp").write_text("/tmp/build-one\n")
    info = producer.save_portable_copy(environment_id, first)
    (package / ".git" / "logs" / "nondeterministic-export-state").write_text("changed\n")
    (package / ".lake" / "build" / "lib" / "lean" / "Sample.trace").write_text("/tmp/build-two\n")
    (package / ".lake" / "build" / "ir" / "Sample.setup.json").write_text(
        '{"workspace":"/tmp/build-two"}\n'
    )
    (package / ".lake" / "build" / "bin" / "sample.rsp").write_text("/tmp/build-two\n")
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
    assert (bundled_package / SOURCE_TREE_INVENTORY).is_file()
    assert not (bundled_package / ".git").exists()


def test_capsule_layout_contains_only_indexed_sparse_artifacts(tmp_path: Path) -> None:
    producer, environment_id, lock = _published_runtime(tmp_path / "producer")
    workspace = producer.store.environment_path(environment_id) / "workspace"
    build = workspace / ".lake" / "packages" / "sample" / ".lake" / "build" / "lib" / "lean"
    manifest = build_manifest(
        workspace=workspace,
        environment_id=environment_id,
        lock_id=lock.lock_id,
        toolchain=lock.toolchain,
        build_roots={"sample": build},
        imports={"Sample": ()},
    )
    layout = tmp_path / "capsule-layout"
    info = producer.bundles.export_capsule_layout(
        environment_id, layout, capsule_manifest=manifest, probe=False
    )
    assert info.exact_environment_id == lock.lock_id
    index = json.loads((layout / "index.json").read_text())
    manifest_descriptor = index["manifests"][0]
    platform_manifest = json.loads(
        (layout / "blobs" / "sha256" / manifest_descriptor["digest"].split(":", 1)[1]).read_text()
    )
    assert platform_manifest["annotations"]["org.lean-runtime.profile"] == "check-capsule"
    config_descriptor = platform_manifest["config"]
    config = _capsule_config_object(
        (layout / "blobs" / "sha256" / config_descriptor["digest"].split(":", 1)[1]).read_bytes()
    )
    assert config["schema"] == "lean-runtime-oci-capsule/1"
    assert config["capsule"]["modules"][0]["name"] == "Sample"
    assert config["packs"][0]["frames"][0]["artifacts"] == [
        ".lake/packages/sample/.lake/build/lib/lean/Sample.olean"
    ]
    all_bytes = b"".join(path.read_bytes() for path in layout.rglob("*") if path.is_file())
    assert b"def sampleValue" not in all_bytes


def test_portable_capsule_imports_without_sources(tmp_path: Path) -> None:
    producer, environment_id, lock = _published_runtime(tmp_path / "producer")
    workspace = producer.store.environment_path(environment_id) / "workspace"
    build = workspace / ".lake" / "packages" / "sample" / ".lake" / "build" / "lib" / "lean"
    manifest = build_manifest(
        workspace=workspace,
        environment_id=environment_id,
        lock_id=lock.lock_id,
        toolchain=lock.toolchain,
        build_roots={"sample": build},
        imports={"Sample": ()},
    )
    layout = tmp_path / "layout"
    producer.bundles.export_capsule_layout(
        environment_id,
        layout,
        capsule_manifest=manifest,
        capabilities=frozenset({"check"}),
        probe=False,
    )
    archive = tmp_path / "sample.lean-capsule"
    archive.write_bytes(
        _oci_archive(
            {
                path.relative_to(layout).as_posix(): path.read_bytes()
                for path in layout.rglob("*")
                if path.is_file()
            }
        )
    )

    consumer = Runtime(home=tmp_path / "consumer", libraries=[])
    imported = consumer.open_portable_copy(archive, name="sample", probe=False)

    assert imported.id == environment_id
    assert (imported.workspace / ".lean-runtime" / "capsule.json").is_file()
    assert (
        imported.workspace
        / ".lake"
        / "packages"
        / "sample"
        / ".lake"
        / "build"
        / "lib"
        / "lean"
        / "Sample.olean"
    ).read_bytes() == b"built"
    assert not (imported.workspace / ".lake" / "packages" / "sample" / "Sample.lean").exists()
    assert consumer.environment("sample").id == environment_id


def test_oci_client_reads_verified_ranges_and_metadata_without_cache() -> None:
    payload = b"0123456789abcdef"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()

    class Handler(http.server.BaseHTTPRequestHandler):
        range_requests = 0

        def do_GET(self) -> None:  # noqa: N802
            if not self.path.endswith(digest):
                self.send_error(404)
                return
            requested = self.headers.get("Range")
            if requested:
                type(self).range_requests += 1
                start, end = (int(item) for item in requested.removeprefix("bytes=").split("-"))
                data = payload[start : end + 1]
                if type(self).range_requests == 2:
                    data = b"x" * len(data)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
            else:
                data = payload
                self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, _format: str, *args: object) -> None:
            del args

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        repository = OCIRepository.parse(f"oci+http://127.0.0.1:{server.server_port}/owner/cache")
        client = OCIRegistryClient(repository)
        descriptor = {"digest": digest, "size": len(payload)}
        assert client.download_blob_range(descriptor, offset=3, size=5) == b"34567"
        expected = "sha256:" + hashlib.sha256(b"34567").hexdigest()
        assert (
            client.download_blob_range(descriptor, offset=3, size=5, expected_digest=expected)
            == b"34567"
        )
        assert Handler.range_requests == 3
        assert client.read_blob(descriptor) == payload
    finally:
        server.shutdown()
        server.server_close()


def test_sparse_oci_pull_downloads_only_the_import_closure(tmp_path: Path) -> None:
    producer, environment_id, lock = _published_runtime(tmp_path / "producer")
    workspace = producer.store.environment_path(environment_id) / "workspace"
    build = workspace / ".lake" / "packages" / "sample" / ".lake" / "build" / "lib" / "lean"
    for module, byte in (("A", b"a"), ("B", b"b"), ("C", b"c")):
        (build / f"{module}.olean").write_bytes(byte * 700)
        (build / f"{module}.ilean").write_bytes(byte.upper() * 100)
    manifest = build_manifest(
        workspace=workspace,
        environment_id=environment_id,
        lock_id=lock.lock_id,
        toolchain=lock.toolchain,
        build_roots={"sample": build},
        imports={"A": (), "B": ("A",), "C": ("B",), "Sample": ()},
    )
    layout = tmp_path / "layout"
    producer.bundles.export_capsule_layout(
        environment_id,
        layout,
        capsule_manifest=manifest,
        target_frame_bytes=1024,
        probe=False,
    )
    index = (layout / "index.json").read_bytes()
    blobs = {
        "sha256:" + path.name: path.read_bytes() for path in (layout / "blobs" / "sha256").iterdir()
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        transferred_ranges: list[tuple[int, int]] = []
        token_requests: int = 0

        def do_GET(self) -> None:  # noqa: N802
            reference = urllib.parse.unquote(self.path.rsplit("/", 1)[-1])
            if self.path.startswith("/token"):
                type(self).token_requests += 1
                body = json.dumps({"token": "test-token"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
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
            if "/manifests/" in self.path:
                data = index if reference == f"capsule-{lock.lock_id}" else blobs.get(reference)
                if data is None:
                    self.send_error(404)
                    return
                media = (
                    "application/vnd.oci.image.index.v1+json"
                    if reference == f"capsule-{lock.lock_id}"
                    else "application/vnd.oci.image.manifest.v1+json"
                )
                self.send_response(200)
                self.send_header("Content-Type", media)
                self.send_header(
                    "Docker-Content-Digest",
                    "sha256:" + hashlib.sha256(data).hexdigest(),
                )
            else:
                data = blobs.get(reference)
                if data is None:
                    self.send_error(404)
                    return
                requested = self.headers.get("Range")
                if requested:
                    start, end = (int(item) for item in requested.removeprefix("bytes=").split("-"))
                    type(self).transferred_ranges.append((start, end))
                    whole = data
                    data = whole[start : end + 1]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(whole)}")
                else:
                    self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, _format: str, *args: object) -> None:
            del args

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        library = f"oci+http://127.0.0.1:{server.server_port}/owner/cache"
        events: list[RuntimeEvent] = []
        consumer = Runtime(home=tmp_path / "consumer", libraries=(library,), on_event=events.append)
        cache = consumer.libraries[0]
        before = set(consumer.store.cas_artifacts.iterdir())
        plan = cache.plan_capsule(lock, ("B",))
        assert set(consumer.store.cas_artifacts.iterdir()) == before
        assert plan.modules == ("A", "B")
        cache.pull_capsule(lock, ("B",))
        progress = [event for event in events if event.kind == "library.layer_progress"]
        assert [(event.data["frame_current"], event.data["frame_total"]) for event in progress] == [
            (1, 2),
            (2, 2),
        ]
        assert progress[-1].current_bytes == progress[-1].total_bytes
        assert progress[0].current_bytes is not None
        assert progress[1].current_bytes is not None
        assert progress[0].current_bytes < progress[1].current_bytes
        environment = consumer.environment(environment_id)
        assert list(environment.workspace.rglob("A.olean"))
        assert list(environment.workspace.rglob("B.olean"))
        assert not list(environment.workspace.rglob("C.olean"))
        assert len(Handler.transferred_ranges) == 2
        assert sum(frame.size for _pack, _descriptor, frame in plan.frames) < sum(
            pack.size for pack, _descriptor in plan.packs
        )
        environment.require_capabilities(["editor"], imports=["B"])
        assert list(environment.workspace.rglob("A.ilean"))
        assert list(environment.workspace.rglob("B.ilean"))
        assert not list(environment.workspace.rglob("C.ilean"))
        assert len(Handler.transferred_ranges) == 3
        assert Handler.token_requests >= 1
        bounded = Runtime(
            home=tmp_path / "bounded",
            libraries=(library,),
            max_download_bytes=1,
        )
        with pytest.raises(DownloadLimitExceeded):
            bounded.libraries[0].pull_capsule(lock, ("B",))
    finally:
        server.shutdown()
        server.server_close()


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

    def is_available_locally(self, _toolchain: str) -> bool:
        return True


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


def _publish_full_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let publisher transport tests skip capsule construction.

    Building a real capsule layout resolves the locked toolchain so it can index
    Lean artifacts, which would install Elan inside a unit test. These tests
    assert upload ordering and failure classification, not layout content, so a
    deterministic full layout is a faithful stand-in.
    """
    monkeypatch.setattr(
        EnvironmentBundles,
        "export_capsule_layout",
        lambda self, environment_id, output, **_kwargs: EnvironmentBundles.export_layout(
            self, environment_id, output
        ),
    )


def test_oci_publisher_uploads_blobs_before_manifests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_full_layout(monkeypatch)
    producer, environment_id, lock = _published_runtime(tmp_path / "producer")
    publisher = OCIEnvironmentPublisher(
        OCIRepository.parse("oci://registry.example/owner/cache"),
        producer.store,
        producer.bundles,
        producer.events,
    )
    operations: list[tuple[str, str]] = []

    class FakeClient:
        def check_push_access(self) -> None:
            pass

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
    assert result.uploaded_bytes == result.total_blob_bytes
    assert result.reused_bytes == 0
    assert result.reuse_percent == 0
    assert operations[-2:] == [
        ("manifest", f"capsule-{lock.lock_id}"),
        ("manifest", "v1"),
    ]


def test_oci_publisher_reports_remote_blob_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_full_layout(monkeypatch)
    producer, environment_id, _lock = _published_runtime(tmp_path / "producer")
    publisher = OCIEnvironmentPublisher(
        OCIRepository.parse("oci://registry.example/owner/cache"),
        producer.store,
        producer.bundles,
        producer.events,
    )

    class ReusingClient:
        def check_push_access(self) -> None:
            pass

        def manifest_exists(self, _digest: str) -> bool:
            return True

        def blob_exists(self, _digest: str) -> bool:
            return True

        def upload_blob(self, _path: Path, _digest: str) -> None:
            pass

        def publish_manifest(self, _reference: str, data: bytes, _media_type: str) -> str:
            return "sha256:" + hashlib.sha256(data).hexdigest()

    publisher.client = ReusingClient()  # type: ignore[assignment]
    result = publisher.publish(environment_id)
    assert result.uploaded_files == 0
    assert result.uploaded_bytes == 0
    assert result.reused_bytes == result.total_blob_bytes
    assert result.reuse_percent == 100


def test_verified_publication_session_is_reused_by_runtime(tmp_path: Path) -> None:
    producer, environment_id, _lock = _published_runtime(tmp_path / "producer")
    access_checks = 0
    publishes = 0

    class VerifiedSession:
        repository = OCIRepository.parse("oci://registry.example/owner/cache")

        def check_access(self) -> PublicationAccess:
            nonlocal access_checks
            access_checks += 1
            return PublicationAccess(self.repository.display, "owner", "environment", True)

        def publish(self, _environment_id: str, **_kwargs: object) -> object:
            nonlocal publishes
            publishes += 1
            return object()

        def complete(self, _result: object) -> None:
            pass

    publisher = VerifiedSession()
    publisher.check_access()
    producer.publish_environment(
        environment_id,
        "oci://registry.example/owner/cache",
        finalize=False,
        publisher=publisher,  # type: ignore[arg-type]
    )

    assert access_checks == 1
    assert publishes == 1


def test_oci_manifest_publication_requires_remote_digest_verification() -> None:
    manifest_data = b'{"schemaVersion":2}'

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_PUT(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(201)
            self.end_headers()

        def do_GET(self) -> None:
            remote_data = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.oci.image.manifest.v1+json")
            self.send_header("Content-Length", str(len(remote_data)))
            self.end_headers()
            self.wfile.write(remote_data)

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        repository = OCIRepository.parse(f"oci+http://127.0.0.1:{server.server_port}/owner/cache")
        with pytest.raises(EnvironmentError, match="remote digest verification"):
            OCIRegistryClient(repository).publish_manifest(
                "release",
                manifest_data,
                "application/vnd.oci.image.manifest.v1+json",
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_oci_publisher_denies_access_before_exporting(tmp_path: Path, monkeypatch) -> None:
    _publish_full_layout(monkeypatch)
    producer, environment_id, _lock = _published_runtime(tmp_path / "producer")
    events: list[RuntimeEvent] = []
    publisher = OCIEnvironmentPublisher(
        OCIRepository.parse("oci://ghcr.io/owner/cache"),
        producer.store,
        producer.bundles,
        EventEmitter(events.append),
        credential=RegistryCredential("owner", "secret", "GitHub CLI"),
    )

    class DeniedClient:
        def check_push_access(self) -> None:
            raise RegistryRequestError(
                "HTTP 403",
                operation="push access probe",
                status_code=403,
            )

    publisher.client = DeniedClient()  # type: ignore[assignment]

    def unexpected_export(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("publication exported an environment before checking access")

    monkeypatch.setattr(producer.bundles, "export_layout", unexpected_export)
    with pytest.raises(PublicationError) as raised:
        publisher.publish(environment_id)

    failure = raised.value
    assert failure.exit_code == 3
    assert failure.published is False
    assert failure.partial is False
    assert failure.status_code == 403
    assert failure.credential_source == "GitHub CLI"
    assert failure.hint == "run `gh auth refresh -s write:packages,read:packages`, then retry"
    assert [event.kind for event in events] == [
        "library.publish_auth_selected",
        "library.publish_failed",
    ]
    assert events[-1].data["published"] is False
    assert "secret" not in json.dumps(events[-1].to_dict())


def test_ghcr_credential_discovery_reads_identity_and_token_once(monkeypatch) -> None:
    monkeypatch.delenv("LEAN_RUNTIME_REGISTRY_USERNAME", raising=False)
    monkeypatch.delenv("LEAN_RUNTIME_REGISTRY_PASSWORD", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/gh")
    calls: list[tuple[str, ...]] = []

    def status(command: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        payload = {
            "hosts": {
                "github.com": [
                    {
                        "active": True,
                        "state": "success",
                        "login": "owner",
                        "token": "secret",
                    }
                ]
            }
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(subprocess, "run", status)
    credential = RegistryCredential.discover(OCIRepository.parse("oci://ghcr.io/owner/cache"))

    assert credential == RegistryCredential("owner", "secret", "GitHub CLI")
    assert len(calls) == 1
    assert calls[0][:3] == ("gh", "auth", "status")
    assert "api" not in calls[0]


def test_ghcr_credential_discovery_fails_when_token_is_unavailable(
    monkeypatch,
) -> None:
    monkeypatch.delenv("LEAN_RUNTIME_REGISTRY_USERNAME", raising=False)
    monkeypatch.delenv("LEAN_RUNTIME_REGISTRY_PASSWORD", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/gh")
    payload = {
        "hosts": {
            "github.com": [{"active": True, "state": "failed", "login": "owner", "token": ""}]
        }
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, json.dumps(payload), ""),
    )

    with pytest.raises(CredentialAcquisitionError, match="did not provide a usable token"):
        RegistryCredential.discover(OCIRepository.parse("oci://ghcr.io/owner/cache"))


def test_partial_environment_credentials_fail_as_authentication_error(monkeypatch) -> None:
    monkeypatch.setenv("LEAN_RUNTIME_REGISTRY_USERNAME", "owner")
    monkeypatch.delenv("LEAN_RUNTIME_REGISTRY_PASSWORD", raising=False)

    with pytest.raises(CredentialAcquisitionError) as raised:
        RegistryCredential.discover(OCIRepository.parse("oci://registry.example/owner/cache"))

    assert raised.value.provider == "environment"
    assert raised.value.failure_kind == "invalid_configuration"
    assert raised.value.retryable is False


def test_ghcr_credential_timeout_fails_closed_as_retryable_publication(
    tmp_path: Path, monkeypatch
) -> None:
    producer, _environment_id, _lock = _published_runtime(tmp_path / "producer")
    monkeypatch.delenv("LEAN_RUNTIME_REGISTRY_USERNAME", raising=False)
    monkeypatch.delenv("LEAN_RUNTIME_REGISTRY_PASSWORD", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/gh")

    def timeout(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(PublicationError) as raised:
        OCIEnvironmentPublisher(
            OCIRepository.parse("oci://ghcr.io/owner/cache"),
            producer.store,
            producer.bundles,
            producer.events,
            auth_timeout=42,
        )

    failure = raised.value
    assert failure.exit_code == 4
    assert failure.phase == "credential_acquisition"
    assert failure.credential_source == "none"
    assert failure.attempted_provider == "GitHub CLI"
    assert failure.auth_failure_kind == "timeout"


def test_oci_publisher_classifies_retryable_preflight_failure(tmp_path: Path) -> None:
    producer, _environment_id, _lock = _published_runtime(tmp_path / "producer")
    publisher = OCIEnvironmentPublisher(
        OCIRepository.parse("oci://registry.example/owner/cache"),
        producer.store,
        producer.bundles,
        producer.events,
    )

    class UnavailableClient:
        def check_push_access(self) -> None:
            raise RegistryRequestError(
                "HTTP 503",
                operation="push access probe",
                status_code=503,
                retryable=True,
            )

    publisher.client = UnavailableClient()  # type: ignore[assignment]
    with pytest.raises(PublicationError) as raised:
        publisher.check_access()
    assert raised.value.exit_code == 4
    assert raised.value.retryable is True
    assert raised.value.partial is False


def test_oci_publisher_treats_unverified_manifest_write_as_indeterminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_full_layout(monkeypatch)
    producer, environment_id, _lock = _published_runtime(tmp_path / "producer")
    events: list[RuntimeEvent] = []
    publisher = OCIEnvironmentPublisher(
        OCIRepository.parse("oci://registry.example/owner/cache"),
        producer.store,
        producer.bundles,
        EventEmitter(events.append),
    )

    class UnverifiedClient:
        def check_push_access(self) -> None:
            pass

        def blob_exists(self, _digest: str) -> bool:
            return True

        def upload_blob(self, _path: Path, _digest: str) -> None:
            pass

        def publish_manifest(self, _reference: str, _data: bytes, _media_type: str) -> str:
            raise EnvironmentError("published manifest could not be read back")

    publisher.client = UnverifiedClient()  # type: ignore[assignment]
    with pytest.raises(PublicationError) as raised:
        publisher.publish(environment_id)

    failure = raised.value
    assert failure.exit_code == 5
    assert failure.partial is True
    assert failure.published is False
    assert failure.phase == "platform_manifest"
    assert [event.kind for event in events].count("library.publish_failed") == 1
    assert not any(event.kind == "library.published" for event in events)


def test_oci_publisher_reports_partial_state_when_tag_finalization_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_full_layout(monkeypatch)
    producer, environment_id, _lock = _published_runtime(tmp_path / "producer")
    events: list[RuntimeEvent] = []
    publisher = OCIEnvironmentPublisher(
        OCIRepository.parse("oci://registry.example/owner/cache"),
        producer.store,
        producer.bundles,
        EventEmitter(events.append),
    )

    class TagFailureClient:
        def check_push_access(self) -> None:
            pass

        def manifest_exists(self, _digest: str) -> bool:
            return True

        def blob_exists(self, _digest: str) -> bool:
            return True

        def upload_blob(self, _path: Path, _digest: str) -> None:
            pass

        def publish_manifest(self, reference: str, data: bytes, _media_type: str) -> str:
            if reference == "release":
                raise RegistryRequestError(
                    "HTTP 503",
                    operation="manifest upload",
                    status_code=503,
                    retryable=True,
                )
            return "sha256:" + hashlib.sha256(data).hexdigest()

    publisher.client = TagFailureClient()  # type: ignore[assignment]
    with pytest.raises(PublicationError) as raised:
        publisher.publish(environment_id, tags=("release",))

    failure = raised.value
    assert failure.exit_code == 5
    assert failure.partial is True
    assert failure.published is False
    assert failure.phase == "tag_finalization"
    terminal = [event for event in events if event.kind == "library.publish_failed"]
    assert len(terminal) == 1
    assert terminal[0].data["partial"] is True
    assert not any(event.kind == "library.published" for event in events)


def test_oci_truncated_blob_download_resumes_with_range(tmp_path: Path) -> None:
    data = b"large layer contents " * 512
    cut = len(data) // 3
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    ranges: list[str | None] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            ranges.append(self.headers.get("Range"))
            if self.headers.get("Range") is None:
                # A cleanly closed, truncated transfer: fewer bytes than the
                # blob's declared size, delivered as a complete response.
                self.send_response(200)
                self.send_header("Content-Length", str(cut))
                self.end_headers()
                self.wfile.write(data[:cut])
                return
            self.send_response(206)
            remainder = data[cut:]
            self.send_header("Content-Range", f"bytes {cut}-{len(data) - 1}/{len(data)}")
            self.send_header("Content-Length", str(len(remainder)))
            self.end_headers()
            self.wfile.write(remainder)

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
        assert ranges == [None, f"bytes={cut}-"]
        retries = [event for event in events if event.kind == "library.layer_download_retry"]
        assert len(retries) == 1
        assert retries[0].data["truncated"] is True
        starts = [event for event in events if event.kind == "library.layer_download_started"]
        assert starts[1].data["resumed_bytes"] == cut
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_open_exact_announces_source_build_capability(tmp_path: Path, monkeypatch) -> None:
    producer, _environment_id, lock = _published_runtime(tmp_path / "producer")
    events = []
    runtime = Runtime(
        toolchains=_FakeToolchains(tmp_path / "consumer"),  # type: ignore[arg-type]
        availability="auto",
        libraries=["oci+http://127.0.0.1:1/owner/cache"],
        on_event=events.append,
    )
    sentinel = object()
    monkeypatch.setattr(runtime.environments, "ensure", lambda _lock, **_kw: sentinel)
    assert runtime.open_exact(lock) is sentinel
    kinds = [event.kind for event in events]
    assert "availability.fallback" in kinds
    required = next(event for event in events if event.kind == "capability.required")
    assert required.data["capability"] == "source_build"


def test_download_limit_fails_closed_when_a_component_cannot_be_priced(tmp_path: Path) -> None:
    _producer, _environment_id, lock = _published_runtime(tmp_path / "producer")
    runtime = Runtime(
        toolchains=_FakeToolchains(tmp_path / "consumer"),  # type: ignore[arg-type]
        libraries=[],
        max_download_bytes=10**12,
    )

    report = runtime.plan_exact(lock)
    assert report["download_bytes_complete"] is False
    with pytest.raises(DownloadLimitExceeded, match="cost is incomplete"):
        runtime.open_exact(lock)
