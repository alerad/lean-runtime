"""Published check-only Lean toolchains over OCI Distribution."""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import zstandard

from .errors import DownloadUnavailable, EnvironmentError, ToolchainError
from .events import EventEmitter
from .locking import FileLock
from .oci import (
    OCIRegistryClient,
    OCIRepository,
    SignatureVerifier,
)
from .oci_protocol import (
    INDEX_MEDIA_TYPE,
    MANIFEST_MEDIA_TYPE,
)
from .oci_protocol import (
    blob_descriptor_path as _descriptor,
)
from .oci_protocol import (
    digest_bytes as _digest,
)
from .oci_protocol import (
    digest_path as _digest_path,
)
from .oci_protocol import (
    json_object as _parse_json_object,
)
from .oci_protocol import (
    platform_matches as _platform_matches,
)
from .serialization import canonical_json_bytes
from .store import EnvironmentStore, platform_compatibility
from .toolchain_slim import SlimManifest, materialize, verify_capabilities
from .toolchains import ToolchainManager, normalize_toolchain

TOOLCHAIN_CONFIG_SCHEMA = "lean-runtime-check-toolchain/1"
TOOLCHAIN_CONFIG_MEDIA_TYPE = "application/vnd.lean-runtime.toolchain.config.v1+json"
TOOLCHAIN_LAYER_MEDIA_TYPE = "application/vnd.lean-runtime.toolchain.layer.v1.tar+zstd"
MAX_TOOLCHAIN_FILES = 100_000
MAX_TOOLCHAIN_BYTES = 8 * 1024**3


def toolchain_reference(toolchain: str) -> str:
    """Return the platform-independent tag for a check-toolchain index."""
    identity = canonical_json_bytes(
        {
            "toolchain": normalize_toolchain(toolchain),
            "profile": "check",
        }
    )
    return "toolchain-" + hashlib.sha256(identity).hexdigest()


def _json_object(data: bytes, label: str) -> dict[str, Any]:
    return _parse_json_object(data, label, subject="OCI")


def _write_layer(root: Path, output: Path) -> None:
    compressor = zstandard.ZstdCompressor(level=10, write_checksum=True, threads=0)
    with (
        output.open("wb") as raw,
        compressor.stream_writer(raw, closefd=False) as compressed,
        tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive,
    ):
        for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
            relative = path.relative_to(root).as_posix()
            stat = path.lstat()
            info = tarfile.TarInfo(relative + ("/" if path.is_dir() else ""))
            info.mode = stat.st_mode & 0o777
            info.mtime = info.uid = info.gid = 0
            info.uname = info.gname = ""
            if path.is_dir():
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif path.is_symlink():
                linkname = os.readlink(path)
                resolved = posixpath.normpath(
                    posixpath.join(PurePosixPath(relative).parent, linkname)
                )
                if (
                    PurePosixPath(linkname).is_absolute()
                    or resolved == ".."
                    or resolved.startswith("../")
                ):
                    raise ToolchainError(f"check toolchain contains unsafe symlink: {path}")
                info.type = tarfile.SYMTYPE
                info.linkname = linkname
                info.size = 0
                archive.addfile(info)
            elif path.is_file() and not path.is_symlink():
                info.size = stat.st_size
                with path.open("rb") as source:
                    archive.addfile(info, source)
            else:
                raise ToolchainError(f"check toolchain contains unsupported entry: {path}")


def _safe_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name or "\0" in name:
        raise ToolchainError(f"unsafe check toolchain member: {name!r}")
    return path


def _extract_layer(layer: Path, destination: Path) -> None:
    total = count = 0
    try:
        with (
            layer.open("rb") as raw,
            zstandard.ZstdDecompressor().stream_reader(raw) as decoded,
            tarfile.open(fileobj=decoded, mode="r|") as archive,
        ):
            for member in archive:
                count += 1
                total += member.size
                if count > MAX_TOOLCHAIN_FILES or total > MAX_TOOLCHAIN_BYTES:
                    raise ToolchainError("check toolchain exceeds extraction limits")
                relative = _safe_member(member.name)
                target = destination.joinpath(*relative.parts)
                for parent in target.parents:
                    if parent == destination:
                        break
                    if parent.is_symlink():
                        raise ToolchainError(
                            "check toolchain member traverses an extracted symlink"
                        )
                else:
                    raise ToolchainError("check toolchain member escapes its destination")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    target.chmod(member.mode & 0o777)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise ToolchainError("check toolchain file has no payload")
                    with target.open("wb") as output:
                        shutil.copyfileobj(source, output)
                    target.chmod(member.mode & 0o777)
                elif member.issym():
                    linkname = member.linkname
                    resolved = posixpath.normpath(
                        posixpath.join(PurePosixPath(member.name).parent, linkname)
                    )
                    if (
                        not linkname
                        or PurePosixPath(linkname).is_absolute()
                        or resolved == ".."
                        or resolved.startswith("../")
                    ):
                        raise ToolchainError("check toolchain contains an unsafe symlink")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(linkname)
                else:
                    raise ToolchainError(
                        "check toolchain may contain only files, directories, and safe symlinks"
                    )
    except (tarfile.TarError, zstandard.ZstdError, OSError) as exc:
        raise ToolchainError("could not extract published check toolchain") from exc


