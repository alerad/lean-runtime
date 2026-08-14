from pathlib import Path

import pytest

from lean_runtime import EnvironmentError, oci_protocol


def test_descriptor_contract_is_content_addressed(tmp_path: Path) -> None:
    payload = b"lean-runtime\n"
    path = tmp_path / "payload"
    path.write_bytes(payload)

    memory = oci_protocol.blob_descriptor(payload, "example/type", annotations={"x": "y"})
    stored = oci_protocol.blob_descriptor_path(path, "example/type", annotations={"x": "y"})

    assert memory == stored
    assert memory == {
        "mediaType": "example/type",
        "digest": "sha256:7677c1c4006debfab16212fec9bb09526c4596782053d3c319d889031a8cfbd9",
        "size": 13,
        "annotations": {"x": "y"},
    }


def test_descriptor_blob_rejects_tampering(tmp_path: Path) -> None:
    payload = b"expected"
    descriptor = oci_protocol.blob_descriptor(payload, "example/type")
    path = tmp_path / "blob"
    path.write_bytes(b"tampered")
    entries = {"blobs/sha256/" + descriptor["digest"].removeprefix("sha256:"): path}

    with pytest.raises(EnvironmentError, match="digest mismatch"):
        oci_protocol.descriptor_blob_path(entries, descriptor, "payload")


def test_json_object_preserves_caller_subject() -> None:
    with pytest.raises(EnvironmentError, match="OCI manifest is not valid JSON"):
        oci_protocol.json_object(b"not-json", "manifest", subject="OCI")


def test_platform_match_uses_host_abi_and_optional_artifact_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        oci_protocol,
        "platform_compatibility",
        lambda: {"system": "linux", "machine": "x86_64", "abi": "glibc-2.39"},
    )
    descriptor = {
        "platform": {"os": "linux", "architecture": "amd64"},
        "annotations": {
            "org.lean-runtime.platform.abi": "glibc-2.39",
            "org.lean-runtime.artifact.kind": "execution-program",
        },
    }

    assert oci_protocol.platform_matches(descriptor)
    assert oci_protocol.platform_matches(descriptor, artifact_kind="execution-program")
    assert not oci_protocol.platform_matches(descriptor, artifact_kind="check-toolchain")
