"""Verified portable copies of published environments."""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import zstandard

from ._archive import (
    extract_layer,
    extract_oci_archive,
    write_oci_archive,
    write_tar_gzip,
)
from ._paths import remove_tree
from ._sources import (
    SOURCE_TREE_INVENTORY,
    source_tree_inventory,
    verify_package,
    verify_workspace_lock,
)
from ._transaction import publish_tree, staged_tree
from .backends import Backend
from .capsules import (
    CAPSULE_MANIFEST,
    ArtifactCapability,
    CapsuleManifest,
    inventory_workspace,
    materialize_capsule,
    render_setup,
    setup_artifact_groups,
)
from .errors import EnvironmentError
from .events import EventEmitter
from .lake import ROOT_MODULE
from .lockfiles import EnvironmentLock, LockedPackage
from .locking import FileLock
from .oci_protocol import (
    INDEX_MEDIA_TYPE,
    MANIFEST_MEDIA_TYPE,
)
from .oci_protocol import (
    blob_descriptor_path as _blob_descriptor_path,
)
from .oci_protocol import (
    descriptor_blob_path as _descriptor_blob_path,
)
from .oci_protocol import (
    json_object as _json_object,
)
from .oci_protocol import (
    require_media_type as _require_media_type,
)
from .packs import (
    PACK_MEDIA_TYPE,
    SparsePack,
    build_sparse_packs,
    project_artifacts,
    unpack_frame,
)
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
CAPSULE_BUNDLE_SCHEMA = "lean-runtime-oci-capsule/1"
CONFIG_MEDIA_TYPE = "application/vnd.lean-runtime.environment.config.v1+json"
CAPSULE_CONFIG_MEDIA_TYPE = "application/vnd.lean-runtime.capsule.config.v1+json+zstd"
LAYER_MEDIA_TYPE = "application/vnd.lean-runtime.environment.layer.v1.tar+gzip"
MAX_CAPSULE_CONFIG_BYTES = 64 * 1024**2


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


def capsule_config_bytes(value: Mapping[str, Any]) -> bytes:
    return zstandard.ZstdCompressor(level=10, write_checksum=True).compress(
        canonical_json_bytes(dict(value))
    )


def capsule_config_object(data: bytes) -> dict[str, Any]:
    try:
        decoded = zstandard.ZstdDecompressor().decompress(
            data, max_output_size=MAX_CAPSULE_CONFIG_BYTES
        )
    except zstandard.ZstdError as exc:
        raise EnvironmentError("OCI capsule config is not valid zstd data") from exc
    return _json_object(decoded, "capsule config")


