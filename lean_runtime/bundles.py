"""Verified portable copies of published environments."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .backends import Backend
from .errors import EnvironmentError
from .events import EventEmitter
from .lake import ROOT_MODULE
from .lockfiles import EnvironmentLock, LockedPackage
from .locking import FileLock
from .policies import ExecutionPolicy
from .serialization import canonical_json_bytes, write_json_atomic
from .store import (
    EnvironmentStore,
    environment_identity,
    platform_compatibility,
    platform_record,
)
from .toolchains import ToolchainManager

BUNDLE_SCHEMA = "lean-runtime-oci-bundle/1"
CONFIG_MEDIA_TYPE = "application/vnd.lean-runtime.environment.config.v1+json"
LAYER_MEDIA_TYPE = "application/vnd.lean-runtime.environment.layer.v1.tar+gzip"
MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
MAX_BUNDLE_BYTES = 20 * 1024**3
MAX_FILES = 2_000_000


@dataclass(frozen=True, slots=True)
class PortableCopyInfo:
    environment_id: str
    exact_environment_id: str
    copy_id: str
    path: str

    def to_dict(self) -> dict[str, str]:
        return {
            "environment_id": self.environment_id,
            "exact_environment_id": self.exact_environment_id,
            "copy_id": self.copy_id,
            "path": self.path,
        }


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _digest_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _blob_descriptor(data: bytes, media_type: str, **extra: Any) -> dict[str, Any]:
    return {"mediaType": media_type, "digest": _digest(data), "size": len(data), **extra}


def _blob_descriptor_path(path: Path, media_type: str, **extra: Any) -> dict[str, Any]:
    return {
        "mediaType": media_type,
        "digest": _digest_path(path),
        "size": path.stat().st_size,
        **extra,
    }


def _normalized_info(name: str, *, mode: int, kind: bytes = tarfile.REGTYPE) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mode = mode
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _tree_entries(root: Path, excluded: Path | None = None) -> Iterable[tuple[Path, str]]:
    for path in sorted(root.rglob("*"), key=lambda value: value.relative_to(root).as_posix()):
        if excluded is not None and (path == excluded or excluded in path.parents):
            continue
        yield path, path.relative_to(root).as_posix()


def _write_tar_gzip(root: Path, output: Path, *, excluded: Path | None = None) -> None:
    with (
        output.open("wb") as raw_output,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw_output, mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive,
    ):
        for path, name in _tree_entries(root, excluded):
            stat = path.lstat()
            mode = stat.st_mode & 0o777
            if path.is_symlink():
                info = _normalized_info(name, mode=mode or 0o777, kind=tarfile.SYMTYPE)
                info.linkname = os.readlink(path)
                archive.addfile(info)
            elif path.is_dir():
                archive.addfile(
                    _normalized_info(name + "/", mode=mode or 0o755, kind=tarfile.DIRTYPE)
                )
            elif path.is_file():
                info = _normalized_info(name, mode=mode or 0o644)
                info.size = stat.st_size
                with path.open("rb") as handle:
                    archive.addfile(info, handle)
            else:
                raise EnvironmentError(f"bundle contains unsupported filesystem entry: {path}")


def _tar_gzip(root: Path, *, excluded: Path | None = None) -> bytes:
    """Compatibility helper for small tests; production export streams to disk."""
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "layer.tar.gz"
        _write_tar_gzip(root, path, excluded=excluded)
        return path.read_bytes()


def _oci_archive(entries: dict[str, bytes]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, data in sorted(entries.items()):
            info = _normalized_info(name, mode=0o644)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    output = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
        compressed.write(raw.getvalue())
    return output.getvalue()


def _write_oci_archive(entries: dict[str, Path], output: Path) -> None:
    with (
        output.open("wb") as raw_output,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw_output, mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive,
    ):
        for name, path in sorted(entries.items()):
            info = _normalized_info(name, mode=0o644)
            info.size = path.stat().st_size
            with path.open("rb") as handle:
                archive.addfile(info, handle)


def _json_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnvironmentError(f"bundle {label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise EnvironmentError(f"bundle {label} must be a JSON object")
    return value


def _descriptor_blob(entries: dict[str, bytes], descriptor: dict[str, Any], label: str) -> bytes:
    digest = descriptor.get("digest")
    size = descriptor.get("size")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise EnvironmentError(f"bundle {label} has an invalid digest")
    data = entries.get("blobs/sha256/" + digest.removeprefix("sha256:"))
    if data is None or len(data) != size or _digest(data) != digest:
        raise EnvironmentError(f"bundle {label} digest mismatch")
    return data


def _descriptor_blob_path(entries: dict[str, Path], descriptor: dict[str, Any], label: str) -> Path:
    digest = descriptor.get("digest")
    size = descriptor.get("size")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise EnvironmentError(f"bundle {label} has an invalid digest")
    path = entries.get("blobs/sha256/" + digest.removeprefix("sha256:"))
    if path is None or path.stat().st_size != size or _digest_path(path) != digest:
        raise EnvironmentError(f"bundle {label} digest mismatch")
    return path


def _require_media_type(descriptor: dict[str, Any], expected: str, label: str) -> None:
    if descriptor.get("mediaType") != expected:
        raise EnvironmentError(f"bundle {label} has an unsupported media type")


def _safe_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\x00" in name:
        raise EnvironmentError(f"unsafe bundle member path: {name!r}")
    return path


def _packages_directory(lock: EnvironmentLock) -> PurePosixPath:
    value = lock.manifest.get("packagesDir", ".lake/packages")
    if not isinstance(value, str):
        raise EnvironmentError("lock packagesDir must be a relative string")
    path = _safe_name(value)
    if path == PurePosixPath("."):
        raise EnvironmentError("lock packagesDir must not be the workspace root")
    return path


def _extract_layer(data: bytes | Path, destination: Path) -> None:
    total = 0
    count = 0
    destination.mkdir(parents=True, exist_ok=True)
    try:
        if isinstance(data, Path):
            archive = tarfile.open(data, mode="r:gz")  # noqa: SIM115
        else:
            archive = tarfile.open(  # noqa: SIM115
                fileobj=io.BytesIO(data), mode="r:gz"
            )
    except (tarfile.TarError, OSError) as exc:
        raise EnvironmentError("bundle layer is not a valid gzip tar archive") from exc
    with archive:
        for member in archive:
            count += 1
            total += member.size
            if count > MAX_FILES or total > MAX_BUNDLE_BYTES:
                raise EnvironmentError("bundle layer exceeds extraction limits")
            relative = _safe_name(member.name)
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(member.mode & 0o777)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise EnvironmentError(f"bundle member has no content: {member.name}")
                with target.open("wb") as handle:
                    shutil.copyfileobj(source, handle)
                target.chmod(member.mode & 0o777)
            elif member.issym():
                link = PurePosixPath(member.linkname)
                resolved = relative.parent.joinpath(link)
                if link.is_absolute() or ".." in resolved.parts:
                    raise EnvironmentError(f"unsafe bundle symlink: {member.name!r}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(member.linkname)
            else:
                raise EnvironmentError(f"unsupported bundle member: {member.name!r}")


def _verify_package(root: Path, package: LockedPackage) -> None:
    marker = root / ".lean-runtime-source.json"
    if not marker.is_file():
        raise EnvironmentError(f"bundled package source marker is missing: {package.name}")
    marker_value = _json_object(marker.read_bytes(), f"package {package.name} source marker")
    expected_marker = {
        "source_id": package.source_id,
        "url": package.url,
        "revision": package.revision,
        "tree_hash": package.tree_hash,
    }
    if any(marker_value.get(key) != value for key, value in expected_marker.items()):
        raise EnvironmentError(f"bundled package source marker mismatch: {package.name}")
    content_hash = marker_value.get("content_hash")
    if (
        not isinstance(content_hash, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", content_hash) is None
    ):
        raise EnvironmentError(f"bundled package source marker mismatch: {package.name}")
    observed: list[str] = []
    for revision in ("HEAD", "HEAD^{tree}"):
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", revision],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise EnvironmentError(f"bundled package Git metadata is invalid: {package.name}")
        observed.append(result.stdout.strip().lower())
    if observed != [package.revision.lower(), package.tree_hash.lower()]:
        raise EnvironmentError(f"bundled package source mismatch: {package.name}")
    status = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=no",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if status.returncode:
        raise EnvironmentError(f"bundled package Git metadata is invalid: {package.name}")
    changes = [
        line
        for line in status.stdout.splitlines()
        if line and line != "?? .lean-runtime-source.json" and not line.startswith("?? .lake/")
    ]
    if changes:
        raise EnvironmentError(f"bundled package checked-out content mismatch: {package.name}")


def _verify_workspace_lock(workspace: Path, lock: EnvironmentLock) -> None:
    expected_text = {
        "lean-toolchain": lock.toolchain + "\n",
        "lakefile.toml": lock.root_lakefile,
        f"{ROOT_MODULE}.lean": lock.root_module,
    }
    for relative, expected in expected_text.items():
        path = workspace / relative
        if not path.is_file() or path.read_text(encoding="utf-8") != expected:
            raise EnvironmentError(f"bundled root workspace does not match lock: {relative}")
    manifest_path = workspace / "lake-manifest.json"
    if (
        not manifest_path.is_file()
        or _json_object(manifest_path.read_bytes(), "root Lake manifest") != lock.manifest
    ):
        raise EnvironmentError("bundled root Lake manifest does not match lock")
    if not (workspace / ".lake" / "build").is_dir():
        raise EnvironmentError("bundled root build artifacts are missing")


class EnvironmentBundles:
    def __init__(
        self,
        store: EnvironmentStore,
        toolchains: ToolchainManager,
        backend: Backend,
        events: EventEmitter,
    ) -> None:
        self.store = store
        self.toolchains = toolchains
        self.backend = backend
        self.events = events

    def export(self, environment_id: str, output: Path) -> PortableCopyInfo:
        root = self.store.environment_path(environment_id)
        metadata = _json_object((root / "metadata.json").read_bytes(), "metadata")
        lock = self.store.load_lock(str(metadata["lock_id"]))
        expected = environment_identity(lock, str(metadata["build_profile"]))
        if expected != environment_id:
            raise EnvironmentError(f"environment identity mismatch: {environment_id}")
        workspace = root / "workspace"
        packages_dir = workspace.joinpath(*_packages_directory(lock).parts)
        _verify_workspace_lock(workspace, lock)
        package_roots: list[tuple[LockedPackage, Path]] = []
        for package in sorted(lock.packages, key=lambda item: item.name):
            package_root = packages_dir / package.name
            if not package_root.is_dir():
                raise EnvironmentError(f"environment package is missing: {package.name}")
            _verify_package(package_root, package)
            package_roots.append((package, package_root))
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            with tempfile.TemporaryDirectory(prefix="lean-runtime-export-") as temporary_dir:
                staging = Path(temporary_dir)
                entries: dict[str, Path] = {}
                layers: list[dict[str, Any]] = []

                root_layer = staging / "root.tar.gz"
                _write_tar_gzip(workspace, root_layer, excluded=packages_dir)
                layers.append(
                    _blob_descriptor_path(
                        root_layer,
                        LAYER_MEDIA_TYPE,
                        annotations={"org.lean-runtime.layer.kind": "root"},
                    )
                )
                entries["blobs/sha256/" + str(layers[-1]["digest"]).removeprefix("sha256:")] = (
                    root_layer
                )
                for index, (package, package_root) in enumerate(package_roots):
                    layer = staging / f"package-{index}.tar.gz"
                    _write_tar_gzip(package_root, layer)
                    descriptor = _blob_descriptor_path(
                        layer,
                        LAYER_MEDIA_TYPE,
                        annotations={
                            "org.lean-runtime.layer.kind": "package",
                            "org.lean-runtime.package.name": package.name,
                            "org.lean-runtime.package.source-id": package.source_id,
                            "org.lean-runtime.package.tree-hash": package.tree_hash,
                        },
                    )
                    layers.append(descriptor)
                    entries["blobs/sha256/" + str(descriptor["digest"]).removeprefix("sha256:")] = (
                        layer
                    )

                config_path = staging / "config.json"
                config_path.write_bytes(
                    canonical_json_bytes(
                        {
                            "schema": BUNDLE_SCHEMA,
                            "environment_id": environment_id,
                            "lock_id": lock.lock_id,
                            "lock": lock.to_dict(),
                            "build_profile": metadata["build_profile"],
                            "platform_compatibility": platform_compatibility(),
                            "builder": {"store_schema": metadata.get("schema")},
                        }
                    )
                )
                config_descriptor = _blob_descriptor_path(config_path, CONFIG_MEDIA_TYPE)
                entries[
                    "blobs/sha256/" + str(config_descriptor["digest"]).removeprefix("sha256:")
                ] = config_path
                manifest_path = staging / "manifest.json"
                manifest_path.write_bytes(
                    canonical_json_bytes(
                        {
                            "schemaVersion": 2,
                            "mediaType": MANIFEST_MEDIA_TYPE,
                            "config": config_descriptor,
                            "layers": layers,
                            "annotations": {
                                "org.lean-runtime.environment-id": environment_id,
                                "org.lean-runtime.lock-id": lock.lock_id,
                            },
                        }
                    )
                )
                compatibility = platform_compatibility()
                manifest_descriptor = _blob_descriptor_path(
                    manifest_path,
                    MANIFEST_MEDIA_TYPE,
                    annotations={
                        "org.lean-runtime.platform.schema": compatibility["schema"],
                        "org.lean-runtime.platform.abi": compatibility["abi"],
                    },
                    platform={
                        "os": compatibility["system"],
                        "architecture": {
                            "x86_64": "amd64",
                            "arm64": "arm64",
                        }.get(compatibility["machine"], compatibility["machine"]),
                    },
                )
                entries[
                    "blobs/sha256/" + str(manifest_descriptor["digest"]).removeprefix("sha256:")
                ] = manifest_path
                index_path = staging / "index.json"
                index_path.write_bytes(
                    canonical_json_bytes(
                        {
                            "schemaVersion": 2,
                            "mediaType": INDEX_MEDIA_TYPE,
                            "manifests": [manifest_descriptor],
                        }
                    )
                )
                layout_path = staging / "oci-layout"
                layout_path.write_bytes(b'{"imageLayoutVersion":"1.0.0"}')
                entries["index.json"] = index_path
                entries["oci-layout"] = layout_path
                _write_oci_archive(entries, temporary)
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
        self.events.emit(
            "bundle.exported", "Exported deterministic environment bundle", path=str(output)
        )
        return PortableCopyInfo(
            environment_id, lock.lock_id, manifest_descriptor["digest"], str(output)
        )

    def import_bundle(
        self, bundle: Path, *, name: str | None = None, probe: bool = True
    ) -> PortableCopyInfo:
        with tempfile.TemporaryDirectory(prefix="lean-runtime-import-") as temporary:
            entries = self._extract_oci_archive(bundle, Path(temporary))
            if entries.get("oci-layout") is None or entries["oci-layout"].read_bytes() != (
                b'{"imageLayoutVersion":"1.0.0"}'
            ):
                raise EnvironmentError("unsupported OCI image layout")
            index_path = entries.get("index.json")
            if index_path is None:
                raise EnvironmentError("OCI bundle has no index")
            return self.import_layout(
                _json_object(index_path.read_bytes(), "index"),
                entries,
                origin={"kind": "portable_copy", "copy": str(bundle)},
                name=name,
                probe=probe,
            )

    @staticmethod
    def _extract_oci_archive(bundle: Path, destination: Path) -> dict[str, Path]:
        entries: dict[str, Path] = {}
        total = 0
        count = 0
        try:
            archive = tarfile.open(bundle, mode="r:gz")  # noqa: SIM115
        except (tarfile.TarError, OSError) as exc:
            raise EnvironmentError(f"could not read OCI bundle: {bundle}") from exc
        with archive:
            for member in archive:
                count += 1
                if count > MAX_FILES:
                    raise EnvironmentError("OCI bundle exceeds file-count limit")
                _safe_name(member.name)
                if not member.isfile():
                    raise EnvironmentError("OCI bundle may contain only regular files")
                total += member.size
                if total > MAX_BUNDLE_BYTES:
                    raise EnvironmentError("OCI bundle exceeds import limits")
                source = archive.extractfile(member)
                assert source is not None
                if member.name in entries:
                    raise EnvironmentError(f"duplicate OCI bundle member: {member.name}")
                path = destination.joinpath(*PurePosixPath(member.name).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("wb") as output:
                    shutil.copyfileobj(source, output)
                entries[member.name] = path
        return entries

    def import_layout(
        self,
        index: dict[str, Any],
        entries: dict[str, Path],
        *,
        origin: dict[str, Any],
        name: str | None = None,
        probe: bool = True,
    ) -> PortableCopyInfo:
        """Verify and publish an OCI layout whose blobs are already on disk."""
        manifests = index.get("manifests")
        if (
            not isinstance(manifests, list)
            or len(manifests) != 1
            or not isinstance(manifests[0], dict)
        ):
            raise EnvironmentError("bundle index must contain exactly one manifest")
        manifest_descriptor = manifests[0]
        _require_media_type(manifest_descriptor, MANIFEST_MEDIA_TYPE, "manifest")
        manifest_path = _descriptor_blob_path(entries, manifest_descriptor, "manifest")
        manifest = _json_object(manifest_path.read_bytes(), "manifest")
        if manifest.get("mediaType") != MANIFEST_MEDIA_TYPE:
            raise EnvironmentError("unsupported bundle manifest media type")
        config_descriptor = manifest.get("config")
        layers = manifest.get("layers")
        if not isinstance(config_descriptor, dict) or not isinstance(layers, list):
            raise EnvironmentError("bundle manifest is incomplete")
        _require_media_type(config_descriptor, CONFIG_MEDIA_TYPE, "config")
        config_path = _descriptor_blob_path(entries, config_descriptor, "config")
        config = _json_object(config_path.read_bytes(), "config")
        if config.get("schema") != BUNDLE_SCHEMA or not isinstance(config.get("lock"), dict):
            raise EnvironmentError("unsupported environment bundle schema")
        lock = EnvironmentLock.from_dict(config["lock"])
        build_profile = config.get("build_profile")
        environment_id = environment_identity(lock, str(build_profile))
        if config.get("lock_id") != lock.lock_id or config.get("environment_id") != environment_id:
            raise EnvironmentError("bundle identity mismatch")
        if config.get("platform_compatibility") != platform_compatibility():
            raise EnvironmentError("bundle platform is not compatible with this host")

        destination = self.store.environment_path(environment_id)
        with FileLock(self.store.lock_dir / f"{environment_id}.lock", timeout=1800):
            if not destination.is_dir():
                stage = self.store.environments / f".staging-{os.getpid()}-{uuid.uuid4().hex}"
                workspace = stage / "workspace"
                try:
                    package_descriptors: dict[str, dict[str, Any]] = {}
                    root_descriptor: dict[str, Any] | None = None
                    for descriptor in layers:
                        if not isinstance(descriptor, dict):
                            raise EnvironmentError("bundle has an invalid layer descriptor")
                        _require_media_type(descriptor, LAYER_MEDIA_TYPE, "layer")
                        annotations = descriptor.get("annotations", {})
                        kind = (
                            annotations.get("org.lean-runtime.layer.kind")
                            if isinstance(annotations, dict)
                            else None
                        )
                        if kind == "root" and root_descriptor is None:
                            root_descriptor = descriptor
                        elif kind == "package" and isinstance(
                            annotations.get("org.lean-runtime.package.name"), str
                        ):
                            package_descriptors[annotations["org.lean-runtime.package.name"]] = (
                                descriptor
                            )
                        else:
                            raise EnvironmentError("bundle has an unknown or duplicate layer")
                    if root_descriptor is None or set(package_descriptors) != {
                        p.name for p in lock.packages
                    }:
                        raise EnvironmentError("bundle layers do not match the environment lock")
                    _extract_layer(
                        _descriptor_blob_path(entries, root_descriptor, "root layer"), workspace
                    )
                    _verify_workspace_lock(workspace, lock)
                    packages_dir = workspace.joinpath(*_packages_directory(lock).parts)
                    for package in lock.packages:
                        descriptor = package_descriptors[package.name]
                        annotations = descriptor["annotations"]
                        if (
                            annotations.get("org.lean-runtime.package.source-id")
                            != package.source_id
                            or annotations.get("org.lean-runtime.package.tree-hash")
                            != package.tree_hash
                        ):
                            raise EnvironmentError(
                                f"bundle package annotation mismatch: {package.name}"
                            )
                        package_root = packages_dir / package.name
                        _extract_layer(
                            _descriptor_blob_path(entries, descriptor, f"package {package.name}"),
                            package_root,
                        )
                        _verify_package(package_root, package)
                    if probe:
                        command = self.toolchains.command(
                            lock.toolchain, "lake", "env", "lean", f"{ROOT_MODULE}.lean"
                        )
                        result = self.backend.execute(
                            command,
                            cwd=workspace,
                            environment=self.toolchains.environment,
                            policy=ExecutionPolicy(timeout_seconds=300, max_output_bytes=2_000_000),
                        )
                        if result.exit_code:
                            raise EnvironmentError(
                                "imported environment probe failed: "
                                + (result.stdout + result.stderr)[-2000:]
                            )
                    metadata = {
                        "schema": "lean-runtime-published-environment/1",
                        "environment_id": environment_id,
                        "lock_id": lock.lock_id,
                        "toolchain": lock.toolchain,
                        "platform": platform_record(),
                        "platform_compatibility": platform_compatibility(),
                        "build_profile": build_profile,
                        "status": "ready",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "origin": {
                            **origin,
                            "manifest_digest": manifest_descriptor["digest"],
                            "blob_digests": sorted(
                                "sha256:" + key.removeprefix("blobs/sha256/")
                                for key in entries
                                if key.startswith("blobs/sha256/")
                            ),
                        },
                    }
                    write_json_atomic(stage / "metadata.json", metadata)
                    self.store.publish_lock(lock)
                    stage.replace(destination)
                finally:
                    if stage.exists():
                        shutil.rmtree(stage)
        if name:
            self.store.set_alias(name, environment_id)
        self.events.emit(
            "bundle.imported",
            "Imported verified environment bundle",
            origin=origin,
        )
        return PortableCopyInfo(
            environment_id,
            lock.lock_id,
            str(manifest_descriptor["digest"]),
            str(origin.get("bundle", origin.get("registry", "oci-layout"))),
        )
