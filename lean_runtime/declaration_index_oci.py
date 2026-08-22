"""OCI transport for composable, platform-neutral declaration shards."""

from __future__ import annotations

import hashlib
import re
import tempfile
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import zstandard

from .declaration_index import (
    DECLARATION_INDEX_SCHEMA,
    DeclarationIndex,
    DeclarationIndexSet,
    DeclarationShard,
)
from .errors import DownloadLimitExceeded, DownloadUnavailable, EnvironmentError
from .events import EventEmitter
from .oci import OCIRegistryClient, OCIRepository, RegistryCredential
from .oci_protocol import MANIFEST_MEDIA_TYPE, blob_descriptor_path, digest_path, json_object
from .policies import format_byte_size
from .serialization import canonical_json_bytes
from .store import EnvironmentStore

DECLARATION_INDEX_CONFIG_MEDIA_TYPE = (
    "application/vnd.lean-runtime.declaration-index.config.v2+json"
)
DECLARATION_INDEX_LAYER_MEDIA_TYPE = (
    "application/vnd.lean-runtime.declaration-shard.sqlite.v1+zstd"
)
MAX_DECLARATION_SHARD_BYTES = 256 * 1024 * 1024
MAX_DECLARATION_INDEX_CONFIG_BYTES = 4 * 1024 * 1024
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def declaration_index_reference(lock_id: str) -> str:
    if re.fullmatch(r"lock_[0-9a-f]{64}", lock_id) is None:
        raise ValueError(f"invalid lock identity: {lock_id!r}")
    return "declaration-index-" + lock_id


class SignatureVerifier(Protocol):
    def verify(self, repository: OCIRepository, digest: str) -> None: ...


@dataclass(frozen=True, slots=True)
class DeclarationShardSource:
    shard_id: str
    package: str
    source_id: str
    toolchain: str
    subdir: str | None
    module_roots: tuple[str, ...]
    namespace_roots: tuple[str, ...]
    path: Path


@dataclass(frozen=True, slots=True)
class DeclarationIndexPublication:
    reference: str
    manifest_digest: str
    shards: tuple[DeclarationShard, ...]
    uploaded_bytes: int
    reused_bytes: int

    def to_dict(self) -> dict[str, object]:
        return {
            "reference": self.reference,
            "manifest_digest": self.manifest_digest,
            "shards": [item.to_dict() for item in self.shards],
            "uploaded_bytes": self.uploaded_bytes,
            "reused_bytes": self.reused_bytes,
        }


def _selected_shards(
    shards: Sequence[DeclarationShard], requested_names: tuple[str, ...]
) -> tuple[DeclarationShard, ...]:
    """Route qualified names by namespace; bare names require every shard."""

    if not requested_names or any("." not in name for name in requested_names):
        return tuple(shards)
    roots = {name.split(".", 1)[0] for name in requested_names}
    selected = tuple(
        shard for shard in shards if roots.intersection(shard.namespace_roots)
    )
    # A conservative fallback keeps hints correct for unusual generated names.
    return selected or tuple(shards)


def retained_declaration_index_set(
    store: EnvironmentStore,
    lock_id: str,
    requested_names: tuple[str, ...],
    *,
    require_complete: bool = False,
) -> DeclarationIndexSet | None:
    records = store.declaration_index_shards(lock_id)
    if not records:
        return None
    available_shards = tuple(item for item, _path in records)
    wanted_ids = {
        item.shard_id for item in _selected_shards(available_shards, requested_names)
    }
    indexes: list[tuple[DeclarationShard, DeclarationIndex]] = []
    for shard, path in records:
        if shard.shard_id not in wanted_ids:
            continue
        if path is None:
            if require_complete:
                return None
            continue
        indexes.append((shard, DeclarationIndex(path, expected_shard_id=shard.shard_id)))
    return DeclarationIndexSet(lock_id, tuple(indexes)) if indexes else None


