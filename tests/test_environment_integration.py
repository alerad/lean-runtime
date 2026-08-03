from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lean_runtime import EnvironmentSpec, GitPackage, Runtime


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
    events = []
    runtime = Runtime(home=resolver_home, on_event=events.append)
    spec = EnvironmentSpec(
        "4.32.0",
        (GitPackage.tag("sample", package.as_uri(), "v1.0.0", root_module="Sample"),),
    )
    lock = runtime.resolve(spec)
    assert lock.packages[0].requested_revision == "v1.0.0"
    assert lock.packages[0].revision == revision
    assert revision in lock.root_lakefile
    assert "v1.0.0" not in lock.root_lakefile
    assert {event.kind for event in events} >= {
        "resolution.started",
        "tag.resolved",
        "source.locked",
        "resolution.completed",
    }
    lock_path = tmp_path / "environment.lock.json"
    lock.write(lock_path)
    child_environment = os.environ.copy()
    child_environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    ensure_code = (
        "import sys; from lean_runtime import EnvironmentLock,Runtime; "
        "runtime=Runtime(home=sys.argv[1]); "
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
    built = [builder.communicate(timeout=120) for builder in builders]
    assert all(builder.returncode == 0 for builder in builders), built
    identities = {stdout.strip() for stdout, _ in built}
    assert len(identities) == 1
    runtime = Runtime(home=runtime_home)
    environment = runtime.open("demo")
    assert identities == {environment.id}
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
    assert (runtime.store.executions / f"{first.execution_id}.json").is_file()
    assert (runtime.store.executions / f"{repeated.execution_id}.json").is_file()
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
