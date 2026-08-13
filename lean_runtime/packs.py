"""Seekable, independently verified zstd packs for capsule artifacts."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import struct
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import zstandard

from .capsules import ArtifactCapability, CapsuleArtifact, CapsuleManifest
from .errors import EnvironmentError
from .locking import FileLock

PACK_SCHEMA = "lean-runtime-sparse-pack/1"
PACK_MEDIA_TYPE = "application/vnd.lean-runtime.sparse-pack.v1+zstd"
# One-MiB frames keep a narrow import from paying for many lexically adjacent,
# unrelated modules. Larger frames compress slightly better, but the measured
# range-amplification dominated that saving by more than 4x on Mathlib.
DEFAULT_FRAME_BYTES = 1024**2
MAX_FRAME_BYTES = 128 * 1024**2
MAX_PACK_RAW_BYTES = 512 * 1024**2
_MAGIC = b"LRCAP1\0"
_HEADER = struct.Struct(">IQ")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _digest_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


@dataclass(frozen=True, slots=True)
class PackFrame:
    offset: int
    size: int
    raw_size: int
    digest: str
    modules: tuple[str, ...]
    artifacts: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "offset": self.offset,
            "size": self.size,
            "raw_size": self.raw_size,
            "digest": self.digest,
            "modules": list(self.modules),
            "artifacts": list(self.artifacts),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PackFrame:
        offset = value.get("offset")
        size = value.get("size")
        raw_size = value.get("raw_size")
        digest = value.get("digest")
        modules = value.get("modules")
        artifacts = value.get("artifacts")
        if (
            not isinstance(offset, int)
            or offset < 0
            or not isinstance(size, int)
            or size < 1
            or not isinstance(raw_size, int)
            or raw_size < 1
            or raw_size > MAX_FRAME_BYTES
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or not isinstance(modules, list)
            or not all(isinstance(item, str) and item for item in modules)
            or not isinstance(artifacts, list)
            or not all(isinstance(item, str) and item for item in artifacts)
            or modules != sorted(set(modules))
            or len(artifacts) != len(set(artifacts))
        ):
            raise EnvironmentError("sparse pack contains an invalid frame")
        return cls(offset, size, raw_size, digest, tuple(modules), tuple(artifacts))


@dataclass(frozen=True, slots=True)
class SparsePack:
    package: str
    capability: ArtifactCapability
    digest: str
    size: int
    frames: tuple[PackFrame, ...]
    path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PACK_SCHEMA,
            "package": self.package,
            "capability": self.capability,
            "digest": self.digest,
            "size": self.size,
            "frames": [frame.to_dict() for frame in self.frames],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SparsePack:
        package = value.get("package")
        capability = value.get("capability")
        digest = value.get("digest")
        size = value.get("size")
        frames = value.get("frames")
        if (
            value.get("schema") != PACK_SCHEMA
            or not isinstance(package, str)
            or not package
            or capability not in {"check", "native", "editor", "development"}
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or not isinstance(size, int)
            or size < 0
            or not isinstance(frames, list)
            or not all(isinstance(item, dict) for item in frames)
        ):
            raise EnvironmentError("sparse pack index is invalid")
        parsed = tuple(PackFrame.from_dict(item) for item in frames)
        expected_offset = 0
        for frame in parsed:
            if frame.offset != expected_offset:
                raise EnvironmentError("sparse pack frames are not contiguous")
            expected_offset += frame.size
        if expected_offset != size:
            raise EnvironmentError("sparse pack size does not match its frames")
        return cls(package, capability, digest, size, parsed)

    def frames_for_modules(self, modules: Iterable[str]) -> tuple[PackFrame, ...]:
        selected = frozenset(modules)
        return tuple(frame for frame in self.frames if selected.intersection(frame.modules))


def _encode_frame(workspace: Path, artifacts: Iterable[CapsuleArtifact]) -> bytes:
    output = bytearray(_MAGIC)
    for artifact in artifacts:
        path = artifact.path.encode("utf-8")
        data = workspace.joinpath(*PurePosixPath(artifact.path).parts).read_bytes()
        if len(data) != artifact.size or _digest(data) != artifact.digest:
            raise EnvironmentError(f"capsule artifact changed while packing: {artifact.path}")
        output.extend(_HEADER.pack(len(path), len(data)))
        output.extend(path)
        output.extend(data)
    return bytes(output)


def _frame_groups(
    modules: list[tuple[str, tuple[CapsuleArtifact, ...]]], target_bytes: int
) -> Iterable[list[tuple[str, tuple[CapsuleArtifact, ...]]]]:
    group: list[tuple[str, tuple[CapsuleArtifact, ...]]] = []
    size = len(_MAGIC)
    for module in modules:
        module_size = sum(_HEADER.size + len(item.path.encode()) + item.size for item in module[1])
        if group and size + module_size > target_bytes:
            yield group
            group = []
            size = len(_MAGIC)
        group.append(module)
        size += module_size
    if group:
        yield group


def _pack_shards(
    modules: list[tuple[str, tuple[CapsuleArtifact, ...]]],
) -> Iterable[list[tuple[str, tuple[CapsuleArtifact, ...]]]]:
    """Bound OCI blobs while preserving deterministic package-level reuse."""
    shard: list[tuple[str, tuple[CapsuleArtifact, ...]]] = []
    size = 0
    for module in modules:
        module_size = sum(_HEADER.size + len(item.path.encode()) + item.size for item in module[1])
        if shard and size + module_size > MAX_PACK_RAW_BYTES:
            yield shard
            shard = []
            size = 0
        shard.append(module)
        size += module_size
    if shard:
        yield shard


def build_sparse_packs(
    workspace: Path,
    manifest: CapsuleManifest,
    output: Path,
    *,
    target_frame_bytes: int = DEFAULT_FRAME_BYTES,
    capabilities: frozenset[ArtifactCapability] | None = None,
) -> tuple[SparsePack, ...]:
    """Write deterministic package/capability packs and return their indexes."""
    if target_frame_bytes < 1024 or target_frame_bytes > MAX_FRAME_BYTES:
        raise ValueError("target frame size is outside supported bounds")
    grouped: dict[
        tuple[str, ArtifactCapability], list[tuple[str, tuple[CapsuleArtifact, ...]]]
    ] = {}
    for module in manifest.modules:
        by_capability: dict[ArtifactCapability, list[CapsuleArtifact]] = {}
        for artifact in module.artifacts:
            if artifact.capability == "metadata":
                continue
            if capabilities is not None and artifact.capability not in capabilities:
                continue
            by_capability.setdefault(artifact.capability, []).append(artifact)
        for capability, artifacts in by_capability.items():
            grouped.setdefault((module.package, capability), []).append(
                (module.name, tuple(sorted(artifacts, key=lambda item: item.path)))
            )

    output.mkdir(parents=True, exist_ok=True)
    compressor = zstandard.ZstdCompressor(level=10, write_checksum=True, threads=0)
    packs: list[SparsePack] = []
    for (package, capability), modules in sorted(grouped.items()):
        for shard_index, shard in enumerate(_pack_shards(sorted(modules))):
            temporary = output / (f".{package}-{capability}-{shard_index}-{os.getpid()}.partial")
            frames: list[PackFrame] = []
            offset = 0
            try:
                with temporary.open("wb") as handle:
                    for group in _frame_groups(shard, target_frame_bytes):
                        frame_artifacts = tuple(item for _name, items in group for item in items)
                        raw = _encode_frame(workspace, frame_artifacts)
                        if len(raw) > MAX_FRAME_BYTES:
                            raise EnvironmentError(
                                f"capsule frame exceeds {MAX_FRAME_BYTES} bytes uncompressed"
                            )
                        compressed = compressor.compress(raw)
                        handle.write(compressed)
                        frames.append(
                            PackFrame(
                                offset,
                                len(compressed),
                                len(raw),
                                _digest(compressed),
                                tuple(name for name, _items in group),
                                tuple(item.path for item in frame_artifacts),
                            )
                        )
                        offset += len(compressed)
                digest = _digest_path(temporary)
                destination = output / f"{digest.removeprefix('sha256:')}.lrpack"
                if destination.exists():
                    temporary.unlink()
                else:
                    temporary.replace(destination)
                packs.append(
                    SparsePack(
                        package,
                        capability,
                        digest,
                        destination.stat().st_size,
                        tuple(frames),
                        destination,
                    )
                )
            finally:
                temporary.unlink(missing_ok=True)
    return tuple(packs)


def unpack_frame(
    compressed: bytes,
    frame: PackFrame,
    artifacts: Mapping[str, CapsuleArtifact],
    cas_root: Path,
    *,
    lock_root: Path | None = None,
) -> tuple[str, ...]:
    """Verify one independently compressed frame and publish artifacts to CAS."""
    if len(compressed) != frame.size or _digest(compressed) != frame.digest:
        raise EnvironmentError("sparse pack frame digest mismatch")
    try:
        raw = zstandard.ZstdDecompressor().decompress(compressed, max_output_size=frame.raw_size)
    except zstandard.ZstdError as exc:
        raise EnvironmentError("sparse pack frame is not valid zstd data") from exc
    if len(raw) != frame.raw_size or not raw.startswith(_MAGIC):
        raise EnvironmentError("sparse pack frame payload is invalid")
    position = len(_MAGIC)
    observed: list[str] = []
    cas_root.mkdir(parents=True, exist_ok=True)
    while position < len(raw):
        if position + _HEADER.size > len(raw):
            raise EnvironmentError("sparse pack record header is truncated")
        path_size, data_size = _HEADER.unpack_from(raw, position)
        position += _HEADER.size
        end_path = position + path_size
        end_data = end_path + data_size
        if end_data > len(raw):
            raise EnvironmentError("sparse pack record is truncated")
        try:
            path = raw[position:end_path].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EnvironmentError("sparse pack record path is invalid") from exc
        position = end_path
        data = raw[position:end_data]
        position = end_data
        artifact = artifacts.get(path)
        if artifact is None or len(data) != artifact.size or _digest(data) != artifact.digest:
            raise EnvironmentError(f"sparse pack artifact digest mismatch: {path}")
        destination = cas_root / artifact.digest.removeprefix("sha256:")
        lock = (
            FileLock(lock_root / f"cas-{destination.name}.lock", timeout=1800)
            if lock_root is not None
            else nullcontext()
        )
        with lock:
            if not destination.exists():
                with tempfile.NamedTemporaryFile(dir=cas_root, delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(data)
                try:
                    if not destination.exists():
                        temporary.replace(destination)
                finally:
                    temporary.unlink(missing_ok=True)
        observed.append(path)
    if tuple(observed) != frame.artifacts:
        raise EnvironmentError("sparse pack frame artifact inventory mismatch")
    return tuple(observed)


def project_artifacts(
    paths: Iterable[str],
    artifacts: Mapping[str, CapsuleArtifact],
    cas_root: Path,
    root: Path,
    *,
    lock_root: Path | None = None,
) -> int:
    """Hardlink verified CAS artifacts into a disposable environment projection."""
    linked = 0
    for path in paths:
        artifact = artifacts[path]
        source = cas_root / artifact.digest.removeprefix("sha256:")
        lock = (
            FileLock(lock_root / f"cas-{source.name}.lock", timeout=1800)
            if lock_root is not None
            else nullcontext()
        )
        with lock:
            if (
                not source.is_file()
                or source.stat().st_size != artifact.size
                or _digest_path(source) != artifact.digest
            ):
                raise EnvironmentError(f"capsule CAS artifact is unavailable: {path}")
            os.utime(source, None)
            destination = root.joinpath(*PurePosixPath(path).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if (
                    not destination.is_file()
                    or destination.stat().st_size != artifact.size
                    or _digest_path(destination) != artifact.digest
                ):
                    raise EnvironmentError(
                        f"capsule projection contains a conflicting artifact: {path}"
                    )
                continue
            try:
                os.link(source, destination)
            except OSError:
                shutil.copy2(source, destination)
        linked += artifact.size
    return linked
