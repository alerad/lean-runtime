"""OCI Distribution pull transport and transparent environment caches."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Protocol, TypeVar

from .bundles import (
    CAPSULE_BUNDLE_SCHEMA,
    CAPSULE_CONFIG_MEDIA_TYPE,
    EnvironmentBundles,
    _capsule_config_object,
)
from .capsules import CapsuleManifest
from .errors import (
    CredentialAcquisitionError,
    DownloadLimitExceeded,
    DownloadUnavailable,
    EnvironmentError,
    PublicationError,
    RegistryRequestError,
)
from .events import EventEmitter
from .lockfiles import EnvironmentLock
from .locking import FileLock
from .oci_protocol import (
    INDEX_MEDIA_TYPE,
    MANIFEST_MEDIA_TYPE,
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
from .packs import PACK_MEDIA_TYPE, PackFrame, SparsePack, project_artifacts, unpack_frame
from .policies import format_byte_size
from .serialization import canonical_json_bytes
from .store import EnvironmentStore, environment_identity, platform_compatibility

_REPOSITORY = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+")
_BEARER_PARAMETER = re.compile(r'([a-zA-Z]+)="([^"]*)"')
_DIGEST = re.compile(r"sha256:([0-9a-f]{64})")
_ACCEPT = ", ".join((INDEX_MEDIA_TYPE, MANIFEST_MEDIA_TYPE))
DEFAULT_ENVIRONMENT_LIBRARIES = ("oci://ghcr.io/alerad/lean-runtime-cache",)
_BLOB_INTEGRITY_ATTEMPTS = 4
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class RegistryCredential:
    username: str | None
    password: str | None
    source: str

    @property
    def authenticated(self) -> bool:
        return self.username is not None and self.password is not None

    @classmethod
    def discover(cls, repository: OCIRepository, *, timeout: float = 10) -> RegistryCredential:
        username = os.environ.get("LEAN_RUNTIME_REGISTRY_USERNAME")
        password = os.environ.get("LEAN_RUNTIME_REGISTRY_PASSWORD")
        if username is not None or password is not None:
            if not username or not password:
                raise CredentialAcquisitionError(
                    "registry credentials require both LEAN_RUNTIME_REGISTRY_USERNAME "
                    "and LEAN_RUNTIME_REGISTRY_PASSWORD",
                    provider="environment",
                    failure_kind="invalid_configuration",
                    retryable=False,
                )
            return cls(username, password, "environment")
        if repository.registry == "ghcr.io" and shutil.which("gh") is not None:
            try:
                status = subprocess.run(
                    (
                        "gh",
                        "auth",
                        "status",
                        "--active",
                        "--hostname",
                        "github.com",
                        "--json",
                        "hosts",
                        "--show-token",
                    ),
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise CredentialAcquisitionError(
                    f"GitHub CLI credential acquisition timed out after {timeout:g} seconds",
                    provider="GitHub CLI",
                    failure_kind="timeout",
                    retryable=True,
                ) from exc
            except OSError as exc:
                raise CredentialAcquisitionError(
                    f"GitHub CLI credential acquisition failed: {exc}",
                    provider="GitHub CLI",
                    failure_kind="process_error",
                    retryable=False,
                ) from exc
            try:
                payload = json.loads(status.stdout)
                accounts = payload.get("hosts", {}).get("github.com", [])
                active = next(
                    (
                        account
                        for account in accounts
                        if isinstance(account, dict) and account.get("active") is True
                    ),
                    None,
                )
            except (AttributeError, json.JSONDecodeError) as exc:
                raise CredentialAcquisitionError(
                    "GitHub CLI returned malformed authentication status",
                    provider="GitHub CLI",
                    failure_kind="invalid_response",
                    retryable=False,
                ) from exc
            if isinstance(active, dict):
                selected_username = active.get("login")
                selected_token = active.get("token")
                selected_state = active.get("state")
                if (
                    isinstance(selected_username, str)
                    and selected_username
                    and isinstance(selected_token, str)
                    and selected_token
                    and selected_state in {None, "success"}
                ):
                    return cls(
                        selected_username,
                        selected_token,
                        "GitHub CLI",
                    )
                raise CredentialAcquisitionError(
                    "GitHub CLI has an active account but did not provide a usable token",
                    provider="GitHub CLI",
                    failure_kind="token_unavailable",
                    retryable=False,
                )
            if accounts:
                detail = status.stderr.strip()
                raise CredentialAcquisitionError(
                    "GitHub CLI has configured accounts but none is usable"
                    + (f": {detail}" if detail else ""),
                    provider="GitHub CLI",
                    failure_kind="account_unavailable",
                    retryable=False,
                )
        return cls(None, None, "anonymous")


def _request_failure(operation: str, exc: BaseException) -> RegistryRequestError:
    if isinstance(exc, urllib.error.HTTPError):
        retryable = exc.code in {408, 429} or 500 <= exc.code < 600
        return RegistryRequestError(
            f"OCI {operation} failed: HTTP {exc.code}",
            operation=operation,
            status_code=exc.code,
            retryable=retryable,
        )
    return RegistryRequestError(
        f"OCI {operation} failed: {exc}", operation=operation, retryable=True
    )


def capsule_reference(lock_id: str) -> str:
    if not re.fullmatch(r"lock_[0-9a-f]{64}", lock_id):
        raise ValueError(f"invalid lock identity: {lock_id!r}")
    return "capsule-" + lock_id


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


@dataclass(frozen=True, slots=True)
class CapsuleAcquisitionPlan:
    manifest_descriptor: dict[str, Any]
    manifest: dict[str, Any]
    manifest_data: bytes
    config_descriptor: dict[str, Any]
    config_data: bytes
    config: dict[str, Any]
    capsule: CapsuleManifest
    packs: tuple[tuple[SparsePack, dict[str, Any]], ...]
    roots: tuple[str, ...]
    capabilities: frozenset[str]
    modules: tuple[str, ...]
    frames: tuple[tuple[SparsePack, dict[str, Any], PackFrame], ...]
    total_bytes: int
    cached_bytes: int

    @property
    def download_bytes(self) -> int:
        return self.total_bytes - self.cached_bytes


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
        if timeout <= 0:
            raise ValueError("registry timeout must be positive")
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

    def check_push_access(self) -> None:
        """Prove repository push access with an empty, immediately cancelled upload."""
        request = urllib.request.Request(self._url("blobs/uploads/"), data=b"", method="POST")
        try:
            with self._request(request) as response:
                # A registry may complete a zero-byte upload immediately.
                if response.status == 201:
                    return
                if response.status != 202:
                    raise EnvironmentError("OCI registry did not accept the access probe")
                location = response.headers.get("Location")
                upload_url = urllib.parse.urljoin(response.url, location) if location else None
            if upload_url is not None:
                cleanup = urllib.request.Request(upload_url, method="DELETE")
                try:
                    with self._request(cleanup):
                        pass
                except (urllib.error.HTTPError, OSError):
                    # Upload sessions expire and contain no content. Cleanup is
                    # best-effort because registries vary on DELETE support.
                    pass
        except (urllib.error.HTTPError, OSError) as exc:
            raise _request_failure("push access probe", exc) from exc

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
                    events.emit(
                        "library.layer_cached", "Reusing cached OCI blob", digest=digest, size=size
                    )
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
                    last_progress = 0.0
                    with response, temporary.open(mode) as output:
                        while chunk := response.read(1024 * 1024):
                            written += len(chunk)
                            if written > size:
                                raise EnvironmentError("OCI blob exceeds its declared size")
                            observed.update(chunk)
                            output.write(chunk)
                            now = time.monotonic()
                            if written == size or now - last_progress >= 0.2:
                                last_progress = now
                                events.emit(
                                    "library.layer_progress",
                                    "Downloading OCI blob",
                                    phase="download",
                                    current_bytes=written,
                                    total_bytes=size,
                                    digest=digest,
                                )
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

    def download_blob_range(
        self,
        descriptor: dict[str, Any],
        *,
        offset: int,
        size: int,
        expected_digest: str | None = None,
    ) -> bytes:
        """Download one exact byte range without caching a partial OCI blob."""
        digest = descriptor.get("digest")
        total = descriptor.get("size")
        match = _DIGEST.fullmatch(digest) if isinstance(digest, str) else None
        if (
            match is None
            or not isinstance(total, int)
            or offset < 0
            or size < 1
            or offset + size > total
            or (expected_digest is not None and _DIGEST.fullmatch(expected_digest) is None)
        ):
            raise EnvironmentError("OCI range request is outside its blob descriptor")
        failure = ""
        for attempt in range(1, _BLOB_INTEGRITY_ATTEMPTS + 1):
            request = urllib.request.Request(self._url(f"blobs/{digest}"))
            request.add_header("Range", f"bytes={offset}-{offset + size - 1}")
            if attempt > 1:
                request.add_header("Cache-Control", "no-cache")
            try:
                with self._request(request) as response:
                    if response.status != 206:
                        raise DownloadUnavailable(
                            "OCI registry does not support sparse range acquisition"
                        )
                    content_range = response.headers.get("Content-Range")
                    expected = f"bytes {offset}-{offset + size - 1}/{total}"
                    if content_range != expected:
                        raise EnvironmentError("OCI registry returned a mismatched byte range")
                    data = bytes(response.read(size + 1))
            except urllib.error.HTTPError as exc:
                if attempt == _BLOB_INTEGRITY_ATTEMPTS or (
                    exc.code not in {408, 429} and not 500 <= exc.code < 600
                ):
                    raise DownloadUnavailable(
                        f"OCI sparse range request failed: HTTP {exc.code}"
                    ) from exc
                failure = f"HTTP {exc.code}"
            except OSError as exc:
                if attempt == _BLOB_INTEGRITY_ATTEMPTS:
                    raise DownloadUnavailable(f"OCI registry is unavailable: {exc}") from exc
                failure = str(exc)
            else:
                observed = "sha256:" + hashlib.sha256(data).hexdigest()
                if len(data) == size and (expected_digest is None or observed == expected_digest):
                    return data
                failure = (
                    "truncated byte range" if len(data) != size else "byte-range digest mismatch"
                )
                if attempt == _BLOB_INTEGRITY_ATTEMPTS:
                    raise EnvironmentError(f"OCI registry returned a {failure}")
            time.sleep(0.25 * attempt)
        raise DownloadUnavailable(f"OCI sparse range request failed: {failure}")  # pragma: no cover

    def read_blob(self, descriptor: dict[str, Any], *, limit: int = 64 * 1024**2) -> bytes:
        """Read and verify a small OCI blob without mutating the local cache."""
        digest = descriptor.get("digest")
        size = descriptor.get("size")
        match = _DIGEST.fullmatch(digest) if isinstance(digest, str) else None
        if match is None or not isinstance(size, int) or size < 0 or size > limit:
            raise EnvironmentError("OCI metadata blob exceeds supported limits")
        request = urllib.request.Request(self._url(f"blobs/{digest}"))
        try:
            with self._request(request) as response:
                data = bytes(response.read(size + 1))
        except urllib.error.HTTPError as exc:
            raise DownloadUnavailable(f"OCI metadata download failed: HTTP {exc.code}") from exc
        except OSError as exc:
            raise DownloadUnavailable(f"OCI registry is unavailable: {exc}") from exc
        if len(data) != size or "sha256:" + hashlib.sha256(data).hexdigest() != digest:
            raise EnvironmentError("OCI metadata blob failed digest verification")
        return data

    def cache_verified_blob(
        self,
        data: bytes,
        descriptor: dict[str, Any],
        store: EnvironmentStore,
    ) -> Path:
        """Atomically cache bytes already verified against an OCI descriptor."""
        digest = descriptor.get("digest")
        size = descriptor.get("size")
        match = _DIGEST.fullmatch(digest) if isinstance(digest, str) else None
        if (
            match is None
            or not isinstance(size, int)
            or len(data) != size
            or "sha256:" + hashlib.sha256(data).hexdigest() != digest
        ):
            raise EnvironmentError("refusing to cache an invalid OCI metadata blob")
        destination = store.oci_blobs / match.group(1)
        with FileLock(store.lock_dir / f"oci-{match.group(1)}.lock", timeout=1800):
            if destination.is_file() and destination.stat().st_size == size:
                if _digest_path(destination) == digest:
                    return destination
                destination.unlink()
            with tempfile.NamedTemporaryFile(dir=store.oci_blobs, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(data)
            try:
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
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
            raise _request_failure("blob lookup", exc) from exc
        except OSError as exc:
            raise _request_failure("blob lookup", exc) from exc

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
            raise _request_failure("manifest lookup", exc) from exc
        except OSError as exc:
            raise _request_failure("manifest lookup", exc) from exc

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
            raise _request_failure("blob upload", exc) from exc
        except OSError as exc:
            raise _request_failure("blob upload", exc) from exc

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
            raise _request_failure("manifest upload", exc) from exc
        except OSError as exc:
            raise _request_failure("manifest upload", exc) from exc
        if recorded is not None and recorded != digest:
            raise EnvironmentError("OCI registry reported a mismatched published manifest digest")
        last_error: DownloadUnavailable | None = None
        for attempt in range(4):
            try:
                remote = self.manifest(reference)
            except DownloadUnavailable as exc:
                last_error = exc
            else:
                if remote.digest == digest:
                    return digest
            if attempt < 3:
                time.sleep(0.25 * (2**attempt))
        detail = f": {last_error}" if last_error is not None else ""
        raise EnvironmentError(f"published OCI manifest failed remote digest verification{detail}")


def _json_object(data: bytes, label: str) -> dict[str, Any]:
    return _parse_json_object(data, label, subject="OCI")


def _manifest_descriptors(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    descriptors = [manifest.get("config"), *manifest.get("layers", [])]
    if not all(isinstance(item, dict) for item in descriptors):
        raise EnvironmentError("OCI platform manifest is incomplete")
    return descriptors


class OCIEnvironmentCache:
    def __init__(
        self,
        repository: OCIRepository,
        store: EnvironmentStore,
        bundles: EnvironmentBundles,
        events: EventEmitter,
        verifier: SignatureVerifier | None = None,
        *,
        max_download_bytes: int | None = None,
    ) -> None:
        self.repository = repository
        self.store = store
        self.bundles = bundles
        self.events = events
        self.verifier = verifier
        self.max_download_bytes = max_download_bytes
        self.client = OCIRegistryClient(repository)

    def _platform_manifest(
        self, lock: EnvironmentLock, *, reference: str | None = None
    ) -> tuple[dict[str, Any], dict[str, Any], bytes]:
        """Fetch, verify, and select this platform's manifest for a lock."""
        self.events.emit(
            "library.lookup",
            "Looking up a downloadable environment",
            registry=self.repository.display,
            lock_id=lock.lock_id,
        )
        response = self.client.manifest(reference or lock.lock_id)
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
        return manifest_descriptor, manifest, manifest_data

    def plan_capsule(
        self,
        lock: EnvironmentLock,
        roots: tuple[str, ...],
        *,
        capabilities: frozenset[str] = frozenset({"check"}),
    ) -> CapsuleAcquisitionPlan:
        """Plan exact sparse frames for imported roots without downloading packs."""
        manifest_descriptor, manifest, manifest_data = self._platform_manifest(
            lock, reference=capsule_reference(lock.lock_id)
        )
        config_descriptor = manifest.get("config")
        layers = manifest.get("layers")
        if not isinstance(config_descriptor, dict) or not isinstance(layers, list):
            raise DownloadUnavailable("downloadable environment has no capsule manifest")
        if config_descriptor.get("mediaType") != CAPSULE_CONFIG_MEDIA_TYPE:
            raise DownloadUnavailable("downloadable environment is a legacy full bundle")
        config_digest = str(config_descriptor.get("digest", ""))
        config_match = _DIGEST.fullmatch(config_digest)
        config_cache = (
            self.store.oci_blobs / config_match.group(1) if config_match is not None else None
        )
        config_size = config_descriptor.get("size")
        config_cached = (
            config_cache is not None
            and config_cache.is_file()
            and isinstance(config_size, int)
            and config_cache.stat().st_size == config_size
            and _digest_path(config_cache) == config_digest
        )
        config_data = (
            config_cache.read_bytes()
            if config_cached and config_cache is not None
            else self.client.read_blob(config_descriptor)
        )
        config = _capsule_config_object(config_data)
        if (
            config.get("schema") != CAPSULE_BUNDLE_SCHEMA
            or config.get("lock_id") != lock.lock_id
            or not isinstance(config.get("capsule"), dict)
            or not isinstance(config.get("packs"), list)
        ):
            raise EnvironmentError("downloadable capsule identity is invalid")
        capsule = CapsuleManifest.from_dict(config["capsule"])
        if capsule.lock_id != lock.lock_id or capsule.toolchain != lock.toolchain:
            raise EnvironmentError("downloadable capsule does not match its lock")
        closure = capsule.closure(roots)
        module_names = tuple(module.name for module in closure)
        descriptors = {
            str(item.get("digest")): item
            for item in layers
            if isinstance(item, dict) and item.get("mediaType") == PACK_MEDIA_TYPE
        }
        packs: list[tuple[SparsePack, dict[str, Any]]] = []
        frames: list[tuple[SparsePack, dict[str, Any], PackFrame]] = []
        artifact_map = {
            artifact.path: artifact for module in capsule.modules for artifact in module.artifacts
        }
        total_bytes = int(config_descriptor.get("size", 0))
        cached_bytes = total_bytes if config_cached else 0
        selected = frozenset(module_names)
        for raw in config["packs"]:
            if not isinstance(raw, dict):
                raise EnvironmentError("downloadable capsule has an invalid pack index")
            pack = SparsePack.from_dict(raw)
            descriptor = descriptors.get(pack.digest)
            if descriptor is None or descriptor.get("size") != pack.size:
                raise EnvironmentError("downloadable capsule pack descriptor mismatch")
            packs.append((pack, descriptor))
            if pack.capability not in capabilities:
                continue
            for frame in pack.frames_for_modules(selected):
                frames.append((pack, descriptor, frame))
                total_bytes += frame.size
                if all(
                    (
                        self.store.cas_artifacts / artifact_map[path].digest.removeprefix("sha256:")
                    ).is_file()
                    and (
                        self.store.cas_artifacts / artifact_map[path].digest.removeprefix("sha256:")
                    )
                    .stat()
                    .st_size
                    == artifact_map[path].size
                    and _digest_path(
                        self.store.cas_artifacts / artifact_map[path].digest.removeprefix("sha256:")
                    )
                    == artifact_map[path].digest
                    for path in frame.artifacts
                ):
                    cached_bytes += frame.size
        return CapsuleAcquisitionPlan(
            manifest_descriptor,
            manifest,
            manifest_data,
            config_descriptor,
            config_data,
            config,
            capsule,
            tuple(packs),
            roots,
            capabilities,
            module_names,
            tuple(frames),
            total_bytes,
            cached_bytes,
        )

    def pull_capsule(
        self,
        lock: EnvironmentLock,
        roots: tuple[str, ...],
        *,
        name: str | None = None,
        capabilities: frozenset[str] = frozenset({"check"}),
    ) -> str:
        """Acquire only selected module frames and project a check environment."""
        plan = self.plan_capsule(lock, roots, capabilities=capabilities)
        self.client.cache_verified_blob(plan.config_data, plan.config_descriptor, self.store)
        self.events.emit(
            "acquisition.planned",
            f"Sparse environment download planned: {format_byte_size(plan.download_bytes)}",
            phase="plan",
            current_bytes=plan.cached_bytes,
            total_bytes=plan.total_bytes,
            registry=self.repository.display,
            lock_id=lock.lock_id,
            download_bytes=plan.download_bytes,
            cached_bytes=plan.cached_bytes,
            modules=len(plan.modules),
        )
        if self.max_download_bytes is not None and plan.download_bytes > self.max_download_bytes:
            raise DownloadLimitExceeded(
                f"acquiring this import closure downloads {format_byte_size(plan.download_bytes)}, "
                f"above the configured limit of {format_byte_size(self.max_download_bytes)}"
            )
        artifacts = {
            artifact.path: artifact
            for module in plan.capsule.modules
            for artifact in module.artifacts
        }
        selected_digests = [
            artifacts[path].digest
            for module in plan.capsule.closure(plan.roots)
            for artifact in module.artifacts
            if artifact.capability in plan.capabilities
            for path in (artifact.path,)
            if path in artifacts
        ]
        missing_frames = [
            (descriptor, frame)
            for _pack, descriptor, frame in plan.frames
            if not all(
                (
                    self.store.cas_artifacts / artifacts[path].digest.removeprefix("sha256:")
                ).is_file()
                and (self.store.cas_artifacts / artifacts[path].digest.removeprefix("sha256:"))
                .stat()
                .st_size
                == artifacts[path].size
                and _digest_path(
                    self.store.cas_artifacts / artifacts[path].digest.removeprefix("sha256:")
                )
                == artifacts[path].digest
                for path in frame.artifacts
            )
        ]
        # Bound both concurrency and buffered compressed data. Eight parallel
        # range reads hide registry round-trip latency without turning a full
        # Mathlib closure into thousands of serial HTTP requests.
        # Hold a collection lease across unpacking and projection so a
        # concurrent clean cannot reclaim a freshly unpacked artifact.
        with self.store.cas_artifact_lease(selected_digests):
            downloaded_frame_bytes = 0
            total_frame_bytes = sum(frame.size for _descriptor, frame in missing_frames)
            completed_frames = 0
            for offset in range(0, len(missing_frames), 8):
                batch = missing_frames[offset : offset + 8]
                with ThreadPoolExecutor(max_workers=len(batch)) as executor:
                    compressed_frames = tuple(
                        executor.map(
                            lambda item: self.client.download_blob_range(
                                item[0],
                                offset=item[1].offset,
                                size=item[1].size,
                                expected_digest=item[1].digest,
                            ),
                            batch,
                        )
                    )
                for (_descriptor, frame), compressed in zip(batch, compressed_frames, strict=True):
                    unpack_frame(
                        compressed,
                        frame,
                        artifacts,
                        self.store.cas_artifacts,
                        lock_root=self.store.lock_dir,
                    )
                    downloaded_frame_bytes += frame.size
                    completed_frames += 1
                    self.events.emit(
                        "library.layer_progress",
                        "Downloading sparse capsule frames",
                        phase="download",
                        current_bytes=downloaded_frame_bytes,
                        total_bytes=total_frame_bytes,
                        digest=frame.digest,
                        frame_current=completed_frames,
                        frame_total=len(missing_frames),
                    )

            environment_id = environment_identity(lock)
            destination = self.store.environment_path(environment_id)
            with FileLock(self.store.lock_dir / f"{environment_id}.lock", timeout=1800):
                fresh = not destination.is_dir()
                stage = (
                    self.store.environments / f".staging-{os.getpid()}-{time.time_ns()}"
                    if fresh
                    else destination
                )
                workspace = stage / "workspace"
                try:
                    paths = {
                        artifact.path
                        for module in plan.capsule.closure(plan.roots)
                        for artifact in module.artifacts
                        if artifact.capability in plan.capabilities
                    }
                    project_artifacts(
                        paths,
                        artifacts,
                        self.store.cas_artifacts,
                        workspace,
                        lock_root=self.store.lock_dir,
                    )
                    capsule_path = workspace / ".lean-runtime" / "capsule.json"
                    capsule_path.parent.mkdir(parents=True, exist_ok=True)
                    capsule_path.write_bytes(canonical_json_bytes(plan.capsule.to_dict()))
                    if fresh:
                        (workspace / ".lake" / "build").mkdir(parents=True, exist_ok=True)
                        (workspace / "lean-toolchain").write_text(lock.toolchain + "\n")
                        (workspace / "lakefile.toml").write_text(lock.root_lakefile)
                        (workspace / "LeanRuntimeEnvironment.lean").write_text(lock.root_module)
                        (workspace / "lake-manifest.json").write_bytes(
                            canonical_json_bytes(lock.manifest)
                        )
                        self.store.publish_lock(lock)
                        metadata = {
                            "schema": "lean-runtime-published-environment/1",
                            "environment_id": environment_id,
                            "lock_id": lock.lock_id,
                            "toolchain": lock.toolchain,
                            "platform": platform_compatibility(),
                            "platform_compatibility": platform_compatibility(),
                            "build_profile": "release",
                            "status": "ready",
                            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "origin": {
                                "kind": "sparse_downloadable",
                                "library": self.repository.display,
                                "modules": list(plan.modules),
                                "capabilities": sorted(plan.capabilities),
                            },
                        }
                        (stage / "metadata.json").write_bytes(canonical_json_bytes(metadata))
                        stage.replace(destination)
                    else:
                        metadata_path = destination / "metadata.json"
                        metadata = _json_object(metadata_path.read_bytes(), "environment metadata")
                        origin = metadata.get("origin")
                        if (
                            not isinstance(origin, dict)
                            or origin.get("kind") != "sparse_downloadable"
                        ):
                            raise EnvironmentError(
                                "cannot extend a non-sparse environment projection"
                            )
                        origin["modules"] = sorted(
                            set(origin.get("modules", ())).union(plan.modules)
                        )
                        origin["capabilities"] = sorted(
                            set(origin.get("capabilities", ())).union(plan.capabilities)
                        )
                        metadata_path.write_bytes(canonical_json_bytes(metadata))
                except BaseException:
                    if fresh and stage.exists():
                        shutil.rmtree(stage)
                    raise
        if name:
            self.store.set_alias(name, environment_id)
        self.events.emit(
            "library.verified",
            "Sparse environment artifacts were verified and projected",
            environment_id=environment_id,
            modules=len(plan.modules),
        )
        return environment_id

    def _acquisition_sizes(self, descriptors: list[Any]) -> tuple[int, int]:
        """Return (total, locally cached) bytes for a manifest's blobs."""
        total = 0
        cached = 0
        for descriptor in descriptors:
            assert isinstance(descriptor, dict)
            size = descriptor.get("size")
            digest = str(descriptor.get("digest", ""))
            match = _DIGEST.fullmatch(digest)
            if match is None or not isinstance(size, int) or size < 0:
                raise EnvironmentError("OCI manifest contains an invalid blob descriptor")
            total += size
            blob = self.store.oci_blobs / match.group(1)
            if blob.is_file() and blob.stat().st_size == size:
                cached += size
        return total, cached

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