def _bundle_lean_paths(workspace: Path, lock: EnvironmentLock) -> tuple[Path, ...]:
    roots: list[Path] = []
    for package in lock.packages:
        root = lock.package_root(workspace, package)
        compiled = root / ".lake" / "build" / "lib" / "lean"
        if compiled.is_dir():
            roots.append(compiled)
    workspace_root = workspace / ".lake" / "build" / "lib" / "lean"
    if workspace_root.is_dir():
        roots.append(workspace_root)
    return tuple(roots)


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

    def export_layout(self, environment_id: str, output: Path) -> PortableCopyInfo:
        """Write verified OCI layout files without wrapping them in a second archive."""
        root = self.store.environment_path(environment_id)
        metadata = _json_object((root / "metadata.json").read_bytes(), "metadata")
        lock = self.store.load_lock(str(metadata["lock_id"]))
        expected = environment_identity(lock, str(metadata["build_profile"]))
        if expected != environment_id:
            raise EnvironmentError(f"environment identity mismatch: {environment_id}")
        workspace = root / "workspace"
        packages_dir = workspace.joinpath(*lock.packages_directory.parts)
        verify_workspace_lock(workspace, lock)
        package_roots: list[tuple[LockedPackage, Path]] = []
        for package in sorted(lock.packages, key=lambda item: item.name):
            package_root = packages_dir / package.name
            if not package_root.is_dir():
                raise EnvironmentError(f"environment package is missing: {package.name}")
            verify_package(package_root, package)
            package_roots.append((package, package_root))
        if output.is_symlink() or (output.exists() and not output.is_dir()):
            raise EnvironmentError(f"OCI layout destination is not a directory: {output}")
        if output.exists() and any(output.iterdir()):
            raise EnvironmentError(f"OCI layout destination is not empty: {output}")
        output.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix="lean-runtime-export-") as temporary_dir:
                staging = Path(temporary_dir)
                entries: dict[str, Path] = {}
                layers: list[dict[str, Any]] = []

                root_layer = staging / "root.tar.gz"
                write_tar_gzip(
                    workspace,
                    root_layer,
                    excluded=packages_dir,
                    omit_volatile_build_metadata=True,
                )
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
                    inventory = source_tree_inventory(package_root, package)
                    write_tar_gzip(
                        package_root,
                        layer,
                        excluded_names=frozenset({".git", SOURCE_TREE_INVENTORY}),
                        extra_files={SOURCE_TREE_INVENTORY: inventory},
                        omit_volatile_build_metadata=True,
                    )
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
                for name, path in entries.items():
                    destination = output.joinpath(*PurePosixPath(name).parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if path != destination:
                        path.replace(destination)
        except BaseException:
            remove_tree(output)
            raise
        self.events.emit(
            "bundle.layout_exported",
            "Exported deterministic environment OCI layout",
            path=str(output),
        )
        return PortableCopyInfo(
            environment_id, lock.lock_id, manifest_descriptor["digest"], str(output)
        )

    def export_capsule_layout(
        self,
        environment_id: str,
        output: Path,
        *,
        capsule_manifest: CapsuleManifest | None = None,
        target_frame_bytes: int | None = None,
        probe: bool = True,
        roots: Sequence[str] | None = None,
        capabilities: frozenset[ArtifactCapability] | None = None,
    ) -> PortableCopyInfo:
        """Write a source-free, sparse check-capsule OCI layout.

        ``capsule_manifest`` is an internal test/publisher-resume hook. Normal
        publication inventories headers with the exact selected Lean parser.
        """
        root = self.store.environment_path(environment_id)
        metadata = _json_object((root / "metadata.json").read_bytes(), "metadata")
        lock = self.store.load_lock(str(metadata["lock_id"]))
        expected = environment_identity(lock, str(metadata["build_profile"]))
        if expected != environment_id:
            raise EnvironmentError(f"environment identity mismatch: {environment_id}")
        workspace = root / "workspace"
        verify_workspace_lock(workspace, lock)
        manifest = capsule_manifest or inventory_workspace(
            workspace,
            lock,
            environment_id,
            self.toolchains.command(lock.toolchain, "lean"),
        )
        if (
            manifest.environment_id != environment_id
            or manifest.lock_id != lock.lock_id
            or manifest.toolchain != lock.toolchain
        ):
            raise EnvironmentError("check capsule identity does not match its environment")
        if roots is not None:
            manifest = CapsuleManifest(
                manifest.environment_id,
                manifest.lock_id,
                manifest.toolchain,
                manifest.closure(roots),
            )
        if probe:
            self._verify_capsule(workspace, lock, manifest)
        if output.is_symlink() or (output.exists() and not output.is_dir()):
            raise EnvironmentError(f"OCI layout destination is not a directory: {output}")
        if output.exists() and any(output.iterdir()):
            raise EnvironmentError(f"OCI layout destination is not empty: {output}")
        output.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix="lean-runtime-capsule-") as temporary_dir:
                staging = Path(temporary_dir)
                packs = build_sparse_packs(
                    workspace,
                    manifest,
                    staging / "packs",
                    **(
                        {"target_frame_bytes": target_frame_bytes}
                        if target_frame_bytes is not None
                        else {}
                    ),
                    capabilities=capabilities,
                )
                entries: dict[str, Path] = {}
                layers: list[dict[str, Any]] = []
                for pack in packs:
                    assert pack.path is not None
                    descriptor = _blob_descriptor_path(
                        pack.path,
                        PACK_MEDIA_TYPE,
                        annotations={
                            "org.lean-runtime.layer.kind": "sparse-pack",
                            "org.lean-runtime.package.name": pack.package,
                            "org.lean-runtime.capability": pack.capability,
                        },
                    )
                    if descriptor["digest"] != pack.digest or descriptor["size"] != pack.size:
                        raise EnvironmentError("sparse pack descriptor changed during export")
                    layers.append(descriptor)
                    entries["blobs/sha256/" + str(descriptor["digest"]).removeprefix("sha256:")] = (
                        pack.path
                    )

                config_path = staging / "config.json"
                config_path.write_bytes(
                    capsule_config_bytes(
                        {
                            "schema": CAPSULE_BUNDLE_SCHEMA,
                            "environment_id": environment_id,
                            "lock_id": lock.lock_id,
                            "lock": lock.to_dict(),
                            "build_profile": metadata["build_profile"],
                            "platform_compatibility": platform_compatibility(),
                            "capsule": manifest.to_dict(),
                            "packs": [pack.to_dict() for pack in packs],
                        }
                    )
                )
                config_descriptor = _blob_descriptor_path(config_path, CAPSULE_CONFIG_MEDIA_TYPE)
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
                                "org.lean-runtime.profile": "check-capsule",
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
                        "org.lean-runtime.profile": "check-capsule",
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
                for name, path in entries.items():
                    destination = output.joinpath(*PurePosixPath(name).parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if path != destination:
                        shutil.copy2(path, destination)
        except BaseException:
            remove_tree(output)
            raise
        self.events.emit(
            "capsule.layout_exported",
            "Exported sparse check-capsule OCI layout",
            path=str(output),
            modules=len(manifest.modules),
        )
        return PortableCopyInfo(
            environment_id, lock.lock_id, manifest_descriptor["digest"], str(output)
        )

    def _verify_capsule(
        self, workspace: Path, lock: EnvironmentLock, manifest: CapsuleManifest
    ) -> None:
        """Differentially check a full workspace and an isolated capsule."""
        roots = tuple(
            dict.fromkeys(
                module
                for line in lock.root_module.splitlines()
                if line.strip().startswith("import ")
                for module in line.strip().removeprefix("import ").split()
            )
        )
        source = lock.root_module + "\nexample : True := by trivial\n"
        policy = ExecutionPolicy(timeout_seconds=600, max_output_bytes=2_000_000)
        command = self.toolchains.command(lock.toolchain, "lean")
        with tempfile.TemporaryDirectory(
            prefix="capsule-differential-", dir=self.store.home
        ) as raw:
            root = Path(raw)
            full_source = root / "FullProbe.lean"
            full_source.write_text(source)
            full_environment = self.toolchains.environment
            full_environment["LEAN_PATH"] = os.pathsep.join(
                str(path) for path in _bundle_lean_paths(workspace, lock)
            )
            full = self.backend.execute(
                [*command, str(full_source)],
                cwd=root,
                environment=full_environment,
                policy=policy,
            )
            if full.exit_code:
                raise EnvironmentError(
                    "full environment failed the capsule differential probe: "
                    + (full.stdout + full.stderr)[-2000:]
                )

            capsule = root / "isolated"
            materialize_capsule(workspace, capsule, manifest)
            capsule_source = root / "CapsuleProbe.lean"
            capsule_source.write_text(source)
            setup_path = root / "CapsuleProbe.setup.json"
            setup_path.write_bytes(
                canonical_json_bytes(
                    render_setup(
                        lean_version=lock.toolchain,
                        name="CapsuleProbe",
                        package="lean_runtime_probe",
                        import_artifacts=setup_artifact_groups(manifest, capsule, roots),
                    )
                )
            )
            isolated = self.backend.execute(
                [*command, f"--setup={setup_path}", str(capsule_source)],
                cwd=root,
                environment=self.toolchains.environment,
                policy=policy,
            )
            if isolated.exit_code:
                raise EnvironmentError(
                    "isolated check capsule failed the differential probe: "
                    + (isolated.stdout + isolated.stderr)[-2000:]
                )
        self.events.emit(
            "capsule.differential_verified",
            "Full environment and isolated capsule accepted the verification probe",
            modules=len(manifest.modules),
        )

    def _probe_capsule_projection(
        self, workspace: Path, lock: EnvironmentLock, capsule: CapsuleManifest
    ) -> None:
        """Check the locked public root using only one capsule projection."""
        roots = tuple(
            module
            for line in lock.root_module.splitlines()
            if line.strip().startswith("import ")
            for module in line.strip().removeprefix("import ").split()
        )
        source = lock.root_module + "\nexample : True := by trivial\n"
        with tempfile.TemporaryDirectory(prefix="portable-capsule-probe-") as raw:
            probe_root = Path(raw)
            source_path = probe_root / "Probe.lean"
            source_path.write_text(source)
            setup_path = probe_root / "Probe.setup.json"
            setup_path.write_bytes(
                canonical_json_bytes(
                    render_setup(
                        lean_version=lock.toolchain,
                        name="Probe",
                        package="lean_runtime_probe",
                        import_artifacts=setup_artifact_groups(capsule, workspace, roots),
                    )
                )
            )
            result = self.backend.execute(
                [
                    *self.toolchains.command(lock.toolchain, "lean"),
                    f"--setup={setup_path}",
                    str(source_path),
                ],
                cwd=probe_root,
                environment=self.toolchains.environment,
                policy=ExecutionPolicy(timeout_seconds=300, max_output_bytes=2_000_000),
            )
            if result.exit_code:
                raise EnvironmentError(
                    "portable capsule Lean probe failed: " + (result.stdout + result.stderr)[-2000:]
                )

    def export(self, environment_id: str, output: Path) -> PortableCopyInfo:
        """Write a deterministic portable archive from a verified direct OCI layout."""
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            with tempfile.TemporaryDirectory(prefix="lean-runtime-layout-") as raw:
                layout = Path(raw)
                info = self.export_layout(environment_id, layout)
                entries = {
                    path.relative_to(layout).as_posix(): path
                    for path in layout.rglob("*")
                    if path.is_file()
                }
                write_oci_archive(entries, temporary)
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
        self.events.emit(
            "bundle.exported", "Exported deterministic environment bundle", path=str(output)
        )
        return PortableCopyInfo(
            info.environment_id,
            info.exact_environment_id,
            info.copy_id,
            str(output),
        )

    def export_capsule(
        self,
        environment_id: str,
        output: Path,
        *,
        roots: Sequence[str] | None = None,
    ) -> PortableCopyInfo:
        """Write a portable source-free archive for one import closure."""
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            with tempfile.TemporaryDirectory(prefix="lean-runtime-capsule-layout-") as raw:
                layout = Path(raw)
                info = self.export_capsule_layout(
                    environment_id,
                    layout,
                    roots=roots,
                    capabilities=frozenset({"check"}),
                )
                entries = {
                    path.relative_to(layout).as_posix(): path
                    for path in layout.rglob("*")
                    if path.is_file()
                }
                write_oci_archive(entries, temporary)
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
        self.events.emit(
            "capsule.exported", "Exported portable sparse check capsule", path=str(output)
        )
        return PortableCopyInfo(
            info.environment_id,
            info.exact_environment_id,
            info.copy_id,
            str(output),
        )

    def import_bundle(
        self, bundle: Path, *, name: str | None = None, probe: bool = True
    ) -> PortableCopyInfo:
        with tempfile.TemporaryDirectory(prefix="lean-runtime-import-") as temporary:
            entries = extract_oci_archive(bundle, Path(temporary))
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

    def _import_capsule_layout(
        self,
        manifest_descriptor: dict[str, Any],
        config_descriptor: dict[str, Any],
        layers: list[Any],
        entries: dict[str, Path],
        *,
        origin: dict[str, Any],
        name: str | None,
        probe: bool,
    ) -> PortableCopyInfo:
        """Verify and atomically import a portable sparse capsule."""
        _require_media_type(config_descriptor, CAPSULE_CONFIG_MEDIA_TYPE, "capsule config")
        config_path = _descriptor_blob_path(entries, config_descriptor, "capsule config")
        config = capsule_config_object(config_path.read_bytes())
        if (
            config.get("schema") != CAPSULE_BUNDLE_SCHEMA
            or not isinstance(config.get("lock"), dict)
            or not isinstance(config.get("capsule"), dict)
            or not isinstance(config.get("packs"), list)
        ):
            raise EnvironmentError("unsupported portable capsule schema")
        lock = EnvironmentLock.from_dict(config["lock"])
        build_profile = str(config.get("build_profile"))
        environment_id = environment_identity(lock, build_profile)
        capsule = CapsuleManifest.from_dict(config["capsule"])
        if (
            config.get("lock_id") != lock.lock_id
            or config.get("environment_id") != environment_id
            or capsule.environment_id != environment_id
            or capsule.lock_id != lock.lock_id
            or capsule.toolchain != lock.toolchain
            or config.get("platform_compatibility") != platform_compatibility()
        ):
            raise EnvironmentError("portable capsule identity mismatch")

        descriptors = {
            str(item.get("digest")): item
            for item in layers
            if isinstance(item, dict) and item.get("mediaType") == PACK_MEDIA_TYPE
        }
        packs: list[tuple[SparsePack, Path]] = []
        packed_paths: set[str] = set()
        capabilities: set[str] = set()
        for raw in config["packs"]:
            if not isinstance(raw, dict):
                raise EnvironmentError("portable capsule has an invalid pack index")
            pack = SparsePack.from_dict(raw)
            descriptor = descriptors.get(pack.digest)
            if descriptor is None or descriptor.get("size") != pack.size:
                raise EnvironmentError("portable capsule pack descriptor mismatch")
            path = _descriptor_blob_path(entries, descriptor, "capsule pack")
            for frame in pack.frames:
                if packed_paths.intersection(frame.artifacts):
                    raise EnvironmentError("portable capsule repeats packed artifacts")
                packed_paths.update(frame.artifacts)
            if pack.capability != "check":
                raise EnvironmentError("portable check capsule contains an optional overlay")
            capabilities.add(pack.capability)
            packs.append((pack, path))
        artifacts = {
            artifact.path: artifact for module in capsule.modules for artifact in module.artifacts
        }
        expected_paths = {
            path for path, artifact in artifacts.items() if artifact.capability == "check"
        }
        if packed_paths != expected_paths:
            raise EnvironmentError("portable capsule pack inventory is incomplete")

        destination = self.store.environment_path(environment_id)
        probe_existing = False
        with FileLock(self.store.lock_paths.environment(environment_id), timeout=1800):
            if destination.is_dir():
                probe_existing = probe
                existing = destination / "workspace" / CAPSULE_MANIFEST
                if (
                    not existing.is_file()
                    or CapsuleManifest.load(existing).digest != capsule.digest
                ):
                    raise EnvironmentError(
                        "portable capsule conflicts with an existing environment"
                    )
            else:
                with staged_tree(self.store.environments, self.store.lock_paths) as stage:
                    workspace = stage / "workspace"
                    for pack, path in packs:
                        with path.open("rb") as handle:
                            for frame in pack.frames:
                                handle.seek(frame.offset)
                                unpack_frame(
                                    handle.read(frame.size),
                                    frame,
                                    artifacts,
                                    self.store.cas_artifacts,
                                    locks=self.store.lock_paths,
                                )
                    project_artifacts(
                        packed_paths,
                        artifacts,
                        self.store.cas_artifacts,
                        workspace,
                        locks=self.store.lock_paths,
                    )
                    capsule_path = workspace / CAPSULE_MANIFEST
                    capsule_path.parent.mkdir(parents=True, exist_ok=True)
                    capsule_path.write_bytes(canonical_json_bytes(capsule.to_dict()))
                    (workspace / ".lake" / "build").mkdir(parents=True, exist_ok=True)
                    (workspace / "lean-toolchain").write_text(lock.toolchain + "\n")
                    (workspace / "lakefile.toml").write_text(lock.root_lakefile)
                    (workspace / f"{ROOT_MODULE}.lean").write_text(lock.root_module)
                    (workspace / "lake-manifest.json").write_bytes(
                        canonical_json_bytes(lock.manifest)
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
                            "kind": "portable_sparse_capsule",
                            "modules": [module.name for module in capsule.modules],
                            "capabilities": sorted(capabilities),
                        },
                    }
                    if probe:
                        self._probe_capsule_projection(workspace, lock, capsule)
                    write_json_atomic(stage / "metadata.json", metadata)
                    self.store.publish_lock(lock)
                    publish_tree(stage, destination)
        if probe_existing:
            self._probe_capsule_projection(destination / "workspace", lock, capsule)
        if name:
            self.store.set_alias(name, environment_id)
        return PortableCopyInfo(
            environment_id,
            lock.lock_id,
            str(manifest_descriptor["digest"]),
            str(origin.get("copy", "oci-layout")),
        )

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
        if config_descriptor.get("mediaType") == CAPSULE_CONFIG_MEDIA_TYPE:
            return self._import_capsule_layout(
                manifest_descriptor,
                config_descriptor,
                layers,
                entries,
                origin=origin,
                name=name,
                probe=probe,
            )
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
        with FileLock(self.store.lock_paths.environment(environment_id), timeout=1800):
            if not destination.is_dir():
                with staged_tree(self.store.environments, self.store.lock_paths) as stage:
                    workspace = stage / "workspace"
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
                    extract_layer(
                        _descriptor_blob_path(entries, root_descriptor, "root layer"), workspace
                    )
                    verify_workspace_lock(workspace, lock)
                    packages_dir = workspace.joinpath(*lock.packages_directory.parts)
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
                        extract_layer(
                            _descriptor_blob_path(entries, descriptor, f"package {package.name}"),
                            package_root,
                        )
                        verify_package(package_root, package)
                    if probe:
                        command = self.toolchains.command(
                            lock.toolchain, "lean", f"{ROOT_MODULE}.lean"
                        )
                        probe_environment = self.toolchains.environment
                        probe_environment["LEAN_PATH"] = os.pathsep.join(
                            str(path) for path in _bundle_lean_paths(workspace, lock)
                        )
                        result = self.backend.execute(
                            command,
                            cwd=workspace,
                            environment=probe_environment,
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
                    publish_tree(stage, destination)
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
