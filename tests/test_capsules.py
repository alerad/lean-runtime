from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from lean_runtime import EnvironmentError, ExecutionPolicy, Runtime


def _revision() -> str:
    return "a" * 40


def _payload(root: Path) -> Path:
    payload = root / "payload"
    payload.mkdir()
    executable = payload / "echo-bridge"
    executable.write_text("#!/bin/sh\nwhile IFS= read -r line; do printf '%s\\n' \"$line\"; done\n")
    executable.chmod(0o755)
    return payload


def test_capsule_identity_payload_validation_and_interactive_execution(tmp_path: Path) -> None:
    runtime = Runtime(home=tmp_path / "runtime")
    capsule = runtime.create_capsule(
        _payload(tmp_path),
        command=("echo-bridge",),
        source_revision=_revision(),
        toolchain="leanprover/lean4:v4.32.2",
        capability_digest="sha256:" + hashlib.sha256(b"capabilities").hexdigest(),
    )

    assert capsule.id.startswith("capsule_")
    with capsule.spawn_interactive(policy=ExecutionPolicy(timeout_seconds=10)) as session:
        session.stdin.write('{"ping":true}\n')
        session.stdin.flush()
        assert session.stdout.readline() == '{"ping":true}\n'
    assert session.close().provenance is not None
    assert session.close().provenance.capsule_id == capsule.id

    reopened = runtime.open_capsule(capsule.id)
    assert reopened.manifest.source_revision == _revision()


def test_capsule_rejects_payload_tampering(tmp_path: Path) -> None:
    runtime = Runtime(home=tmp_path / "runtime")
    capsule = runtime.create_capsule(
        _payload(tmp_path), command=("echo-bridge",), source_revision=_revision()
    )
    executable = capsule.root / "payload" / "echo-bridge"
    executable.write_text("changed")
    with pytest.raises(EnvironmentError, match="payload digest mismatch"):
        runtime.open_capsule(capsule.id)


def test_capsule_oci_round_trip_is_deterministic(tmp_path: Path) -> None:
    producer = Runtime(home=tmp_path / "producer")
    capsule = producer.create_capsule(
        _payload(tmp_path), command=("echo-bridge",), source_revision=_revision()
    )
    first = tmp_path / "first.oci.tar.gz"
    second = tmp_path / "second.oci.tar.gz"
    first_info = producer.export_capsule(capsule.id, first)
    second_info = producer.export_capsule(capsule.id, second)

    assert first.read_bytes() == second.read_bytes()
    assert first_info.manifest_digest == second_info.manifest_digest

    consumer = Runtime(home=tmp_path / "consumer")
    imported = consumer.import_capsule(first)
    assert imported.id == capsule.id
    assert imported.manifest_digest == first_info.manifest_digest


def test_capsule_rejects_command_outside_payload(tmp_path: Path) -> None:
    runtime = Runtime(home=tmp_path / "runtime")
    with pytest.raises(EnvironmentError, match="absent"):
        runtime.create_capsule(
            _payload(tmp_path), command=("missing",), source_revision=_revision()
        )


@pytest.mark.skipif(os.name == "nt", reason="test fixture uses a POSIX script")
def test_capsule_cannot_switch_executable(tmp_path: Path) -> None:
    runtime = Runtime(home=tmp_path / "runtime")
    capsule = runtime.create_capsule(
        _payload(tmp_path), command=("echo-bridge",), source_revision=_revision()
    )
    with pytest.raises(EnvironmentError, match="declared executable"):
        capsule.spawn_interactive(("other",))