@dataclass(frozen=True, slots=True)
class ToolchainPlan:
    toolchain: str
    reference: str
    descriptor: dict[str, Any]
    config_descriptor: dict[str, Any]
    config_data: bytes
    total_bytes: int
    cached_bytes: int
    lean_commit: str
    slim_manifest: dict[str, Any]

    @property
    def download_bytes(self) -> int:
        return self.total_bytes - self.cached_bytes


class OCIToolchainLibrary:
    def __init__(
        self,
        repository: OCIRepository,
        store: EnvironmentStore,
        toolchains: ToolchainManager,
        events: EventEmitter,
        verifier: SignatureVerifier | None = None,
    ) -> None:
        self.repository = repository
        self.store = store
        self.toolchains = toolchains
        self.events = events
        self.verifier = verifier
        self.client = OCIRegistryClient(repository)

    def _manifest(
        self, toolchain: str
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], bytes, bool]:
        response = self.client.manifest(toolchain_reference(toolchain))
        if self.verifier is not None:
            self.verifier.verify(self.repository, response.digest)
        document = _json_object(response.data, "toolchain index")
        if response.media_type == INDEX_MEDIA_TYPE or document.get("mediaType") == INDEX_MEDIA_TYPE:
            manifests = document.get("manifests")
            candidates = (
                [item for item in manifests if isinstance(item, dict) and _platform_matches(item)]
                if isinstance(manifests, list)
                else []
            )
            if not candidates:
                raise DownloadUnavailable("toolchain index has no compatible platform")
            descriptor = candidates[0]
            selected = self.client.manifest(str(descriptor.get("digest")))
            if selected.digest != descriptor.get("digest") or len(selected.data) != descriptor.get(
                "size"
            ):
                raise EnvironmentError("toolchain platform manifest descriptor mismatch")
            manifest = _json_object(selected.data, "toolchain manifest")
        elif response.media_type == MANIFEST_MEDIA_TYPE:
            manifest = document
        else:
            raise DownloadUnavailable("registry reference is not a check toolchain")
        config_descriptor = manifest.get("config")
        layers = manifest.get("layers")
        if (
            not isinstance(config_descriptor, dict)
            or config_descriptor.get("mediaType") != TOOLCHAIN_CONFIG_MEDIA_TYPE
            or not isinstance(layers, list)
            or len(layers) != 1
            or not isinstance(layers[0], dict)
            or layers[0].get("mediaType") != TOOLCHAIN_LAYER_MEDIA_TYPE
        ):
            raise DownloadUnavailable("registry reference is not a compatible check toolchain")
        config_digest = str(config_descriptor.get("digest", ""))
        config_size = config_descriptor.get("size")
        match = re.fullmatch(r"sha256:([0-9a-f]{64})", config_digest)
        cached_path = self.store.oci_blobs / match.group(1) if match is not None else None
        config_cached = (
            cached_path is not None
            and cached_path.is_file()
            and isinstance(config_size, int)
            and cached_path.stat().st_size == config_size
            and _digest_path(cached_path) == config_digest
        )
        config_data = (
            cached_path.read_bytes()
            if config_cached and cached_path is not None
            else self.client.read_blob(config_descriptor)
        )
        config = _json_object(config_data, "toolchain config")
        if (
            config.get("schema") != TOOLCHAIN_CONFIG_SCHEMA
            or config.get("toolchain") != normalize_toolchain(toolchain)
            or config.get("platform_compatibility") != platform_compatibility()
            or not isinstance(config.get("lean_commit"), str)
            or not config.get("lean_commit")
            or not isinstance(config.get("slim_manifest"), dict)
        ):
            raise EnvironmentError("published check toolchain identity mismatch")
        return manifest, config, config_descriptor, config_data, config_cached

    def plan(self, toolchain: str) -> ToolchainPlan:
        local_check = getattr(self.toolchains, "is_available_locally", None)
        if callable(local_check) and local_check(toolchain):
            return ToolchainPlan(
                normalize_toolchain(toolchain),
                toolchain_reference(toolchain),
                {},
                {},
                b"",
                0,
                0,
                "local",
                {},
            )
        manifest, config, config_descriptor, config_data, config_cached = self._manifest(toolchain)
        descriptor = manifest["layers"][0]
        size = descriptor.get("size")
        digest = str(descriptor.get("digest", "")).removeprefix("sha256:")
        if not isinstance(size, int) or size < 0 or len(digest) != 64:
            raise EnvironmentError("published check toolchain descriptor is invalid")
        cached = self.store.oci_blobs / digest
        layer_cached = (
            size
            if cached.is_file()
            and cached.stat().st_size == size
            and _digest_path(cached) == descriptor.get("digest")
            else 0
        )
        config_size = int(config_descriptor["size"])
        return ToolchainPlan(
            normalize_toolchain(toolchain),
            toolchain_reference(toolchain),
            descriptor,
            config_descriptor,
            config_data,
            size + config_size,
            layer_cached + (config_size if config_cached else 0),
            str(config.get("lean_commit", "")),
            dict(config["slim_manifest"]),
        )

    def pull(
        self,
        toolchain: str,
        *,
        cancel: threading.Event | None = None,
    ) -> bool:
        local_check = getattr(self.toolchains, "is_available_locally", None)
        if callable(local_check) and local_check(toolchain):
            return True
        plan = self.plan(toolchain)
        self.client.cache_verified_blob(plan.config_data, plan.config_descriptor, self.store)
        try:
            layer = self.client.download_blob(
                plan.descriptor,
                self.store,
                self.events,
                cancel=cancel,
            )
        except EnvironmentError as exc:
            if cancel is not None and cancel.is_set():
                raise ToolchainError(
                    f"Lean toolchain download was cancelled: {normalize_toolchain(toolchain)!r}"
                ) from exc
            raise
        destination = self.toolchains.slim_path(toolchain)
        with FileLock(
            self.store.lock_dir / f"toolchain-{toolchain_reference(toolchain)}.lock",
            timeout=1800,
            cancel=cancel,
        ):
            if self.toolchains.is_available_locally(toolchain):
                return True
            staging = destination.parent / f".{destination.name}.staging-{os.getpid()}"
            if staging.exists():
                shutil.rmtree(staging)
            staging.mkdir(parents=True)
            try:
                _extract_layer(layer, staging)
                manifest = SlimManifest.load(staging)
                if manifest is None or manifest.toolchain != normalize_toolchain(toolchain):
                    raise ToolchainError("published check toolchain has an invalid slim manifest")
                if manifest.to_dict() != plan.slim_manifest:
                    raise ToolchainError(
                        "published check toolchain manifest does not match its OCI config"
                    )
                failures = [
                    (name, detail)
                    for name, ok, detail in verify_capabilities(
                        staging, environment=self.toolchains.environment
                    )
                    if not ok
                ]
                if failures:
                    raise ToolchainError(
                        f"published check toolchain failed verification: {failures}"
                    )
                observed_commit = subprocess.check_output(
                    [str(staging / "bin" / "lean"), "-g"], text=True
                ).strip()
                if observed_commit != plan.lean_commit:
                    raise ToolchainError("published check toolchain Lean commit mismatch")
                destination.parent.mkdir(parents=True, exist_ok=True)
                staging.replace(destination)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        return True


