from __future__ import annotations

from pathlib import Path

import pytest

from lean_runtime.capsules import build_manifest
from lean_runtime.errors import EnvironmentError
from lean_runtime.packs import SparsePack, build_sparse_packs, project_artifacts, unpack_frame


def _manifest(tmp_path: Path):
    workspace = tmp_path / "workspace"
    build = workspace / ".lake" / "packages" / "sample" / ".lake" / "build" / "lib" / "lean"
    for module, data in (("A", b"a" * 700), ("B", b"b" * 700), ("C", b"c" * 700)):
        path = build / f"{module}.olean"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = build_manifest(
        workspace=workspace,
        environment_id="env_test",
        lock_id="lock_test",
        toolchain="v4.33.0",
        build_roots={"sample": build},
        imports={"A": (), "B": ("A",), "C": ("B",)},
    )
    return workspace, manifest


def test_sparse_packs_round_trip_selected_frames_through_cas(tmp_path: Path) -> None:
    workspace, manifest = _manifest(tmp_path)
    (pack,) = build_sparse_packs(workspace, manifest, tmp_path / "packs", target_frame_bytes=1024)
    assert SparsePack.from_dict(pack.to_dict()).digest == pack.digest
    selected_modules = {module.name for module in manifest.closure(["B"])}
    frames = pack.frames_for_modules(selected_modules)
    assert len(frames) == 2
    artifacts = {
        artifact.path: artifact for module in manifest.modules for artifact in module.artifacts
    }
    cas = tmp_path / "cas"
    with pack.path.open("rb") as handle:  # type: ignore[union-attr]
        for frame in frames:
            handle.seek(frame.offset)
            unpack_frame(handle.read(frame.size), frame, artifacts, cas)
    paths = [path for frame in frames for path in frame.artifacts]
    projected = tmp_path / "projected"
    assert project_artifacts(paths, artifacts, cas, projected) == 1400
    assert next(projected.rglob("A.olean")).read_bytes() == b"a" * 700
    assert next(projected.rglob("B.olean")).read_bytes() == b"b" * 700
    assert not list(projected.rglob("C.olean"))


def test_sparse_frame_corruption_fails_before_cas_publication(tmp_path: Path) -> None:
    workspace, manifest = _manifest(tmp_path)
    (pack,) = build_sparse_packs(workspace, manifest, tmp_path / "packs", target_frame_bytes=1024)
    frame = pack.frames[0]
    data = bytearray(pack.path.read_bytes()[frame.offset : frame.offset + frame.size])  # type: ignore[union-attr]
    data[-1] ^= 1
    with pytest.raises(EnvironmentError, match="frame digest mismatch"):
        unpack_frame(
            bytes(data),
            frame,
            {
                artifact.path: artifact
                for module in manifest.modules
                for artifact in module.artifacts
            },
            tmp_path / "cas",
        )
    assert not (tmp_path / "cas").exists()


def test_sparse_packs_bound_individual_oci_blobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, manifest = _manifest(tmp_path)
    monkeypatch.setattr("lean_runtime.packs.MAX_PACK_RAW_BYTES", 1000)

    packs = build_sparse_packs(workspace, manifest, tmp_path / "packs", target_frame_bytes=1024)

    assert len(packs) == 3
    assert all(len(pack.frames) == 1 for pack in packs)
