from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from lean_runtime import EnvironmentError, ExecutionPolicy, Runtime
from lean_runtime.oci import OCIRepository
from lean_runtime.programs import LEGACY_PROGRAM_SCHEMA, ProgramDescription, ProgramLibrary


def _revision() -> str:
    return "a" * 40


def _payload(root: Path) -> Path:
    payload = root / "payload"
    payload.mkdir()
    executable = payload / "echo-bridge"
    executable.write_text("#!/bin/sh\nexec /bin/cat\n")
    executable.chmod(0o755)
    return payload


@pytest.mark.skipif(os.name == "nt", reason="the fixture program is a POSIX shell script")
def test_program_identity_payload_validation_and_interactive_execution(tmp_path: Path) -> None:
    runtime = Runtime(home=tmp_path / "runtime")
    program = runtime.create_program(
        _payload(tmp_path),
        command=("echo-bridge",),
        source_revision=_revision(),
        toolchain="leanprover/lean4:v4.32.2",
        capability_id="sha256:" + hashlib.sha256(b"capabilities").hexdigest(),
        provenance={
            "lean.toolchain": "leanprover/lean4:v4.32.2",
            "leancert.core.revision": "b" * 40,
        },
    )

    assert program.id.startswith("program_")
    with program.spawn_interactive(policy=ExecutionPolicy(timeout_seconds=10)) as session:
        assert session.request_json({"ping": True}) == {"ping": True}
    assert session.close().provenance is not None
    assert session.close().provenance.program_id == program.id

    reopened = runtime.program(program.id)
    assert reopened.description.source_revision == _revision()
    assert reopened.description.provenance["leancert.core.revision"] == "b" * 40


def test_program_provenance_is_content_addressed(tmp_path: Path) -> None:
    runtime = Runtime(home=tmp_path / "runtime")
    payload = _payload(tmp_path)
    first = runtime.create_program(
        payload,
        command=("echo-bridge",),
        source_revision=_revision(),
        provenance={"leancert.core.revision": "b" * 40},
    )
    second = runtime.create_program(
        payload,
        command=("echo-bridge",),
        source_revision=_revision(),
        provenance={"leancert.core.revision": "c" * 40},
    )

    assert first.id != second.id


def test_legacy_program_identity_remains_stable() -> None:
    description = ProgramDescription(
        command=("echo-bridge",),
        files={"echo-bridge": {"kind": "file", "sha256": "sha256:" + "0" * 64}},
        computer_compatibility={"schema": "computer/1"},
        source_revision=_revision(),
        schema=LEGACY_PROGRAM_SCHEMA,
    )

    restored = ProgramDescription.from_dict(description.to_dict())
    assert restored.program_id == description.program_id
    assert "provenance" not in restored.to_dict()


def test_program_rejects_payload_tampering(tmp_path: Path) -> None:
    runtime = Runtime(home=tmp_path / "runtime")
    program = runtime.create_program(
        _payload(tmp_path), command=("echo-bridge",), source_revision=_revision()
    )
    executable = program.root / "payload" / "echo-bridge"
    executable.write_text("changed")
    with pytest.raises(EnvironmentError, match="payload digest mismatch"):
        runtime.program(program.id)


def test_program_oci_round_trip_is_deterministic(tmp_path: Path) -> None:
    producer = Runtime(home=tmp_path / "producer")
    program = producer.create_program(
        _payload(tmp_path), command=("echo-bridge",), source_revision=_revision()
    )
    first = tmp_path / "first.oci.tar.gz"
    second = tmp_path / "second.oci.tar.gz"
    first_info = producer.save_program_copy(program.id, first)
    second_info = producer.save_program_copy(program.id, second)

    assert first.read_bytes() == second.read_bytes()
    assert first_info.copy_id == second_info.copy_id

    consumer = Runtime(home=tmp_path / "consumer")
    imported = consumer.open_program_copy(first)
    assert imported.id == program.id
    assert imported.copy_id == first_info.copy_id


def test_program_index_finalization_is_ordered_and_rejects_duplicate_platforms() -> None:
    library = ProgramLibrary(
        OCIRepository.parse("oci://registry.example/owner/programs"),
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )
    published: list[tuple[str, bytes]] = []

    class IndexClient:
        def manifest_exists(self, _digest: str) -> bool:
            return True

        def publish_manifest(self, reference: str, data: bytes, _media_type: str) -> str:
            published.append((reference, data))
            return "sha256:" + hashlib.sha256(data).hexdigest()

    library.client = IndexClient()  # type: ignore[assignment]

    def descriptor(system: str, architecture: str, abi: str, digest: str) -> dict[str, object]:
        return {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": "sha256:" + digest * 64,
            "size": 42,
            "platform": {"os": system, "architecture": architecture},
            "annotations": {
                "org.lean-runtime.artifact.kind": "execution-program",
                "org.lean-runtime.platform.abi": abi,
            },
        }

    linux = descriptor("linux", "amd64", "glibc-2.35", "a")
    macos = descriptor("darwin", "arm64", "darwin-arm64", "b")
    revision = "c" * 40
    publication_id = library.publish_index(revision, [linux, macos], tags=("dogfood",))

    assert publication_id.startswith("sha256:")
    assert [reference for reference, _data in published] == [revision, "dogfood"]
    assert published[0][1] == published[1][1]
    index = json.loads(published[0][1])
    assert [item["platform"]["os"] for item in index["manifests"]] == ["darwin", "linux"]

    duplicate = descriptor("linux", "amd64", "glibc-2.35", "d")
    with pytest.raises(ValueError, match="duplicate program platform"):
        library.publish_index(revision, [linux, duplicate])


def test_program_rejects_command_outside_payload(tmp_path: Path) -> None:
    runtime = Runtime(home=tmp_path / "runtime")
    with pytest.raises(EnvironmentError, match="absent"):
        runtime.create_program(
            _payload(tmp_path), command=("missing",), source_revision=_revision()
        )


@pytest.mark.skipif(os.name == "nt", reason="test fixture uses a POSIX script")
def test_program_cannot_switch_executable(tmp_path: Path) -> None:
    runtime = Runtime(home=tmp_path / "runtime")
    program = runtime.create_program(
        _payload(tmp_path), command=("echo-bridge",), source_revision=_revision()
    )
    with pytest.raises(EnvironmentError, match="declared executable"):
        program.spawn_interactive(("other",))