@dataclass(frozen=True, slots=True)
class PublicationAccess:
    registry: str
    username: str | None
    credential_source: str
    push_verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "registry": self.registry,
            "username": self.username,
            "credential_source": self.credential_source,
            "push_verified": self.push_verified,
        }


class OCIEnvironmentPublisher:
    def __init__(
        self,
        repository: OCIRepository,
        store: EnvironmentStore,
        bundles: EnvironmentBundles,
        events: EventEmitter,
        *,
        auth_timeout: float = 10,
        registry_timeout: float = 30,
        credential: RegistryCredential | None = None,
    ) -> None:
        if auth_timeout <= 0 or registry_timeout <= 0:
            raise ValueError("publication timeouts must be positive")
        self.repository = repository
        self.store = store
        self.bundles = bundles
        self.events = events
        try:
            self.credential = credential or RegistryCredential.discover(
                repository, timeout=auth_timeout
            )
        except CredentialAcquisitionError as exc:
            failure = PublicationError(
                f"publication authentication failed before registry access: {exc}",
                phase="credential_acquisition",
                registry=repository.display,
                retryable=exc.retryable,
                credential_source="none",
                attempted_provider=exc.provider,
                auth_failure_kind=exc.failure_kind,
                hint=(
                    "retry credential acquisition"
                    if exc.retryable
                    else "repair the credential provider"
                ),
            )
            events.emit(
                "library.publish_failed",
                str(failure),
                phase=failure.phase,
                registry=failure.registry,
                retryable=failure.retryable,
                published=False,
                partial=False,
                credential_source="none",
                attempted_provider=exc.provider,
                auth_failure_kind=exc.failure_kind,
                username=None,
                hint=failure.hint,
            )
            raise failure from exc
        self.client = OCIRegistryClient(
            repository,
            timeout=registry_timeout,
            username=self.credential.username,
            password=self.credential.password,
        )
        self._access: PublicationAccess | None = None
        self._manifest_attempted = False
        self._platform_published = False
        self._index_published = False

    def _auth_hint(self, status_code: int | None) -> str | None:
        if status_code not in {401, 403}:
            return None
        if self.repository.registry == "ghcr.io":
            if self.credential.source == "GitHub CLI":
                return "run `gh auth refresh -s write:packages,read:packages`, then retry"
            if self.credential.source == "environment":
                return (
                    "set LEAN_RUNTIME_REGISTRY_USERNAME and "
                    "LEAN_RUNTIME_REGISTRY_PASSWORD to a token with write:packages"
                )
            return (
                "authenticate with `gh auth login`, then run "
                "`gh auth refresh -s write:packages,read:packages`"
            )
        return (
            "set LEAN_RUNTIME_REGISTRY_USERNAME and LEAN_RUNTIME_REGISTRY_PASSWORD "
            "for this registry"
        )

    def _publication_failure(self, exc: BaseException, *, phase: str) -> PublicationError:
        status_code = exc.status_code if isinstance(exc, RegistryRequestError) else None
        retryable = (
            exc.retryable if isinstance(exc, RegistryRequestError) else isinstance(exc, OSError)
        )
        partial = self._manifest_attempted or self._platform_published or self._index_published
        if status_code in {401, 403}:
            identity = self.credential.username or "anonymous"
            qualification = (
                "; GHCR may also return 403 for an inaccessible or not-yet-created "
                "package namespace"
                if self.repository.registry == "ghcr.io" and status_code == 403
                else ""
            )
            message = (
                f"registry denied {phase} for {self.repository.display} as {identity} "
                f"(HTTP {status_code}); most likely the credentials lack repository push access"
                f"{qualification}"
            )
        elif partial:
            message = (
                f"publication was not finalized during {phase}: {exc}; immutable platform "
                "content may exist, but no verified final release was produced"
            )
        else:
            message = f"publication failed during {phase}: {exc}"
        failure = PublicationError(
            message,
            phase=phase,
            registry=self.repository.display,
            status_code=status_code,
            retryable=retryable,
            published=False,
            partial=partial,
            credential_source=self.credential.source,
            username=self.credential.username,
            hint=self._auth_hint(status_code),
        )
        self.events.emit(
            "library.publish_failed",
            message,
            phase=phase,
            registry=failure.registry,
            status_code=failure.status_code,
            retryable=failure.retryable,
            published=False,
            partial=failure.partial,
            credential_source=failure.credential_source,
            username=failure.username,
            hint=failure.hint,
        )
        return failure

    def fail(self, exc: BaseException, *, phase: str) -> PublicationError:
        """Record a required post-publication phase as a terminal failure."""
        return self._publication_failure(exc, phase=phase)

    def complete(self, result: PublicationInfo) -> None:
        """Emit success only after every requested publication phase has passed."""
        digest = result.publication_id or result.computer_copy_id
        self.events.emit(
            "library.published",
            "Published and remotely verified downloadable environment",
            registry=self.repository.display,
            reference=f"{self.repository.display}@{digest}",
            exact_environment_id=result.exact_environment_id,
            environment_id=result.environment_id,
            index_digest=result.publication_id,
            uploaded_bytes=result.uploaded_bytes,
            reused_bytes=result.reused_bytes,
            published=True,
            partial=False,
            remotely_verified=True,
        )

    def _required(self, phase: str, operation: Callable[[], _T]) -> _T:
        try:
            return operation()
        except PublicationError:
            raise
        except (EnvironmentError, OSError) as exc:
            raise self._publication_failure(exc, phase=phase) from exc

    def check_access(self) -> PublicationAccess:
        if self._access is not None:
            return self._access
        identity = self.credential.username or "anonymous"
        self.events.emit(
            "library.publish_auth_selected",
            f"Authenticating to {self.repository.registry} as {identity} "
            f"(source: {self.credential.source})",
            registry=self.repository.display,
            username=self.credential.username,
            credential_source=self.credential.source,
            authenticated=self.credential.authenticated,
        )
        self._required("access_preflight", self.client.check_push_access)
        self._access = PublicationAccess(
            self.repository.display,
            self.credential.username,
            self.credential.source,
            True,
        )
        self.events.emit(
            "library.publish_access_verified",
            "Registry push access verified",
            **self._access.to_dict(),
        )
        return self._access

    def publish(
        self,
        environment_id: str,
        *,
        tags: tuple[str, ...] = (),
        finalize: bool = True,
        profile: str = "check-capsule",
    ) -> PublicationInfo:
        try:
            return self._publish(
                environment_id,
                tags=tags,
                finalize=finalize,
                profile=profile,
            )
        except PublicationError:
            raise
        except (EnvironmentError, OSError) as exc:
            raise self._publication_failure(exc, phase="local_preparation") from exc

    def _publish(
        self,
        environment_id: str,
        *,
        tags: tuple[str, ...] = (),
        finalize: bool = True,
        profile: str = "check-capsule",
    ) -> PublicationInfo:
        if profile != "check-capsule":
            raise ValueError("publication profile must be 'check-capsule'")
        self.check_access()
        with tempfile.TemporaryDirectory(prefix="lean-runtime-publish-") as temporary:
            temporary_root = Path(temporary)
            layout_root = temporary_root / "layout"
            self.events.emit(
                "library.bundle_export_started",
                "Exporting and verifying the environment OCI layout",
                environment_id=environment_id,
                registry=self.repository.display,
            )
            bundle_info = self.bundles.export_capsule_layout(environment_id, layout_root)
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
                existed = self._required("blob_lookup", partial(self.client.blob_exists, digest))
                self.events.emit(
                    "library.layer_reused" if existed else "library.layer_upload_started",
                    "Reusing remote OCI blob" if existed else "Uploading OCI blob",
                    digest=digest,
                    size=size,
                )
                self._required(
                    "blob_upload",
                    partial(self.client.upload_blob, blob, digest),
                )
                uploaded += int(not existed)
                uploaded_bytes += 0 if existed else size
                if not existed:
                    self.events.emit(
                        "library.layer_uploaded",
                        "Uploaded OCI blob",
                        digest=digest,
                        size=size,
                    )
            self._manifest_attempted = True
            manifest_digest = self._required(
                "platform_manifest",
                lambda: self.client.publish_manifest(
                    str(manifest_descriptor["digest"]), manifest_data, MANIFEST_MEDIA_TYPE
                ),
            )
            if manifest_digest != manifest_descriptor["digest"]:
                raise EnvironmentError("published platform manifest digest changed")
            self._platform_published = True
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
        self.check_access()
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
            if not isinstance(digest, str) or not self._required(
                "platform_manifest_verification",
                partial(self.client.manifest_exists, digest),
            ):
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
        reference = capsule_reference(lock_id)
        for tag in tags:
            if not tag or len(tag) > 128 or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", tag):
                raise ValueError(f"invalid OCI tag: {tag!r}")
        self._manifest_attempted = True
        index_digest = self._required(
            "index_finalization",
            lambda: self.client.publish_manifest(reference, index_data, INDEX_MEDIA_TYPE),
        )
        self._index_published = True
        for tag in tags:
            self._manifest_attempted = True
            self._required(
                "tag_finalization",
                partial(self.client.publish_manifest, tag, index_data, INDEX_MEDIA_TYPE),
            )
        return index_digest
