from __future__ import annotations

import asyncio
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from lean_runtime import PackageReference, Runtime, RuntimeEvent


@contextmanager
def _bundle_registry(bundle: Path, lock_id: str) -> Iterator[str]:
    entries: dict[str, bytes] = {}
    with tarfile.open(bundle, "r:gz") as archive:
        for member in archive:
            source = archive.extractfile(member)
            assert source is not None
            entries[member.name] = source.read()
    index = json.loads(entries["index.json"])
    manifest_digest = index["manifests"][0]["digest"]

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            manifest_prefix = "/v2/owner/cache/manifests/"
            if self.path == manifest_prefix + lock_id:
                self._send(entries["index.json"], "application/vnd.oci.image.index.v1+json")
                return
            if self.path == manifest_prefix + urllib.parse.quote(manifest_digest, safe=":"):
                self._send(
                    entries["blobs/sha256/" + manifest_digest.removeprefix("sha256:")],
                    "application/vnd.oci.image.manifest.v1+json",
                )
                return
            blob_prefix = "/v2/owner/cache/blobs/"
            if self.path.startswith(blob_prefix):
                digest = self.path.removeprefix(blob_prefix)
                data = entries.get("blobs/sha256/" + digest.removeprefix("sha256:"))
                if data is not None:
                    self._send(data, "application/octet-stream")
                    return
            self.send_response(404)
            self.end_headers()

        def _send(self, data: bytes, media_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", media_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Docker-Content-Digest", "sha256:" + hashlib.sha256(data).hexdigest())
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"oci+http://127.0.0.1:{server.server_port}/owner/cache"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _sample_package(path: Path) -> str:
    path.mkdir()
    (path / "lean-toolchain").write_text("leanprover/lean4:v4.32.0\n")
    (path / "lakefile.toml").write_text(
        'name = "sample"\nversion = "0.1.0"\n\n[[lean_lib]]\nname = "Sample"\n'
    )
    (path / "Sample.lean").write_text("def sampleValue : Nat := 41\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Lean Runtime Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-q",
            "-m",
            "initial",
        ],
        cwd=path,
        check=True,
    )
    subprocess.run(["git", "tag", "v1.0.0"], cwd=path, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


@pytest.mark.integration
def test_local_lake_project_build_and_file_check(tmp_path: Path) -> None:
    root = tmp_path / "local-project"
    root.mkdir()
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.32.0\n")
    (root / "lakefile.toml").write_text(
        'name = "local_project"\n\n[[lean_lib]]\n'
        'name = "LocalProject"\nroots = ["LocalProject.Defs"]\n'
    )
    library = root / "LocalProject"
    library.mkdir()
    (library / "Defs.lean").write_text("def answer : Nat := 42\n")
    main = library / "Main.lean"
    main.write_text("import LocalProject.Defs\nexample : answer = 42 := by rfl\n")

    runtime = Runtime(home=tmp_path / "runtime", caches=[])
    project = runtime.project(main)
    assert project.build(["LocalProject"]).ok
    result = project.check_file(main)
    assert result.ok, result.stdout + result.stderr
    assert result.environment_id is None
    assert result.provenance is not None and result.provenance.project is not None
    assert result.provenance.project.root == str(root)
    scratch = project.check("import LocalProject.Defs\nexample : answer = 42 := by rfl\n")
    assert scratch.ok, scratch.stdout + scratch.stderr
    child_environment = os.environ.copy()
    child_environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "lean_runtime",
            "--home",
            str(tmp_path / "runtime"),
            "--quiet",
            "raw-check",
            str(main),
            "--json",
        ],
        env=child_environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert cli.returncode == 0, cli.stdout + cli.stderr
    cli_result = json.loads(cli.stdout)
    assert cli_result["provenance"]["project"]["root"] == str(root)