@dataclass(frozen=True, slots=True)
class ToolchainPublication:
    toolchain: str
    manifest_digest: str
    descriptor: dict[str, Any]
    bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "toolchain": self.toolchain,
            "manifest_digest": self.manifest_digest,
            "descriptor": self.descriptor,
            "bytes": self.bytes,
        }


class OCIToolchainPublisher:
    def __init__(self, repository: OCIRepository, toolchains: ToolchainManager) -> None:
        self.repository = repository
        self.toolchains = toolchains
        self.client = OCIRegistryClient(repository)

    def publish(self, toolchain: str) -> ToolchainPublication:
        name = self.toolchains.ensure(toolchain)
        source = self.toolchains._elan_toolchain_dir(name)
        with tempfile.TemporaryDirectory(prefix="lean-runtime-toolchain-") as raw:
            root = Path(raw)
            slim = root / "slim"
            if source.is_dir():
                materialize(source, slim, toolchain=name, created_at="1970-01-01T00:00:00Z")
            else:
                installed_slim = self.toolchains.slim_path(name)
                if not self.toolchains.has_slim(name):
                    raise ToolchainError(f"toolchain {name!r} is not available for publication")
                shutil.copytree(installed_slim, slim, symlinks=True)
            failures = [
                row
                for row in verify_capabilities(slim, environment=self.toolchains.environment)
                if not row[1]
            ]
            if failures:
                raise ToolchainError(f"check toolchain publication corpus failed: {failures}")
            lean_commit = subprocess.check_output(
                [str(slim / "bin" / "lean"), "-g"], text=True
            ).strip()
            if not lean_commit:
                raise ToolchainError("could not identify the check toolchain Lean commit")
            layer = root / "toolchain.tar.zst"
            _write_layer(slim, layer)
            layer_descriptor = _descriptor(layer, TOOLCHAIN_LAYER_MEDIA_TYPE)
            config_data = canonical_json_bytes(
                {
                    "schema": TOOLCHAIN_CONFIG_SCHEMA,
                    "toolchain": name,
                    "platform_compatibility": platform_compatibility(),
                    "lean_commit": lean_commit,
                    "slim_manifest": SlimManifest.load(slim).to_dict(),  # type: ignore[union-attr]
                }
            )
            config = root / "config.json"
            config.write_bytes(config_data)
            config_descriptor = _descriptor(config, TOOLCHAIN_CONFIG_MEDIA_TYPE)
            for path, descriptor in ((layer, layer_descriptor), (config, config_descriptor)):
                self.client.upload_blob(path, str(descriptor["digest"]))
            compatibility = platform_compatibility()
            manifest_data = canonical_json_bytes(
                {
                    "schemaVersion": 2,
                    "mediaType": MANIFEST_MEDIA_TYPE,
                    "config": config_descriptor,
                    "layers": [layer_descriptor],
                }
            )
            digest = _digest(manifest_data)
            published = self.client.publish_manifest(digest, manifest_data, MANIFEST_MEDIA_TYPE)
            descriptor = {
                "mediaType": MANIFEST_MEDIA_TYPE,
                "digest": published,
                "size": len(manifest_data),
                "annotations": {
                    "org.lean-runtime.platform.schema": compatibility["schema"],
                    "org.lean-runtime.platform.abi": compatibility["abi"],
                },
                "platform": {
                    "os": compatibility["system"],
                    "architecture": {"x86_64": "amd64", "arm64": "arm64"}.get(
                        compatibility["machine"], compatibility["machine"]
                    ),
                },
            }
            return ToolchainPublication(name, published, descriptor, layer.stat().st_size)

    def publish_index(self, toolchain: str, descriptors: list[dict[str, Any]]) -> str:
        """Atomically publish a multi-platform check-toolchain index."""
        if not descriptors:
            raise ValueError("a toolchain index requires platform manifests")
        platforms: set[tuple[str, str, str]] = set()
        for descriptor in descriptors:
            platform = descriptor.get("platform")
            annotations = descriptor.get("annotations")
            if (
                descriptor.get("mediaType") != MANIFEST_MEDIA_TYPE
                or not isinstance(platform, dict)
                or not isinstance(annotations, dict)
            ):
                raise ValueError("toolchain platform descriptor is incomplete")
            key = (
                str(platform.get("os")),
                str(platform.get("architecture")),
                str(annotations.get("org.lean-runtime.platform.abi")),
            )
            if key in platforms:
                raise ValueError(f"duplicate toolchain platform: {'/'.join(key)}")
            platforms.add(key)
            if not self.client.manifest_exists(str(descriptor.get("digest", ""))):
                raise EnvironmentError("toolchain platform manifest is not published")
        data = canonical_json_bytes(
            {
                "schemaVersion": 2,
                "mediaType": INDEX_MEDIA_TYPE,
                "manifests": sorted(
                    descriptors,
                    key=lambda item: (
                        str(item["platform"]["os"]),
                        str(item["platform"]["architecture"]),
                        str(item["digest"]),
                    ),
                ),
            }
        )
        return self.client.publish_manifest(toolchain_reference(toolchain), data, INDEX_MEDIA_TYPE)
