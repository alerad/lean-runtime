from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from pathlib import Path

import pytest
import zstandard

from lean_runtime.errors import ToolchainError
from lean_runtime.oci import OCIRepository
from lean_runtime.toolchain_oci import (
    OCIToolchainPublisher,
    _extract_layer,
    _write_layer,
    toolchain_reference,
)


def test_toolchain_reference_is_normalized_and_content_addressed() -> None:
    assert toolchain_reference("4.33.0") == toolchain_reference("leanprover/lean4:v4.33.0")
    assert toolchain_reference("4.32.2") != toolchain_reference("4.33.0")


def test_toolchain_reference_is_platform_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    first = toolchain_reference("v4.33.0")
    monkeypatch.setattr(
        "lean_runtime.toolchain_oci.platform_compatibility",
        lambda: {"schema": 99, "system": "elsewhere", "machine": "other", "abi": "other"},
    )
    assert toolchain_reference("v4.33.0") == first


def test_toolchain_zstd_layer_is_deterministic_and_preserves_executables(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    binary = root / "bin" / "lean"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"lean")
    binary.chmod(0o755)
    (root / "slim-manifest.json").write_text("{}")
    first = tmp_path / "first.zst"
    second = tmp_path / "second.zst"
    _write_layer(root, first)
    _write_layer(root, second)
    assert (
        hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()
    )
    destination = tmp_path / "destination"
    destination.mkdir()
    _extract_layer(first, destination)
    assert (destination / "bin" / "lean").read_bytes() == b"lean"
    assert (destination / "bin" / "lean").stat().st_mode & 0o111


def test_toolchain_extractor_rejects_non_zstd_input(tmp_path: Path) -> None:
    layer = tmp_path / "bad.zst"
    layer.write_bytes(b"not zstd")
    destination = tmp_path / "destination"
    destination.mkdir()
    with pytest.raises(ToolchainError, match="could not extract"):
        _extract_layer(layer, destination)


@pytest.mark.skipif(os.name == "nt", reason="Windows runners may not permit symlinks")
def test_toolchain_layer_preserves_safe_internal_symlink(tmp_path: Path) -> None:
    root = tmp_path / "root"
    library = root / "lib" / "libLLVM.so.19"
    library.parent.mkdir(parents=True)
    library.write_bytes(b"llvm")
    (root / "lib" / "libLLVM-19.so").symlink_to("libLLVM.so.19")
    layer = tmp_path / "layer.zst"

    _write_layer(root, layer)
    destination = tmp_path / "destination"
    destination.mkdir()
    _extract_layer(layer, destination)

    link = destination / "lib" / "libLLVM-19.so"
    assert link.is_symlink()
    assert os.readlink(link) == "libLLVM.so.19"
    assert link.read_bytes() == b"llvm"


@pytest.mark.skipif(os.name == "nt", reason="Windows runners may not permit symlinks")
def test_toolchain_layer_rejects_escaping_symlink(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "lib").mkdir(parents=True)
    (root / "lib" / "escape").symlink_to("../../outside")

    with pytest.raises(ToolchainError, match="unsafe symlink"):
        _write_layer(root, tmp_path / "layer.zst")


def _raw_toolchain_layer(tmp_path: Path, members: list[tuple[str, str, bytes | str]]) -> Path:
    layer = tmp_path / "raw-toolchain.tar.zst"
    with (
        layer.open("wb") as raw,
        zstandard.ZstdCompressor().stream_writer(raw, closefd=False) as compressed,
        tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive,
    ):
        for kind, name, value in members:
            info = tarfile.TarInfo(name)
            if kind == "file":
                assert isinstance(value, bytes)
                info.size = len(value)
                archive.addfile(info, io.BytesIO(value))
            elif kind == "symlink":
                assert isinstance(value, str)
                info.type = tarfile.SYMTYPE
                info.linkname = value
                archive.addfile(info)
            else:
                assert kind == "fifo"
                info.type = tarfile.FIFOTYPE
                archive.addfile(info)
    return layer


@pytest.mark.parametrize("name", ["/absolute", "../escape", "dir/../../escape", "dir\\escape"])
def test_toolchain_extractor_rejects_unsafe_paths(tmp_path: Path, name: str) -> None:
    layer = _raw_toolchain_layer(tmp_path, [("file", name, b"payload")])
    with pytest.raises(ToolchainError, match="unsafe check toolchain member"):
        _extract_layer(layer, tmp_path / "output")


def test_toolchain_extractor_rejects_duplicate_member(tmp_path: Path) -> None:
    layer = _raw_toolchain_layer(
        tmp_path,
        [("file", "bin/lean", b"first"), ("file", "bin/lean", b"replacement")],
    )
    with pytest.raises(ToolchainError, match="duplicate check toolchain member"):
        _extract_layer(layer, tmp_path / "output")


def test_toolchain_extractor_rejects_special_member(tmp_path: Path) -> None:
    layer = _raw_toolchain_layer(tmp_path, [("fifo", "pipe", b"")])
    with pytest.raises(ToolchainError, match="only files, directories, and safe symlinks"):
        _extract_layer(layer, tmp_path / "output")


def test_toolchain_extractor_rejects_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layer = _raw_toolchain_layer(tmp_path, [("file", "large", b"ab")])
    monkeypatch.setattr("lean_runtime.toolchain_oci.MAX_TOOLCHAIN_BYTES", 1)
    with pytest.raises(ToolchainError, match="exceeds extraction limits"):
        _extract_layer(layer, tmp_path / "output")


@pytest.mark.skipif(os.name == "nt", reason="Windows runners may not permit symlink creation")
def test_toolchain_extractor_rejects_symlink_destination(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = tmp_path / "destination"
    destination.symlink_to(outside, target_is_directory=True)
    layer = _raw_toolchain_layer(tmp_path, [("file", "bin/lean", b"payload")])
    with pytest.raises(ToolchainError, match="destination must not be a symlink"):
        _extract_layer(layer, destination)


def test_toolchain_index_finalization_is_ordered_and_rejects_duplicate_platforms() -> None:
    publisher = OCIToolchainPublisher(
        OCIRepository.parse("oci://registry.example/owner/toolchains"),
        None,  # type: ignore[arg-type]
    )
    published: list[tuple[str, bytes]] = []

    class IndexClient:
        def manifest_exists(self, _digest: str) -> bool:
            return True

        def publish_manifest(self, reference: str, data: bytes, _media_type: str) -> str:
            published.append((reference, data))
            return "sha256:" + hashlib.sha256(data).hexdigest()

    publisher.client = IndexClient()  # type: ignore[assignment]

    def descriptor(system: str, architecture: str, abi: str, digest: str) -> dict[str, object]:
        return {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": "sha256:" + digest * 64,
            "size": 42,
            "platform": {"os": system, "architecture": architecture},
            "annotations": {"org.lean-runtime.platform.abi": abi},
        }

    linux = descriptor("linux", "amd64", "glibc-2.35", "a")
    macos = descriptor("darwin", "arm64", "darwin-arm64", "b")
    publication_id = publisher.publish_index("v4.33.0", [linux, macos])

    assert publication_id.startswith("sha256:")
    assert published[0][0] == toolchain_reference("v4.33.0")
    index = json.loads(published[0][1])
    assert [item["platform"]["os"] for item in index["manifests"]] == ["darwin", "linux"]

    duplicate = descriptor("linux", "amd64", "glibc-2.35", "c")
    with pytest.raises(ValueError, match="duplicate toolchain platform"):
        publisher.publish_index("v4.33.0", [linux, duplicate])