@pytest.mark.integration
def test_resolve_publish_and_reopen_offline_from_second_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing_elan = os.environ.get("LEAN_RUNTIME_TEST_ELAN")
    existing_home = os.environ.get("LEAN_RUNTIME_TEST_ELAN_HOME")
    if existing_elan:
        monkeypatch.setenv("LEAN_RUNTIME_ELAN", existing_elan)
    if existing_home:
        monkeypatch.setenv("LEAN_RUNTIME_ELAN_HOME", existing_home)
    package = tmp_path / "sample"
    revision = _sample_package(package)
    resolver_home = tmp_path / "resolver-runtime"
    runtime_home = tmp_path / "consumer-runtime"
    events: list[RuntimeEvent] = []
    runtime = Runtime(home=resolver_home, on_event=events.append)
    spec = runtime.spec_from_references([PackageReference.git(package.as_uri(), "v1.0.0")])
    assert spec.toolchain == "leanprover/lean4:v4.32.0"
    assert spec.packages[0].rev == revision
    assert spec.packages[0].module == "Sample"
    lock = runtime.resolve(spec)
    assert lock.packages[0].requested_revision == revision
    assert lock.packages[0].revision == revision
    assert revision in lock.root_lakefile
    assert "v1.0.0" not in lock.root_lakefile
    assert {event.kind for event in events} >= {
        "resolution.started",
        "package_reference.resolved",
        "source.locked",
        "resolution.completed",
    }
    lock_path = tmp_path / "environment.lock.json"
    lock.write(lock_path)
    child_environment = os.environ.copy()
    child_environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    ensure_code = (
        "import sys; from lean_runtime import EnvironmentLock,Runtime; "
        "runtime=Runtime(home=sys.argv[1],caches=[]); "
        "environment=runtime.ensure(EnvironmentLock.load(sys.argv[2]), name='demo'); "
        "print(environment.id)"
    )
    builders = [
        subprocess.Popen(
            [sys.executable, "-c", ensure_code, str(runtime_home), str(lock_path)],
            env=child_environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    try:
        # A completely cold cache may need to download and install the selected
        # Lean toolchain before either contender can enter the build lock.
        built = [builder.communicate(timeout=600) for builder in builders]
    finally:
        for builder in builders:
            if builder.poll() is None:
                builder.kill()
                builder.communicate()
    assert all(builder.returncode == 0 for builder in builders), built
    identities = {stdout.strip() for stdout, _ in built}
    assert len(identities) == 1
    runtime = Runtime(home=runtime_home)
    environment = runtime.open("demo")
    assert identities == {environment.id}
    audit = runtime.audit("demo", rebuild=True)
    assert audit.ok
    assert audit.rebuilt_artifacts is not None
    assert isinstance(audit.artifact_match, bool)
    source = "import Sample\nexample : sampleValue = 41 := by rfl\n"
    first = environment.check(source)
    assert first.ok
    assert first.environment_id == environment.id
    assert first.lock_id == lock.lock_id
    assert first.provenance is not None
    assert first.provenance.packages[0].revision == revision
    repeated = environment.check(source)
    assert repeated.execution_id != first.execution_id
    assert repeated.provenance is not None
    assert repeated.provenance.request_digest == first.provenance.request_digest
    bundle_path = tmp_path / "environment.oci.tar.gz"
    exported = runtime.export_environment(environment.id, bundle_path)
    with _bundle_registry(bundle_path, lock.lock_id) as cache:
        imported_runtime = Runtime(
            home=tmp_path / "imported-runtime", caches=[cache], prebuilt="require"
        )
        imported = imported_runtime.ensure(lock, name="imported")
        assert imported.id == environment.id
        assert exported.lock_id == lock.lock_id
        assert not imported_runtime.store.source_path(lock.packages[0].source_id).exists()
        imported_check = imported.check(source)
        assert imported_check.ok
        assert imported_check.lock_id == lock.lock_id
    assert (runtime.store.executions / f"{first.execution_id}.json").is_file()
    assert (runtime.store.executions / f"{repeated.execution_id}.json").is_file()
    version = environment.execute(["lean", "--version"])
    assert version.ok
    assert "Lean (version 4.32.0" in version.stdout
    bridge_script = "import sys\nfor line in sys.stdin:\n print(line.strip().upper(), flush=True)"
    with environment.spawn_interactive(
        ["lake", "env", sys.executable, "-u", "-c", bridge_script]
    ) as session:
        session.stdin.write("stateful\n")
        session.stdin.flush()
        assert session.stdout.readline() == "STATEFUL\n"
    interactive = session.close()
    assert interactive.ok
    assert interactive.environment_id == environment.id
    files = {
        "Support/Defs.lean": "import Sample\ndef answer : Nat := sampleValue + 1\n",
        "Main.lean": "import Support.Defs\nexample : answer = 42 := by rfl\n",
    }
    multi = environment.check_files(files)
    assert multi.ok
    asynchronous = asyncio.run(environment.check_files_async(files))
    assert asynchronous.ok
    assert runtime.open("demo").id == environment.id
    capture_path = tmp_path / "execution.capture.json"
    environment.capture_files(files, expected_ok=True).write(capture_path)

    shutil.rmtree(package)
    replayed = runtime.replay_capture(capture_path)
    assert replayed.ok
    assert replayed.environment_id == environment.id
    source_path = tmp_path / "Main.lean"
    source_path.write_text(source)
    child = subprocess.run(
        [
            sys.executable,
            "-m",
            "lean_runtime",
            "--home",
            str(runtime_home),
            "check",
            environment.id,
            str(source_path),
            "--json",
        ],
        env=child_environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    second = json.loads(child.stdout)
    assert second["ok"] is True
    assert second["provenance"]["environment_id"] == environment.id
    assert second["provenance"]["lock_id"] == lock.lock_id
