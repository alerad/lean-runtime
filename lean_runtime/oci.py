"""OCI Distribution pull transport and transparent environment caches."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .bundles import (
    INDEX_MEDIA_TYPE,
    MANIFEST_MEDIA_TYPE,
    EnvironmentBundles,
)
from .errors import DownloadUnavailable, EnvironmentError
from .events import EventEmitter
from .lockfiles import EnvironmentLock
from .locking import FileLock
from .serialization import canonical_json_bytes
from .store import EnvironmentStore, platform_compatibility

_REPOSITORY = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+")
_BEARER_PARAMETER = re.compile(r'([a-zA-Z]+)="([^"]*)"')
_DIGEST = re.compile(r"sha256:([0-9a-f]{64})")
_ACCEPT = ", ".join((INDEX_MEDIA_TYPE, MANIFEST_MEDIA_TYPE))
DEFAULT_ENVIRONMENT_LIBRARIES = ("oci://ghcr.io/alerad/lean-runtime-cache",)
_BLOB_INTEGRITY_ATTEMPTS = 4


class SignatureVerifier(Protocol):
    def verify(self, repository: OCIRepository, digest: str) -> None: ...


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> urllib.request.Request | None:
        redirected = super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )
        if redirected is not None and (
            urllib.parse.urlsplit(request.full_url).netloc != urllib.parse.urlsplit(new_url).netloc
        ):
            redirected.remove_header("Authorization")
        return redirected


@dataclass(frozen=True, slots=True)
class OCIRepository:
    registry: str
    repository: str
    insecure: bool = False

    @classmethod
    def parse(cls, value: str) -> OCIRepository:
        if "://" not in value:
            value = "oci://" + value
        if value.startswith("oci+http://"):
            insecure = True
            raw = value.removeprefix("oci+http://")
        elif value.startswith("oci://"):
            insecure = False
            raw = value.removeprefix("oci://")
        else:
            raise ValueError("environment library must be a host and repository path")
        registry, separator, repository = raw.partition("/")
        if (
            not separator
            or not registry
            or not _REPOSITORY.fullmatch(repository)
            or "@" in registry
        ):
            raise ValueError(f"invalid environment library: {value!r}")
        return cls(registry.lower(), repository.lower(), insecure)

    @property
    def display(self) -> str:
        scheme = "oci+http" if self.insecure else "oci"
        return f"{scheme}://{self.registry}/{self.repository}"


@dataclass(frozen=True, slots=True)
class ManifestResponse:
    data: bytes
    media_type: str
    digest: str


class OCIRegistryClient:
    def __init__(
        self,
        repository: OCIRepository,
        *,
        timeout: float = 30,
        user_agent: str = "lean-runtime/0.6",
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self.repository = repository
        self.timeout = timeout
        self.user_agent = user_agent
        self.username = username or os.environ.get("LEAN_RUNTIME_REGISTRY_USERNAME")
        self.password = password or os.environ.get("LEAN_RUNTIME_REGISTRY_PASSWORD")
        self._token: str | None = None
        self._opener = urllib.request.build_opener(_SafeRedirectHandler())

    @property
    def base_url(self) -> str:
        scheme = "http" if self.repository.insecure else "https"
        return f"{scheme}://{self.repository.registry}"

    def _url(self, suffix: str) -> str:
        repository = urllib.parse.quote(self.repository.repository, safe="/")
        return f"{self.base_url}/v2/{repository}/{suffix}"

    def _request(self, request: urllib.request.Request) -> Any:
        request.add_header("User-Agent", self.user_agent)
        if self._token is not None:
            request.add_header("Authorization", f"Bearer {self._token}")
        try:
            return self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            challenge = exc.headers.get("WWW-Authenticate", "")
            if exc.code != 401 or not challenge.startswith("Bearer "):
                raise
            parameters = dict(_BEARER_PARAMETER.findall(challenge))
            realm = parameters.get("realm")
            if not realm:
                raise
            query = {
                key: value
                for key, value in {
                    "service": parameters.get("service"),
                    "scope": parameters.get("scope"),
                }.items()
                if value
            }
            token_request = urllib.request.Request(
                realm + ("?" + urllib.parse.urlencode(query) if query else "")
            )
            token_request.add_header("User-Agent", self.user_agent)
            if self.username is not None and self.password is not None:
                credentials = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
                token_request.add_header("Authorization", f"Basic {credentials}")
            with self._opener.open(token_request, timeout=self.timeout) as response:
                payload = json.load(response)
            token = payload.get("token", payload.get("access_token"))
            if not isinstance(token, str) or not token:
                raise EnvironmentError("OCI registry returned an invalid bearer token") from exc
            self._token = token
            retry = urllib.request.Request(
                request.full_url,
                data=request.data,
                headers=dict(request.header_items()),
                method=request.get_method(),
            )
            retry.add_header("Authorization", f"Bearer {token}")
            return self._opener.open(retry, timeout=self.timeout)

    def manifest(self, reference: str) -> ManifestResponse:
        encoded = urllib.parse.quote(reference, safe=":")
        request = urllib.request.Request(self._url(f"manifests/{encoded}"))
        request.add_header("Accept", _ACCEPT)
        try:
            with self._request(request) as response:
                data = response.read(4 * 1024 * 1024 + 1)
                media_type = response.headers.get_content_type()
                recorded_digest = response.headers.get("Docker-Content-Digest")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise DownloadUnavailable(
                    f"OCI manifest is not available: {self.repository.display}:{reference}"
                ) from exc
            raise DownloadUnavailable(f"OCI registry request failed: HTTP {exc.code}") from exc
        except OSError as exc:
            raise DownloadUnavailable(f"OCI registry is unavailable: {exc}") from exc
        if len(data) > 4 * 1024 * 1024:
            raise EnvironmentError("OCI manifest exceeds the supported size limit")
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        if recorded_digest is not None and recorded_digest != digest:
            raise EnvironmentError("OCI registry returned a mismatched manifest digest")
        if reference.startswith("sha256:") and reference != digest:
            raise EnvironmentError("OCI manifest content does not match its requested digest")
        return ManifestResponse(data, media_type, digest)

    def download_blob(
        self,
        descriptor: dict[str, Any],
        store: EnvironmentStore,
        events: EventEmitter,
    ) -> Path:
        digest = descriptor.get("digest")
        size = descriptor.get("size")
        match = _DIGEST.fullmatch(digest) if isinstance(digest, str) else None
        if match is None or not isinstance(size, int) or size < 0:
            raise EnvironmentError("OCI manifest contains an invalid blob descriptor")
        destination = store.oci_blobs / match.group(1)
        with FileLock(store.lock_dir / f"oci-{match.group(1)}.lock", timeout=1800):
            if destination.is_file() and destination.stat().st_size == size:
                if _digest_path(destination) == digest:
                    events.emit("library.layer_cached", "Reusing cached OCI blob", digest=digest)
                    return destination
                destination.unlink()
            temporary = destination.with_name(f".{destination.name}.partial")
            for attempt in range(1, _BLOB_INTEGRITY_ATTEMPTS + 1):
                if temporary.exists() and temporary.stat().st_size > size:
                    temporary.unlink()
                offset = temporary.stat().st_size if temporary.exists() else 0
                request = urllib.request.Request(self._url(f"blobs/{digest}"))
                if offset:
                    request.add_header("Range", f"bytes={offset}-")
                if attempt > 1:
                    request.add_header("Cache-Control", "no-cache")
                events.emit(
                    "library.layer_download_started",
                    "Downloading OCI blob",
                    digest=digest,
                    size=size,
                    attempt=attempt,
                    resumed_bytes=offset,
                )
                observed = hashlib.sha256()
                written = offset
                if offset:
                    with temporary.open("rb") as existing:
                        for chunk in iter(lambda: existing.read(1024 * 1024), b""):
                            observed.update(chunk)
                try:
                    response = self._request(request)
                    if offset and response.status != 206:
                        offset = 0
                        written = 0
                        observed = hashlib.sha256()
                    if offset:
                        content_range = response.headers.get("Content-Range", "")
                        if not content_range.startswith(f"bytes {offset}-"):
                            response.close()
                            temporary.unlink(missing_ok=True)
                            raise EnvironmentError("OCI registry returned an invalid byte range")
                    mode = "ab" if offset else "wb"
                    with response, temporary.open(mode) as output:
                        while chunk := response.read(1024 * 1024):
                            written += len(chunk)
                            if written > size:
                                raise EnvironmentError("OCI blob exceeds its declared size")
                            observed.update(chunk)
                            output.write(chunk)
                    observed_digest = "sha256:" + observed.hexdigest()
                    if written == size and observed_digest == digest:
                        temporary.replace(destination)
                        break
                    # A short read is a truncated transfer (registry or proxy
                    # closed the connection early): keep the partial so the
                    # next attempt resumes with a Range request instead of
                    # restarting a multi-gigabyte download from zero. Only a
                    # complete-but-mismatched blob is real corruption.
                    truncated = written < size
                    if not truncated:
                        temporary.unlink(missing_ok=True)
                    if attempt == _BLOB_INTEGRITY_ATTEMPTS:
                        temporary.unlink(missing_ok=True)
                        reason = "was truncated" if truncated else "failed digest verification"
                        raise EnvironmentError(
                            f"downloaded OCI blob {reason} "
                            f"after {attempt} attempts (expected {digest}, got "
                            f"{observed_digest}, bytes {written}/{size})"
                        )
                    events.emit(
                        "library.layer_download_retry",
                        "Resuming truncated OCI blob"
                        if truncated
                        else "Retrying OCI blob after integrity verification failed",
                        digest=digest,
                        attempt=attempt + 1,
                        truncated=truncated,
                        observed_digest=observed_digest,
                        downloaded_bytes=written,
                        expected_bytes=size,
                    )
                except urllib.error.HTTPError as exc:
                    raise DownloadUnavailable(f"OCI blob download failed: HTTP {exc.code}") from exc
                except OSError as exc:
                    # Connection resets and read timeouts mid-transfer are
                    # retryable; the kept partial resumes on the next attempt.
                    if attempt == _BLOB_INTEGRITY_ATTEMPTS:
                        raise DownloadUnavailable(f"OCI blob download failed: {exc}") from exc
                    events.emit(
                        "library.layer_download_retry",
                        "Retrying OCI blob after a transport error",
                        digest=digest,
                        attempt=attempt + 1,
                        error=str(exc),
                    )
        return destination

    def blob_exists(self, digest: str) -> bool:
        request = urllib.request.Request(self._url(f"blobs/{digest}"), method="HEAD")
        try:
            with self._request(request) as response:
                recorded = response.headers.get("Docker-Content-Digest")
                return recorded in {None, digest}
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False
            raise DownloadUnavailable(f"OCI blob lookup failed: HTTP {exc.code}") from exc
        except OSError as exc:
            raise DownloadUnavailable(f"OCI registry is unavailable: {exc}") from exc

    def manifest_exists(self, digest: str) -> bool:
        encoded = urllib.parse.quote(digest, safe=":")
        request = urllib.request.Request(self._url(f"manifests/{encoded}"), method="HEAD")
        request.add_header("Accept", _ACCEPT)
        try:
            with self._request(request) as response:
                recorded = response.headers.get("Docker-Content-Digest")
                return recorded in {None, digest}
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False
            raise DownloadUnavailable(f"OCI manifest lookup failed: HTTP {exc.code}") from exc
        except OSError as exc:
            raise DownloadUnavailable(f"OCI registry is unavailable: {exc}") from exc

    def upload_blob(self, path: Path, digest: str) -> None:
        if _digest_path(path) != digest:
            raise EnvironmentError("refusing to upload a blob with a mismatched digest")
        if self.blob_exists(digest):
            return
        request = urllib.request.Request(self._url("blobs/uploads/"), data=b"", method="POST")
        try:
            with self._request(request) as response:
                location = response.headers.get("Location")
                if response.status == 201:
                    return
                if response.status != 202 or not location:
                    raise EnvironmentError("OCI registry did not start a blob upload")
                upload_url = urllib.parse.urljoin(response.url, location)
            separator = "&" if urllib.parse.urlsplit(upload_url).query else "?"
            upload_url += separator + urllib.parse.urlencode({"digest": digest})
            with path.open("rb") as source:
                upload = urllib.request.Request(upload_url, data=source, method="PUT")
                upload.add_header("Content-Type", "application/octet-stream")
                upload.add_header("Content-Length", str(path.stat().st_size))
                with self._request(upload) as response:
                    if response.status != 201:
                        raise EnvironmentError("OCI registry did not accept the blob upload")
        except urllib.error.HTTPError as exc:
            raise DownloadUnavailable(f"OCI blob upload failed: HTTP {exc.code}") from exc
        except OSError as exc:
            raise DownloadUnavailable(f"OCI blob upload failed: {exc}") from exc

    def publish_manifest(self, reference: str, data: bytes, media_type: str) -> str:
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        encoded = urllib.parse.quote(reference, safe=":")
        request = urllib.request.Request(self._url(f"manifests/{encoded}"), data=data, method="PUT")
        request.add_header("Content-Type", media_type)
        request.add_header("Content-Length", str(len(data)))
        try:
            with self._request(request) as response:
                if response.status != 201:
                    raise EnvironmentError("OCI registry did not accept the manifest")
                recorded = response.headers.get("Docker-Content-Digest")
        except urllib.error.HTTPError as exc:
            raise DownloadUnavailable(f"OCI manifest upload failed: HTTP {exc.code}") from exc
        except OSError as exc:
            raise DownloadUnavailable(f"OCI manifest upload failed: {exc}") from exc
        if recorded is not None and recorded != digest:
            raise EnvironmentError("OCI registry reported a mismatched published manifest digest")
        return digest


def _digest_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _json_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnvironmentError(f"OCI {label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise EnvironmentError(f"OCI {label} must be a JSON object")
    return value


def _platform_matches(descriptor: dict[str, Any]) -> bool:
    platform = descriptor.get("platform")
    if not isinstance(platform, dict):
        return False
    compatibility = platform_compatibility()
    annotations = descriptor.get("annotations")
    if (
        not isinstance(annotations, dict)
        or annotations.get("org.lean-runtime.platform.abi") != compatibility["abi"]
    ):
        return False
    architecture = {"x86_64": "amd64", "arm64": "arm64"}.get(
        compatibility["machine"], compatibility["machine"]
    )
    return (
        platform.get("os") == compatibility["system"]
        and platform.get("architecture") == architecture
    )


class OCIEnvironmentCache:
    def __init__(
        self,
        repository: OCIRepository,
        store: EnvironmentStore,
        bundles: EnvironmentBundles,
        events: EventEmitter,
        verifier: SignatureVerifier | None = None,
    ) -> None:
        self.repository = repository
        self.store = store
        self.bundles = bundles
        self.events = events
        self.verifier = verifier
        self.client = OCIRegistryClient(repository)

    def pull(self, lock: EnvironmentLock, *, name: str | None = None) -> str:
        self.events.emit(
            "library.lookup",
            "Looking up a downloadable environment",
            registry=self.repository.display,
            lock_id=lock.lock_id,
        )
        response = self.client.manifest(lock.lock_id)
        if self.verifier is not None:
            self.verifier.verify(self.repository, response.digest)
            self.events.emit(
                "library.signature_verified",
                "Verified environment publisher signature",
                registry=self.repository.display,
                digest=response.digest,
            )
        document = _json_object(response.data, "manifest")
        if response.media_type == INDEX_MEDIA_TYPE or document.get("mediaType") == INDEX_MEDIA_TYPE:
            manifests = document.get("manifests")
            if not isinstance(manifests, list):
                raise EnvironmentError("OCI index has no manifest list")
            candidates = [
                item for item in manifests if isinstance(item, dict) and _platform_matches(item)
            ]
            if not candidates:
                raise DownloadUnavailable("OCI index has no compatible platform manifest")
            descriptor = candidates[0]
            selected = self.client.manifest(str(descriptor.get("digest")))
            if selected.digest != descriptor.get("digest") or len(selected.data) != descriptor.get(
                "size"
            ):
                raise EnvironmentError("OCI platform manifest descriptor mismatch")
            manifest = _json_object(selected.data, "platform manifest")
            manifest_descriptor = descriptor
            manifest_data = selected.data
        elif (
            response.media_type == MANIFEST_MEDIA_TYPE
            or document.get("mediaType") == MANIFEST_MEDIA_TYPE
        ):
            manifest = document
            manifest_data = response.data
            manifest_descriptor = {
                "mediaType": MANIFEST_MEDIA_TYPE,
                "digest": response.digest,
                "size": len(response.data),
                "annotations": {
                    "org.lean-runtime.platform.schema": platform_compatibility()["schema"],
                    "org.lean-runtime.platform.abi": platform_compatibility()["abi"],
                },
                "platform": {
                    "os": platform_compatibility()["system"],
                    "architecture": {"x86_64": "amd64", "arm64": "arm64"}.get(
                        platform_compatibility()["machine"], platform_compatibility()["machine"]
                    ),
                },
            }
        else:
            raise EnvironmentError("OCI registry returned an unsupported manifest media type")
        descriptors = [manifest.get("config"), *manifest.get("layers", [])]
        if not all(isinstance(item, dict) for item in descriptors):
            raise EnvironmentError("OCI platform manifest is incomplete")
        lease_digests = [
            str(manifest_descriptor["digest"]),
            *(str(descriptor["digest"]) for descriptor in descriptors),
        ]
        with self.store.oci_blob_lease(lease_digests):
            entries: dict[str, Path] = {}
            manifest_path = self._cache_manifest(manifest_descriptor, manifest_data)
            entries[
                "blobs/sha256/" + str(manifest_descriptor["digest"]).removeprefix("sha256:")
            ] = manifest_path
            for descriptor in descriptors:
                assert isinstance(descriptor, dict)
                path = self.client.download_blob(descriptor, self.store, self.events)
                entries["blobs/sha256/" + str(descriptor["digest"]).removeprefix("sha256:")] = path
            index = {
                "schemaVersion": 2,
                "mediaType": INDEX_MEDIA_TYPE,
                "manifests": [manifest_descriptor],
            }
            info = self.bundles.import_layout(
                index,
                entries,
                origin={"kind": "downloadable", "library": self.repository.display},
                name=name,
            )
        self.events.emit(
            "library.verified",
            "Imported a verified downloadable environment",
            environment_id=info.environment_id,
            registry=self.repository.display,
        )
        return info.environment_id

    def _cache_manifest(self, descriptor: dict[str, Any], data: bytes) -> Path:
        digest = descriptor.get("digest")
        match = _DIGEST.fullmatch(digest) if isinstance(digest, str) else None
        if match is None or len(data) != descriptor.get("size"):
            raise EnvironmentError("OCI manifest descriptor is invalid")
        path = self.store.oci_blobs / match.group(1)
        if not path.exists():
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
                temporary = Path(output.name)
                output.write(data)
            try:
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return path


@dataclass(frozen=True, slots=True)
class PublicationInfo:
    library: str
    exact_environment_id: str
    environment_id: str
    computer_copy_id: str
    publication_id: str | None
    uploaded_files: int
    total_blob_bytes: int
    uploaded_bytes: int
    reused_bytes: int
    computer_record: dict[str, Any]

    @property
    def reuse_percent(self) -> float:
        if self.total_blob_bytes == 0:
            return 100.0
        return 100.0 * self.reused_bytes / self.total_blob_bytes

    def to_dict(self) -> dict[str, Any]:
        return {
            "library": self.library,
            "exact_environment_id": self.exact_environment_id,
            "environment_id": self.environment_id,
            "computer_copy_id": self.computer_copy_id,
            "publication_id": self.publication_id,
            "uploaded_files": self.uploaded_files,
            "total_blob_bytes": self.total_blob_bytes,
            "uploaded_bytes": self.uploaded_bytes,
            "reused_bytes": self.reused_bytes,
            "reuse_percent": self.reuse_percent,
            "computer_record": self.computer_record,
        }


class OCIEnvironmentPublisher:
    def __init__(
        self,
        repository: OCIRepository,
        store: EnvironmentStore,
        bundles: EnvironmentBundles,
        events: EventEmitter,
    ) -> None:
        self.repository = repository
        self.store = store
        self.bundles = bundles
        self.events = events
        self.client = OCIRegistryClient(repository)

    def publish(
        self,
        environment_id: str,
        *,
        tags: tuple[str, ...] = (),
        finalize: bool = True,
    ) -> PublicationInfo:
        with tempfile.TemporaryDirectory(prefix="lean-runtime-publish-") as temporary:
            temporary_root = Path(temporary)
            layout_root = temporary_root / "layout"
            self.events.emit(
                "library.bundle_export_started",
                "Exporting and verifying the environment OCI layout",
                environment_id=environment_id,
                registry=self.repository.display,
            )
            bundle_info = self.bundles.export_layout(environment_id, layout_root)
            self.events.emit(
                "library.bundle_ready",
                "Environment OCI layout is ready for publication",
                environment_id=environment_id,
                blob_bytes=sum(
                    path.stat().st_size for path in layout_root.rglob("*") if path.is_file()
                ),
            )
            entries = {
                path.relative_to(layout_root).as_posix(): path
                for path in layout_root.rglob("*")
                if path.is_file()
            }
            index_path = entries.get("index.json")
            if index_path is None:
                raise EnvironmentError("exported OCI layout has no index")
            index_data = index_path.read_bytes()
            index = _json_object(index_data, "index")
            manifests = index.get("manifests")
            if not isinstance(manifests, list) or len(manifests) != 1:
                raise EnvironmentError("exported OCI index has no platform manifest")
            manifest_descriptor = manifests[0]
            if not isinstance(manifest_descriptor, dict):
                raise EnvironmentError("exported OCI manifest descriptor is invalid")
            manifest_key = "blobs/sha256/" + str(
                manifest_descriptor.get("digest", "")
            ).removeprefix("sha256:")
            manifest_path = entries.get(manifest_key)
            if manifest_path is None:
                raise EnvironmentError("exported OCI platform manifest is missing")
            manifest_data = manifest_path.read_bytes()
            manifest = _json_object(manifest_data, "platform manifest")
            descriptors = [manifest.get("config"), *manifest.get("layers", [])]
            if not all(isinstance(item, dict) for item in descriptors):
                raise EnvironmentError("exported OCI platform manifest is incomplete")
            uploaded = 0
            total_blob_bytes = 0
            uploaded_bytes = 0
            for descriptor in descriptors:
                assert isinstance(descriptor, dict)
                digest = str(descriptor.get("digest", ""))
                blob = entries.get("blobs/sha256/" + digest.removeprefix("sha256:"))
                if blob is None:
                    raise EnvironmentError("exported OCI blob is missing")
                size = descriptor.get("size")
                if not isinstance(size, int) or size < 0 or blob.stat().st_size != size:
                    raise EnvironmentError("exported OCI blob descriptor has an invalid size")
                total_blob_bytes += size
                existed = self.client.blob_exists(digest)
                self.events.emit(
                    "library.layer_reused" if existed else "library.layer_upload_started",
                    "Reusing remote OCI blob" if existed else "Uploading OCI blob",
                    digest=digest,
                    size=size,
                )
                self.client.upload_blob(blob, digest)
                uploaded += int(not existed)
                uploaded_bytes += 0 if existed else size
                if not existed:
                    self.events.emit(
                        "library.layer_uploaded",
                        "Uploaded OCI blob",
                        digest=digest,
                        size=size,
                    )
            manifest_digest = self.client.publish_manifest(
                str(manifest_descriptor["digest"]), manifest_data, MANIFEST_MEDIA_TYPE
            )
            if manifest_digest != manifest_descriptor["digest"]:
                raise EnvironmentError("published platform manifest digest changed")
            self.events.emit(
                "library.platform_manifest_published",
                "Published the computer-specific OCI manifest",
                digest=manifest_digest,
            )
            if tags and not finalize:
                raise ValueError("tags can only be published while finalizing an OCI index")
            index_digest = (
                self.publish_index(
                    bundle_info.exact_environment_id, [manifest_descriptor], tags=tags
                )
                if finalize
                else None
            )
            self.events.emit(
                "library.published",
                "Published downloadable environment",
                registry=self.repository.display,
                exact_environment_id=bundle_info.exact_environment_id,
                environment_id=environment_id,
                index_digest=index_digest,
            )
            return PublicationInfo(
                self.repository.display,
                bundle_info.exact_environment_id,
                environment_id,
                manifest_digest,
                index_digest,
                uploaded,
                total_blob_bytes,
                uploaded_bytes,
                total_blob_bytes - uploaded_bytes,
                manifest_descriptor,
            )

    def publish_index(
        self,
        lock_id: str,
        platform_descriptors: list[dict[str, Any]],
        *,
        tags: tuple[str, ...] = (),
    ) -> str:
        if not re.fullmatch(r"lock_[0-9a-f]{64}", lock_id):
            raise ValueError(f"invalid lock identity: {lock_id!r}")
        if not platform_descriptors:
            raise ValueError("an OCI index requires at least one platform manifest")
        platforms: set[tuple[str, str, str]] = set()
        for descriptor in platform_descriptors:
            if descriptor.get("mediaType") != MANIFEST_MEDIA_TYPE:
                raise ValueError("platform result has an unsupported manifest media type")
            platform = descriptor.get("platform")
            annotations = descriptor.get("annotations")
            if not isinstance(platform, dict) or not isinstance(annotations, dict):
                raise ValueError("platform result has incomplete compatibility metadata")
            key = (
                str(platform.get("os")),
                str(platform.get("architecture")),
                str(annotations.get("org.lean-runtime.platform.abi")),
            )
            if key in platforms:
                raise ValueError(f"duplicate OCI platform result: {'/'.join(key)}")
            platforms.add(key)
            digest = descriptor.get("digest")
            if not isinstance(digest, str) or not self.client.manifest_exists(digest):
                raise EnvironmentError(f"platform manifest is not published: {digest!r}")
        ordered = sorted(
            platform_descriptors,
            key=lambda item: (
                str(item["platform"]["os"]),
                str(item["platform"]["architecture"]),
                str(item["annotations"]["org.lean-runtime.platform.abi"]),
                str(item["digest"]),
            ),
        )
        index_data = canonical_json_bytes(
            {
                "schemaVersion": 2,
                "mediaType": INDEX_MEDIA_TYPE,
                "manifests": ordered,
                "annotations": {"org.lean-runtime.lock-id": lock_id},
            }
        )
        index_digest = self.client.publish_manifest(lock_id, index_data, INDEX_MEDIA_TYPE)
        for tag in tags:
            if not tag or len(tag) > 128 or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", tag):
                raise ValueError(f"invalid OCI tag: {tag!r}")
            self.client.publish_manifest(tag, index_data, INDEX_MEDIA_TYPE)
        return index_digest
