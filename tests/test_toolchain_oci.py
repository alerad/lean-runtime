from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from lean_runtime.errors import ToolchainError
from lean_runtime.toolchain_oci import _extract_layer, _write_layer, toolchain_reference


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
