"""Ready-to-run programs for fast, verified execution.

Programs are deliberately distinct from published environments.  They retain
an attested executable and its runtime files, but they do not claim to contain
the sources and compiler state required for an independent rebuild.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, cast

from .backends import Backend, BackendResult, InteractiveProcess
from .bundles import (
    INDEX_MEDIA_TYPE,
    MANIFEST_MEDIA_TYPE,
    _blob_descriptor_path,
    _descriptor_blob_path,
    _extract_layer,
    _json_object,
    _require_media_type,
    _safe_name,
    _write_tar_gzip,
)
from .diagnostics import error_diagnostic, parse_diagnostics
from .environments import InteractiveSession
from .errors import DownloadUnavailable, EnvironmentError, PolicyError
from .events import EventEmitter
from .locking import FileLock
from .models import ExecutionProvenance, ExecutionResult, PhaseTiming
from .oci import (
    OCIRegistryClient,
    OCIRepository,
    SignatureVerifier,
)
from .policies import ExecutionPolicy
from .serialization import canonical_json_bytes, sha256_id, write_json_atomic
from .store import EnvironmentStore, clone_tree, platform_compatibility, platform_record

PROGRAM_SCHEMA = "lean-runtime-execution-program/1"
PROGRAM_CONFIG_MEDIA_TYPE = "application/vnd.lean-runtime.program.config.v1+json"
PROGRAM_LAYER_MEDIA_TYPE = "application/vnd.lean-runtime.program.layer.v1.tar+gzip"
_PROGRAM_ID = re.compile(r"program_[0-9a-f]{64}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _relative_executable(value: str) -> str:
    path = _safe_name(value.replace("\\", "/"))
    if path == PurePosixPath("."):
        raise EnvironmentError("program executable must name a file")
    return path.as_posix()


def _file_inventory(root: Path) -> dict[str, dict[str, Any]]:
    inventory: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            target = os.readlink(path)
            resolved = PurePosixPath(relative).parent / target
            if PurePosixPath(target).is_absolute() or ".." in resolved.parts:
                raise EnvironmentError(f"program contains unsafe symlink: {relative}")
            inventory[relative] = {"kind": "symlink", "target": target}
        elif path.is_file():
            inventory[relative] = {
                "kind": "file",
                "sha256": _digest_file(path),
                "size": path.stat().st_size,
                "executable": bool(path.stat().st_mode & 0o111),
            }
        elif not path.is_dir():
            raise EnvironmentError(f"program contains unsupported entry: {relative}")
    if not inventory:
        raise EnvironmentError("program payload must not be empty")
    return inventory


@dataclass(frozen=True, slots=True)
class ProgramDescription:
    command: tuple[str, ...]
    files: dict[str, dict[str, Any]]
    computer_compatibility: dict[str, str]
    source_revision: str
    source_environment_id: str | None = None
    exact_environment_id: str | None = None
    toolchain: str = "unknown"
    capability_id: str | None = None
    schema: str = PROGRAM_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PROGRAM_SCHEMA:
            raise EnvironmentError(f"unsupported program schema: {self.schema!r}")
        if not self.command or any(not isinstance(part, str) or not part for part in self.command):
            raise EnvironmentError("program command must contain non-empty strings")
        _relative_executable(self.command[0])
        if self.command[0].replace("\\", "/") not in self.files:
            raise EnvironmentError("program command executable is absent from its file inventory")
        if not re.fullmatch(r"[0-9a-f]{40,64}", self.source_revision):
            raise EnvironmentError("program source revision must be an exact Git commit")

    @property
    def program_id(self) -> str:
        return sha256_id("program", self.to_dict(include_id=False))

    def to_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        value = {
            "schema": self.schema,
            "command": list(self.command),
            "files": dict(sorted(self.files.items())),
            "computer_compatibility": self.computer_compatibility,
            "source_revision": self.source_revision,
            "source_environment_id": self.source_environment_id,
            "exact_environment_id": self.exact_environment_id,
            "toolchain": self.toolchain,
            "capability_id": self.capability_id,
        }
        return {"program_id": self.program_id, **value} if include_id else value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProgramDescription:
        command = value.get("command")
        files = value.get("files")
        compatibility = value.get("computer_compatibility")
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            raise EnvironmentError("program manifest command is invalid")
        if not isinstance(files, dict) or not all(
            isinstance(name, str) and isinstance(record, dict) for name, record in files.items()
        ):
            raise EnvironmentError("program manifest file inventory is invalid")
        if not isinstance(compatibility, dict):
            raise EnvironmentError("program platform compatibility is invalid")
        manifest = cls(
            command=tuple(command),
            files={str(name): dict(record) for name, record in files.items()},
            computer_compatibility={str(k): str(v) for k, v in compatibility.items()},
            source_revision=str(value.get("source_revision", "")),
            source_environment_id=(
                str(value["source_environment_id"])
                if value.get("source_environment_id") is not None
                else None
            ),
            exact_environment_id=(
                str(value["exact_environment_id"])
                if value.get("exact_environment_id") is not None
                else None
            ),
            toolchain=str(value.get("toolchain", "unknown")),
            capability_id=(
                str(value["capability_id"]) if value.get("capability_id") is not None else None
            ),
            schema=str(value.get("schema", "")),
        )
        recorded = value.get("program_id")
        if recorded is not None and recorded != manifest.program_id:
            raise EnvironmentError("program identity mismatch")
        return manifest


@dataclass(frozen=True, slots=True)
class ProgramInfo:
    program_id: str
    source_revision: str
    copy_id: str | None
    location: str
    computer_record: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ReadyProgram:
    def __init__(
        self,
        store: EnvironmentStore,
        backend: Backend,
        description: ProgramDescription,
        root: Path,
        copy_id: str | None = None,
    ) -> None:
        self.store = store
        self.backend = backend
        self.description = description
        self.root = root
        self.copy_id = copy_id

    @property
    def id(self) -> str:
        return self.description.program_id

    def spawn_interactive(
        self,
        command: Sequence[str] | None = None,
        *,
        policy: ExecutionPolicy | None = None,
    ) -> InteractiveSession:
        requested = tuple(command or self.description.command)
        if not requested or requested[0].replace("\\", "/") != self.description.command[0]:
            raise EnvironmentError("program execution must use its declared executable")
        selected_policy = policy or ExecutionPolicy()
        started_at = _now()
        request_digest = sha256_id(
            "request",
            {
                "program_id": self.id,
                "command": list(requested),
                "policy": selected_policy.to_dict(),
                "backend": self.backend.name,
            },
        )
        execution_id = sha256_id(
            "execution",
            {"request_digest": request_digest, "started_at": started_at, "nonce": uuid.uuid4().hex},
        )
        job_parent = self.store.jobs / execution_id
        job_parent.mkdir(parents=True, exist_ok=True)
        instance = job_parent / f"program-{uuid.uuid4().hex}"

        def cleanup() -> None:
            if instance.exists():
                shutil.rmtree(instance)
            with suppress(OSError):
                job_parent.rmdir()

        try:
            clone_tree(self.root / "payload", instance)
            executable = instance.joinpath(*PurePosixPath(self.description.command[0]).parts)
            resolved = (str(executable), *requested[1:])
            spawn = getattr(self.backend, "spawn_interactive", None)
            if not callable(spawn):
                raise PolicyError(
                    f"backend {self.backend.name!r} does not support interactive execution"
                )
            process = cast(
                InteractiveProcess,
                spawn(resolved, cwd=instance, environment=dict(os.environ), policy=selected_policy),
            )
        except BaseException:
            cleanup()
            raise

        def finalize(raw: BackendResult) -> ExecutionResult:
            combined = "\n".join(part for part in (raw.stdout, raw.stderr) if part)
            diagnostics = parse_diagnostics(combined)
            if raw.timed_out:
                diagnostics += (error_diagnostic("Program execution exceeded its time limit"),)
            provenance = ExecutionProvenance(
                environment_id=self.description.source_environment_id,
                execution_id=execution_id,
                request_digest=request_digest,
                lock_id=self.description.exact_environment_id,
                toolchain=self.description.toolchain,
                packages=(),
                platform=platform_record(),
                backend=self.backend.name,
                requested_policy=selected_policy.to_dict(),
                enforced_policy_fields=raw.enforced_policy_fields,
                source_digest=self.description.capability_id or self.id,
                started_at=started_at,
                program_id=self.id,
                program_copy_id=self.copy_id,
            )
            result = ExecutionResult(
                ok=raw.exit_code == 0,
                exit_code=raw.exit_code,
                toolchain=self.description.toolchain,
                command=tuple(resolved),
                cwd=str(instance),
                stdout=raw.stdout,
                stderr=raw.stderr,
                elapsed_seconds=raw.elapsed_seconds,
                timed_out=raw.timed_out,
                cancelled=raw.cancelled,
                output_truncated=raw.output_truncated,
                diagnostics=diagnostics,
                provenance=provenance,
                timings=(PhaseTiming("execution", round(raw.elapsed_seconds * 1000)),),
            )
            write_json_atomic(
                self.store.executions / f"{execution_id}.json",
                {"schema": "lean-runtime-program-execution/1", "result": result.to_dict()},
            )
            return result

        return InteractiveSession(
            process=process, execution_id=execution_id, finalize=finalize, cleanup=cleanup
        )


class ProgramManager:
    def __init__(self, store: EnvironmentStore, backend: Backend, events: EventEmitter) -> None:
        self.store = store
        self.backend = backend
        self.events = events

    def create(
        self,
        payload: Path,
        *,
        command: Sequence[str],
        source_revision: str,
        source_environment_id: str | None = None,
        exact_environment_id: str | None = None,
        toolchain: str = "unknown",
        capability_id: str | None = None,
    ) -> ReadyProgram:
        payload = payload.expanduser().resolve()
        if not payload.is_dir():
            raise EnvironmentError(f"program payload directory does not exist: {payload}")
        manifest = ProgramDescription(
            command=tuple(command),
            files=_file_inventory(payload),
            computer_compatibility=platform_compatibility(),
            source_revision=source_revision,
            source_environment_id=source_environment_id,
            exact_environment_id=exact_environment_id,
            toolchain=toolchain,
            capability_id=capability_id,
        )
        destination = self.store.programs / manifest.program_id
        with FileLock(self.store.lock_dir / f"{manifest.program_id}.lock", timeout=1800):
            if not destination.exists():
                stage = self.store.programs / f".staging-{uuid.uuid4().hex}"
                try:
                    clone_tree(payload, stage / "payload")
                    write_json_atomic(stage / "program.json", manifest.to_dict())
                    stage.replace(destination)
                finally:
                    if stage.exists():
                        shutil.rmtree(stage)
        self.events.emit(
            "program.created", "Created executable program", program_id=manifest.program_id
        )
        return self.open(manifest.program_id)

    def open(self, program_id: str) -> ReadyProgram:
        if not _PROGRAM_ID.fullmatch(program_id):
            raise EnvironmentError(f"invalid program identity: {program_id!r}")
        root = self.store.programs / program_id
        path = root / "program.json"
        if not path.is_file():
            raise EnvironmentError(f"ready-to-run program is not present: {program_id}")
        manifest = ProgramDescription.from_dict(_json_object(path.read_bytes(), "program manifest"))
        if (
            manifest.program_id != program_id
            or manifest.computer_compatibility != platform_compatibility()
        ):
            raise EnvironmentError("ready-to-run program identity or computer mismatch")
        observed = _file_inventory(root / "payload")
        if observed != manifest.files:
            raise EnvironmentError("ready-to-run program payload digest mismatch")
        metadata = root / "origin.json"
        digest = None
        if metadata.is_file():
            origin = _json_object(metadata.read_bytes(), "program origin")
            digest = origin.get("copy_id")
        return ReadyProgram(self.store, self.backend, manifest, root, digest)

    def export(self, program_id: str, output: Path) -> ProgramInfo:
        program = self.open(program_id)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
        try:
            with tempfile.TemporaryDirectory(prefix="lean-runtime-program-save-copy-") as directory:
                staging = Path(directory)
                layer_path = staging / "payload.tar.gz"
                _write_tar_gzip(program.root / "payload", layer_path)
                layer = _blob_descriptor_path(
                    layer_path,
                    PROGRAM_LAYER_MEDIA_TYPE,
                    annotations={"org.lean-runtime.layer.kind": "program-payload"},
                )
                config_path = staging / "config.json"
                config_path.write_bytes(canonical_json_bytes(program.description.to_dict()))
                config = _blob_descriptor_path(config_path, PROGRAM_CONFIG_MEDIA_TYPE)
                manifest_path = staging / "manifest.json"
                manifest_path.write_bytes(
                    canonical_json_bytes(
                        {
                            "schemaVersion": 2,
                            "mediaType": MANIFEST_MEDIA_TYPE,
                            "config": config,
                            "layers": [layer],
                            "annotations": {
                                "org.lean-runtime.artifact.kind": "execution-program",
                                "org.lean-runtime.program-id": program.id,
                                "org.opencontainers.image.revision": (
                                    program.description.source_revision
                                ),
                            },
                        }
                    )
                )
                compatibility = platform_compatibility()
                descriptor = _blob_descriptor_path(
                    manifest_path,
                    MANIFEST_MEDIA_TYPE,
                    annotations={
                        "org.lean-runtime.artifact.kind": "execution-program",
                        "org.lean-runtime.platform.schema": compatibility["schema"],
                        "org.lean-runtime.platform.abi": compatibility["abi"],
                    },
                    platform={
                        "os": compatibility["system"],
                        "architecture": {"x86_64": "amd64", "arm64": "arm64"}.get(
                            compatibility["machine"], compatibility["machine"]
                        ),
                    },
                )
                entries = {
                    "blobs/sha256/" + str(layer["digest"]).removeprefix("sha256:"): layer_path,
                    "blobs/sha256/" + str(config["digest"]).removeprefix("sha256:"): config_path,
                    "blobs/sha256/"
                    + str(descriptor["digest"]).removeprefix("sha256:"): manifest_path,
                }
                index_path = staging / "index.json"
                index_path.write_bytes(
                    canonical_json_bytes(
                        {
                            "schemaVersion": 2,
                            "mediaType": INDEX_MEDIA_TYPE,
                            "manifests": [descriptor],
                        }
                    )
                )
                layout_path = staging / "oci-layout"
                layout_path.write_bytes(b'{"imageLayoutVersion":"1.0.0"}')
                entries["index.json"] = index_path
                entries["oci-layout"] = layout_path
                from .bundles import _write_oci_archive

                _write_oci_archive(entries, temporary)
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
        return ProgramInfo(
            program.id,
            program.description.source_revision,
            str(descriptor["digest"]),
            str(output),
            descriptor,
        )

    def import_bundle(self, bundle: Path) -> ReadyProgram:
        from .bundles import EnvironmentBundles

        with tempfile.TemporaryDirectory(prefix="lean-runtime-program-open-copy-") as directory:
            entries = EnvironmentBundles._extract_oci_archive(bundle, Path(directory))
            index = entries.get("index.json")
            if index is None:
                raise EnvironmentError("program OCI bundle has no index")
            return self.import_layout(
                _json_object(index.read_bytes(), "program index"),
                entries,
                origin={"kind": "bundle", "bundle": str(bundle)},
            )

    def import_layout(
        self,
        index: dict[str, Any],
        entries: dict[str, Path],
        *,
        origin: dict[str, Any],
    ) -> ReadyProgram:
        manifests = index.get("manifests")
        if (
            not isinstance(manifests, list)
            or len(manifests) != 1
            or not isinstance(manifests[0], dict)
        ):
            raise EnvironmentError("program index must contain exactly one platform manifest")
        descriptor = manifests[0]
        _require_media_type(descriptor, MANIFEST_MEDIA_TYPE, "program manifest")
        manifest_path = _descriptor_blob_path(entries, descriptor, "program manifest")
        oci_manifest = _json_object(manifest_path.read_bytes(), "program OCI manifest")
        annotations = oci_manifest.get("annotations")
        if (
            not isinstance(annotations, dict)
            or annotations.get("org.lean-runtime.artifact.kind") != "execution-program"
        ):
            raise EnvironmentError("downloaded item is not a Lean Runtime ready-to-run program")
        config_descriptor = oci_manifest.get("config")
        layers = oci_manifest.get("layers")
        if (
            not isinstance(config_descriptor, dict)
            or not isinstance(layers, list)
            or len(layers) != 1
        ):
            raise EnvironmentError("program OCI manifest is incomplete")
        _require_media_type(config_descriptor, PROGRAM_CONFIG_MEDIA_TYPE, "program config")
        _require_media_type(layers[0], PROGRAM_LAYER_MEDIA_TYPE, "program layer")
        config_path = _descriptor_blob_path(entries, config_descriptor, "program config")
        manifest = ProgramDescription.from_dict(
            _json_object(config_path.read_bytes(), "program config")
        )
        if manifest.computer_compatibility != platform_compatibility():
            raise EnvironmentError("ready-to-run program is not compatible with this computer")
        destination = self.store.programs / manifest.program_id
        with FileLock(self.store.lock_dir / f"{manifest.program_id}.lock", timeout=1800):
            if not destination.exists():
                stage = self.store.programs / f".staging-{uuid.uuid4().hex}"
                try:
                    _extract_layer(
                        _descriptor_blob_path(entries, layers[0], "program payload"),
                        stage / "payload",
                    )
                    if _file_inventory(stage / "payload") != manifest.files:
                        raise EnvironmentError("program payload digest mismatch")
                    write_json_atomic(stage / "program.json", manifest.to_dict())
                    write_json_atomic(
                        stage / "origin.json",
                        {**origin, "copy_id": descriptor["digest"]},
                    )
                    stage.replace(destination)
                finally:
                    if stage.exists():
                        shutil.rmtree(stage)
        return self.open(manifest.program_id)


def _platform_matches(descriptor: Mapping[str, Any]) -> bool:
    platform = descriptor.get("platform")
    annotations = descriptor.get("annotations")
    compatibility = platform_compatibility()
    architecture = {"x86_64": "amd64", "arm64": "arm64"}.get(
        compatibility["machine"], compatibility["machine"]
    )
    return (
        isinstance(platform, dict)
        and isinstance(annotations, dict)
        and platform.get("os") == compatibility["system"]
        and platform.get("architecture") == architecture
        and annotations.get("org.lean-runtime.platform.abi") == compatibility["abi"]
        and annotations.get("org.lean-runtime.artifact.kind") == "execution-program"
    )


class ProgramLibrary:
    def __init__(
        self,
        repository: OCIRepository,
        store: EnvironmentStore,
        programs: ProgramManager,
        events: EventEmitter,
        verifier: SignatureVerifier | None = None,
    ) -> None:
        self.repository = repository
        self.store = store
        self.programs = programs
        self.events = events
        self.verifier = verifier
        self.client = OCIRegistryClient(repository)

    def pull(
        self,
        reference: str,
        *,
        expected_source_revision: str | None = None,
    ) -> ReadyProgram:
        response = self.client.manifest(reference)
        if self.verifier is not None:
            self.verifier.verify(self.repository, response.digest)
        document = _json_object(response.data, "program registry manifest")
        if response.media_type == INDEX_MEDIA_TYPE or document.get("mediaType") == INDEX_MEDIA_TYPE:
            candidates = [
                item
                for item in document.get("manifests", [])
                if isinstance(item, dict) and _platform_matches(item)
            ]
            if not candidates:
                raise DownloadUnavailable("program index has no compatible platform manifest")
            descriptor = candidates[0]
            selected = self.client.manifest(str(descriptor["digest"]))
            if selected.digest != descriptor["digest"] or len(selected.data) != descriptor["size"]:
                raise EnvironmentError("program platform manifest descriptor mismatch")
            manifest_data = selected.data
            oci_manifest = _json_object(selected.data, "program platform manifest")
        elif response.media_type == MANIFEST_MEDIA_TYPE:
            compatibility = platform_compatibility()
            descriptor = {
                "mediaType": MANIFEST_MEDIA_TYPE,
                "digest": response.digest,
                "size": len(response.data),
                "annotations": {
                    "org.lean-runtime.artifact.kind": "execution-program",
                    "org.lean-runtime.platform.abi": compatibility["abi"],
                },
                "platform": {
                    "os": compatibility["system"],
                    "architecture": {"x86_64": "amd64", "arm64": "arm64"}.get(
                        compatibility["machine"], compatibility["machine"]
                    ),
                },
            }
            manifest_data = response.data
            oci_manifest = document
        else:
            raise EnvironmentError("registry returned an unsupported program media type")
        descriptors = [oci_manifest.get("config"), *oci_manifest.get("layers", [])]
        if not all(isinstance(item, dict) for item in descriptors):
            raise EnvironmentError("program platform manifest is incomplete")
        entries: dict[str, Path] = {}
        manifest_path = self.store.oci_blobs / str(descriptor["digest"]).removeprefix("sha256:")
        if not manifest_path.exists():
            manifest_path.write_bytes(manifest_data)
        entries["blobs/sha256/" + str(descriptor["digest"]).removeprefix("sha256:")] = manifest_path
        for item in descriptors:
            assert isinstance(item, dict)
            path = self.client.download_blob(item, self.store, self.events)
            entries["blobs/sha256/" + str(item["digest"]).removeprefix("sha256:")] = path
        program = self.programs.import_layout(
            {"schemaVersion": 2, "mediaType": INDEX_MEDIA_TYPE, "manifests": [descriptor]},
            entries,
            origin={"kind": "oci", "registry": self.repository.display, "reference": reference},
        )
        if (
            expected_source_revision is not None
            and program.description.source_revision != expected_source_revision
        ):
            raise EnvironmentError("program source revision does not match the requested revision")
        return program

    def publish(
        self,
        program_id: str,
        *,
        tags: Sequence[str] = (),
    ) -> ProgramInfo:
        with tempfile.TemporaryDirectory(prefix="lean-runtime-program-publish-") as directory:
            root = Path(directory)
            archive = root / "program.oci.tar.gz"
            info = self.programs.export(program_id, archive)
            from .bundles import EnvironmentBundles

            entries = EnvironmentBundles._extract_oci_archive(archive, root / "layout")
            index = _json_object(entries["index.json"].read_bytes(), "program index")
            descriptor = index["manifests"][0]
            manifest_path = entries[
                "blobs/sha256/" + str(descriptor["digest"]).removeprefix("sha256:")
            ]
            manifest_data = manifest_path.read_bytes()
            manifest = _json_object(manifest_data, "program manifest")
            for item in [manifest["config"], *manifest["layers"]]:
                blob = entries["blobs/sha256/" + str(item["digest"]).removeprefix("sha256:")]
                self.client.upload_blob(blob, str(item["digest"]))
            digest = self.client.publish_manifest(
                str(descriptor["digest"]), manifest_data, MANIFEST_MEDIA_TYPE
            )
            if digest != descriptor["digest"]:
                raise EnvironmentError("published program manifest digest changed")
            if tags:
                index_data = canonical_json_bytes(index)
                for tag in tags:
                    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
                        raise ValueError(f"invalid OCI tag: {tag!r}")
                    self.client.publish_manifest(tag, index_data, INDEX_MEDIA_TYPE)
            return ProgramInfo(
                info.program_id,
                info.source_revision,
                digest,
                self.repository.display,
                descriptor,
            )

    def publish_index(
        self,
        source_revision: str,
        descriptors: Sequence[dict[str, Any]],
        *,
        tags: Sequence[str] = (),
    ) -> str:
        if not re.fullmatch(r"[0-9a-f]{40,64}", source_revision):
            raise ValueError("program index requires an exact source revision")
        if not descriptors:
            raise ValueError("program index requires at least one platform descriptor")
        platforms: set[tuple[str, str, str]] = set()
        for descriptor in descriptors:
            if descriptor.get("mediaType") != MANIFEST_MEDIA_TYPE:
                raise ValueError("program platform descriptor has an unsupported media type")
            if not self.client.manifest_exists(str(descriptor.get("digest", ""))):
                raise EnvironmentError("program platform manifest has not been published")
            platform = descriptor.get("platform")
            annotations = descriptor.get("annotations")
            if not isinstance(platform, dict) or not isinstance(annotations, dict):
                raise ValueError("program platform descriptor is incomplete")
            if annotations.get("org.lean-runtime.artifact.kind") != "execution-program":
                raise ValueError("computer record does not identify a ready-to-run program")
            key = (
                str(platform.get("os")),
                str(platform.get("architecture")),
                str(annotations.get("org.lean-runtime.platform.abi")),
            )
            if key in platforms:
                raise ValueError(f"duplicate program platform: {'/'.join(key)}")
            platforms.add(key)
        ordered = sorted(
            descriptors,
            key=lambda item: (
                str(item["platform"]["os"]),
                str(item["platform"]["architecture"]),
                str(item["annotations"]["org.lean-runtime.platform.abi"]),
            ),
        )
        index = canonical_json_bytes(
            {
                "schemaVersion": 2,
                "mediaType": INDEX_MEDIA_TYPE,
                "manifests": ordered,
                "annotations": {
                    "org.lean-runtime.artifact.kind": "execution-program-index",
                    "org.opencontainers.image.revision": source_revision,
                },
            }
        )
        digest = self.client.publish_manifest(source_revision, index, INDEX_MEDIA_TYPE)
        for tag in tags:
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
                raise ValueError(f"invalid OCI tag: {tag!r}")
            self.client.publish_manifest(tag, index, INDEX_MEDIA_TYPE)
        return digest