class OCIDeclarationIndexPublisher:
    """Publish an exact environment manifest over reusable declaration shards."""

    def __init__(
        self,
        repository: OCIRepository,
        *,
        credential: RegistryCredential | None = None,
    ) -> None:
        self.repository = repository
        selected = credential or RegistryCredential.discover(repository)
        self.client = OCIRegistryClient(
            repository,
            username=selected.username,
            password=selected.password,
        )

    def publish(
        self, sources: Sequence[DeclarationShardSource], *, lock_id: str
    ) -> DeclarationIndexPublication:
        if not sources or len({item.shard_id for item in sources}) != len(sources):
            raise EnvironmentError("declaration index sources are empty or duplicated")
        uploaded_bytes = 0
        reused_bytes = 0
        with tempfile.TemporaryDirectory(prefix="lean-runtime-declaration-index-") as temporary:
            root = Path(temporary)
            shards: list[DeclarationShard] = []
            layers: list[dict[str, Any]] = []
            compressed_paths: list[Path] = []
            for position, source in enumerate(sources):
                selected_path = source.path.expanduser().resolve()
                DeclarationIndex(selected_path, expected_shard_id=source.shard_id)
                sqlite_digest = digest_path(selected_path)
                compressed = root / f"{position:04d}-{source.shard_id}.sqlite.zst"
                with (
                    selected_path.open("rb") as raw,
                    compressed.open("wb") as output,
                    zstandard.ZstdCompressor(
                        level=10, write_checksum=True, threads=0
                    ).stream_writer(output, closefd=False) as encoder,
                ):
                    while chunk := raw.read(1024 * 1024):
                        encoder.write(chunk)
                layer = blob_descriptor_path(
                    compressed,
                    DECLARATION_INDEX_LAYER_MEDIA_TYPE,
                    annotations={"org.lean-runtime.declaration-shard-id": source.shard_id},
                )
                shard = DeclarationShard(
                    source.shard_id,
                    source.package,
                    source.source_id,
                    source.toolchain,
                    source.subdir,
                    source.module_roots,
                    source.namespace_roots,
                    sqlite_digest,
                    selected_path.stat().st_size,
                    str(layer["digest"]),
                    compressed.stat().st_size,
                )
                shards.append(shard)
                layers.append(layer)
                compressed_paths.append(compressed)
            config = root / "config.json"
            config.write_bytes(
                canonical_json_bytes(
                    {
                        "schema": DECLARATION_INDEX_SCHEMA,
                        "lock_id": lock_id,
                        "shards": [item.to_dict() for item in shards],
                    }
                )
            )
            config_descriptor = blob_descriptor_path(
                config, DECLARATION_INDEX_CONFIG_MEDIA_TYPE
            )
            for path, descriptor in zip(
                (config, *compressed_paths), (config_descriptor, *layers), strict=True
            ):
                digest = str(descriptor["digest"])
                existed = self.client.blob_exists(digest)
                self.client.upload_blob(path, digest)
                if existed:
                    reused_bytes += path.stat().st_size
                else:
                    uploaded_bytes += path.stat().st_size
            manifest = canonical_json_bytes(
                {
                    "schemaVersion": 2,
                    "mediaType": MANIFEST_MEDIA_TYPE,
                    "config": config_descriptor,
                    "layers": layers,
                    "annotations": {
                        "org.lean-runtime.artifact.kind": "declaration-index",
                        "org.lean-runtime.lock-id": lock_id,
                    },
                }
            )
            reference = declaration_index_reference(lock_id)
            digest = self.client.publish_manifest(reference, manifest, MANIFEST_MEDIA_TYPE)
            return DeclarationIndexPublication(
                reference,
                digest,
                tuple(shards),
                uploaded_bytes,
                reused_bytes,
            )


class OCIDeclarationIndexLibrary:
    def __init__(
        self,
        repository: OCIRepository,
        store: EnvironmentStore,
        events: EventEmitter,
        verifier: SignatureVerifier | None = None,
        *,
        max_download_bytes: int | None = None,
    ) -> None:
        self.repository = repository
        self.store = store
        self.events = events
        self.verifier = verifier
        self.max_download_bytes = max_download_bytes
        self.client = OCIRegistryClient(repository)

    @staticmethod
    def _descriptor(value: object, media_type: str, label: str) -> dict[str, Any]:
        if not isinstance(value, dict) or value.get("mediaType") != media_type:
            raise EnvironmentError(f"declaration index has an invalid {label} descriptor")
        digest = value.get("digest")
        size = value.get("size")
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise EnvironmentError(f"declaration index {label} has an invalid digest")
        if not isinstance(size, int) or size < 0:
            raise EnvironmentError(f"declaration index {label} has an invalid size")
        return value

    def retained(
        self,
        lock_id: str,
        requested_names: tuple[str, ...],
        *,
        require_complete: bool = False,
    ) -> DeclarationIndexSet | None:
        return retained_declaration_index_set(
            self.store,
            lock_id,
            requested_names,
            require_complete=require_complete,
        )

    def acquire(
        self,
        lock_id: str,
        requested_names: tuple[str, ...],
        *,
        cancel: threading.Event | None = None,
    ) -> DeclarationIndexSet:
        retained = self.retained(lock_id, requested_names, require_complete=True)
        if retained is not None:
            return retained
        self.events.emit(
            "declaration_index.lookup",
            "Looking up declaration hints for the rejected environment",
            registry=self.repository.display,
            lock_id=lock_id,
        )
        response = self.client.manifest(declaration_index_reference(lock_id))
        if self.verifier is not None:
            self.verifier.verify(self.repository, response.digest)
            self.events.emit(
                "declaration_index.signature_verified",
                "Verified declaration index publisher signature",
                registry=self.repository.display,
                digest=response.digest,
            )
        manifest = json_object(response.data, "manifest", subject="declaration index")
        if (
            response.media_type != MANIFEST_MEDIA_TYPE
            and manifest.get("mediaType") != MANIFEST_MEDIA_TYPE
        ):
            raise DownloadUnavailable("declaration index has an unsupported OCI manifest")
        config_descriptor = self._descriptor(
            manifest.get("config"), DECLARATION_INDEX_CONFIG_MEDIA_TYPE, "config"
        )
        raw_layers = manifest.get("layers")
        if not isinstance(raw_layers, list) or not raw_layers:
            raise EnvironmentError("declaration index manifest contains no shard layers")
        layers = tuple(
            self._descriptor(item, DECLARATION_INDEX_LAYER_MEDIA_TYPE, "layer")
            for item in raw_layers
        )
        config_data = self.client.read_blob(
            config_descriptor, limit=MAX_DECLARATION_INDEX_CONFIG_BYTES
        )
        config = json_object(config_data, "config", subject="declaration index")
        raw_shards = config.get("shards")
        if (
            config.get("schema") != DECLARATION_INDEX_SCHEMA
            or config.get("lock_id") != lock_id
            or not isinstance(raw_shards, list)
            or not raw_shards
            or not all(isinstance(item, dict) for item in raw_shards)
        ):
            raise EnvironmentError("declaration index config identity is invalid")
        shards = tuple(DeclarationShard.from_dict(item) for item in raw_shards)
        descriptors = {str(item["digest"]): item for item in layers}
        if len(descriptors) != len(shards) or any(
            shard.layer_digest not in descriptors
            or descriptors[shard.layer_digest]["size"] != shard.layer_size
            for shard in shards
        ):
            raise EnvironmentError("declaration shard descriptors do not match the config")
        selected = _selected_shards(shards, requested_names)
        existing = {
            shard.shard_id: path
            for shard, path in self.store.declaration_index_shards(lock_id)
            if path is not None
        }
        missing = tuple(item for item in selected if item.shard_id not in existing)
        download_bytes = sum(item.layer_size for item in missing)
        if self.max_download_bytes is not None and download_bytes > self.max_download_bytes:
            raise DownloadLimitExceeded(
                f"declaration shards download {format_byte_size(download_bytes)}, above the "
                f"configured limit of {format_byte_size(self.max_download_bytes)}"
            )
        sources: dict[str, Path] = {}
        temporaries: list[Path] = []
        try:
            for shard in missing:
                descriptor = descriptors[shard.layer_digest]
                compressed = self.client.download_blob(
                    descriptor, self.store, self.events, cancel=cancel
                )
                with tempfile.NamedTemporaryFile(
                    dir=self.store.declaration_index_objects, delete=False
                ) as output:
                    temporary = Path(output.name)
                    temporaries.append(temporary)
                    digest = hashlib.sha256()
                    written = 0
                    try:
                        with compressed.open(
                            "rb"
                        ) as raw, zstandard.ZstdDecompressor().stream_reader(raw) as decoded:
                            while chunk := decoded.read(1024 * 1024):
                                if cancel is not None and cancel.is_set():
                                    raise EnvironmentError(
                                        "declaration shard acquisition was cancelled"
                                    )
                                written += len(chunk)
                                if (
                                    written > shard.sqlite_size
                                    or written > MAX_DECLARATION_SHARD_BYTES
                                ):
                                    raise EnvironmentError(
                                        "declaration shard exceeds its declared size"
                                    )
                                digest.update(chunk)
                                output.write(chunk)
                    except zstandard.ZstdError as exc:
                        raise EnvironmentError(
                            "declaration shard layer is not valid zstd data"
                        ) from exc
                observed_digest = "sha256:" + digest.hexdigest()
                if written != shard.sqlite_size or observed_digest != shard.sqlite_digest:
                    raise EnvironmentError("declaration shard SQLite digest mismatch")
                DeclarationIndex(temporary, expected_shard_id=shard.shard_id)
                sources[shard.shard_id] = temporary
            self.store.publish_declaration_index_shards(
                lock_id,
                shards,
                sources,
                manifest_digest=response.digest,
                library=self.repository.display,
            )
            temporaries.clear()
        finally:
            for temporary in temporaries:
                temporary.unlink(missing_ok=True)
        retained = self.retained(lock_id, requested_names, require_complete=True)
        if retained is None:
            raise EnvironmentError("declaration shards were not retained after acquisition")
        self.events.emit(
            "declaration_index.retained",
            "Retained declaration shards for offline reuse",
            lock_id=lock_id,
            shards=len(retained.indexes),
            downloaded_bytes=download_bytes,
        )
        return retained
