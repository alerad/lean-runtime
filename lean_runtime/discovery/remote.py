"""Registry-published catalog updates layered over the bundled catalog.

A Runtime release bundles the catalog it was tested with. New exact Mathlib
environments are published afterwards to the same OCI library that already
serves their artifacts, so `new`, `update` and discovery can offer them without
a Runtime release. The remote catalog is purely additive: it can introduce new
entry ids with new exact locks, but it can never replace, remove or redefine a
bundled entry. Its trust is the trust already placed in that library; when
publisher verification is required, the catalog manifest's signature is checked
with the same policy as every downloaded environment.

Each added entry is also checked independently of the library before it is
offered: every package must come from a source the bundled catalog already uses
or from the Lean organizations, and an entry named after a release (for example
`mathlib-v4.34.1`) must pin exactly the commit that release tag names upstream.
Publications carry a monotonic sequence (the publishing commit's time), so a
replayed older publication is refused while a newer one may retract entries.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .._process import run_git
from .._project_identity import canonical_git_url
from ..errors import DownloadUnavailable, LeanRuntimeError
from ..oci import OCIRegistryClient, OCIRepository, RegistryCredential
from ..oci_protocol import MANIFEST_MEDIA_TYPE, blob_descriptor_path, json_object
from ..references import PACKAGE_ALIASES
from ..serialization import canonical_json_bytes
from ..toolchains import default_runtime_home
from .catalog import CATALOG_SCHEMA, MAX_CATALOG_BYTES, Catalog, CatalogEntry, _timestamp
from .defaults import default_catalog
from .errors import CatalogError

CATALOG_REFERENCE = "discovery-catalog-v1"
CATALOG_CONFIG_MEDIA_TYPE = "application/vnd.lean-runtime.discovery-catalog.config.v1+json"
CATALOG_LAYER_MEDIA_TYPE = "application/vnd.lean-runtime.discovery-catalog.v1+json"
SEQUENCE_ANNOTATION = "org.lean-runtime.catalog-sequence"
CACHE_SCHEMA = "lean-runtime.discovery.remote-catalog-cache/v2"
STATE_SCHEMA = "lean-runtime.discovery.remote-catalog-state/v1"
REFRESH_INTERVAL_SECONDS = 6 * 60 * 60
# After a failed or incomplete refresh, wait this long before contacting the
# network again so an unreachable registry does not slow every command.
RETRY_AFTER_FAILURE_SECONDS = 30 * 60
TRUSTED_OWNERS = ("leanprover-community", "leanprover")
# `bundled` pins every command to the release catalog (reproducible CI, audits).
SOURCE_ENVIRONMENT = "LEAN_RUNTIME_CATALOG"


class SignatureVerifier(Protocol):
    def verify(self, repository: OCIRepository, digest: str) -> None: ...


def _catalog_directory(home: str | os.PathLike[str] | None) -> Path:
    root = Path(home).expanduser().resolve() if home else default_runtime_home()
    return root / "catalog"


def cache_path(home: str | os.PathLike[str] | None) -> Path:
    return _catalog_directory(home) / "remote.json"


def state_path(home: str | os.PathLike[str] | None) -> Path:
    return _catalog_directory(home) / "refresh-state.json"


def remote_catalog_enabled() -> bool:
    return os.environ.get(SOURCE_ENVIRONMENT, "").strip().lower() != "bundled"


def _remote_entries(value: object) -> tuple[str, tuple[CatalogEntry, ...]]:
    """Parse a remote catalog, skipping entries this Runtime cannot represent.

    A future publication may carry an entry whose lock format this release does
    not understand; that entry is ignored instead of hiding every other update.
    """
    if not isinstance(value, dict) or value.get("schema") != CATALOG_SCHEMA:
        raise CatalogError("remote catalog has an unsupported schema")
    generated_at = _timestamp(value.get("generated_at"), "generated_at")
    raw_entries = value.get("entries")
    if not isinstance(raw_entries, list):
        raise CatalogError("remote catalog entries must be an array")
    entries: list[CatalogEntry] = []
    for item in raw_entries:
        if not isinstance(item, dict):
            continue
        try:
            entries.append(CatalogEntry.from_dict(item))
        except CatalogError:
            continue
    return generated_at, tuple(entries)


def merge_catalogs(
    bundled: Catalog, remote: tuple[CatalogEntry, ...], generated_at: str
) -> Catalog:
    """Add remote entries whose id and exact lock are both new to the bundle."""
    ids = {entry.id for entry in bundled.entries}
    locks = {entry.lock.lock_id for entry in bundled.entries}
    added: list[CatalogEntry] = []
    for entry in remote:
        if entry.id in ids or entry.lock.lock_id in locks:
            continue
        ids.add(entry.id)
        locks.add(entry.lock.lock_id)
        added.append(entry)
    if not added:
        return bundled
    return Catalog(generated_at=generated_at, entries=(*bundled.entries, *added))


@dataclass(frozen=True, slots=True)
class CachedCatalog:
    generated_at: str
    entries: tuple[CatalogEntry, ...]
    manifest_digest: str
    library: str
    fetched_at: float
    sequence: int
    verified: dict[str, str]
    catalog: dict[str, Any]

    @property
    def verified_entries(self) -> tuple[CatalogEntry, ...]:
        return tuple(
            entry for entry in self.entries if self.verified.get(entry.id) == entry.lock.lock_id
        )


def load_cached(home: str | os.PathLike[str] | None) -> CachedCatalog | None:
    """Return the retained remote catalog, or None when absent or unusable."""
    path = cache_path(home)
    try:
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict) or value.get("schema") != CACHE_SCHEMA:
            return None
        catalog = value.get("catalog")
        verified = value.get("verified")
        generated_at, entries = _remote_entries(catalog)
        if not isinstance(catalog, dict) or not isinstance(verified, dict):
            return None
        return CachedCatalog(
            generated_at,
            entries,
            str(value["manifest_digest"]),
            str(value["library"]),
            float(value["fetched_at"]),
            int(value["sequence"]),
            {str(key): str(item) for key, item in verified.items()},
            catalog,
        )
    except (OSError, ValueError, KeyError, TypeError, CatalogError):
        return None


_EFFECTIVE: dict[Path, tuple[tuple[int, int], Catalog]] = {}


def effective_catalog(home: str | os.PathLike[str] | None = None) -> Catalog:
    """Bundled catalog plus any retained remote additions for this Runtime home."""
    bundled = default_catalog()
    if not remote_catalog_enabled():
        return bundled
    path = cache_path(home)
    try:
        stat = path.stat()
    except OSError:
        return bundled
    key = (stat.st_mtime_ns, stat.st_size)
    retained = _EFFECTIVE.get(path)
    if retained is not None and retained[0] == key:
        return retained[1]
    cached = load_cached(home)
    catalog = (
        bundled
        if cached is None
        else merge_catalogs(bundled, cached.verified_entries, cached.generated_at)
    )
    _EFFECTIVE[path] = (key, catalog)
    return catalog


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    """What one refresh attempt did; failures never raise."""

    # fresh | backoff | updated | unchanged | kept-newer | disabled | offline | unavailable
    status: str
    added: tuple[str, ...] = ()
    library: str | None = None
    manifest_digest: str | None = None
    errors: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    unverified: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "added": list(self.added),
            "library": self.library,
            "manifest_digest": self.manifest_digest,
            "errors": list(self.errors),
            "rejected": list(self.rejected),
            "unverified": list(self.unverified),
        }


def fetch_catalog(
    repository: OCIRepository,
    *,
    verifier: SignatureVerifier | None = None,
    timeout: float = 10,
) -> tuple[bytes, str, int]:
    """Download the published catalog bytes, manifest digest and sequence."""
    client = OCIRegistryClient(repository, timeout=timeout)
    response = client.manifest(CATALOG_REFERENCE)
    if verifier is not None:
        verifier.verify(repository, response.digest)
    manifest = json_object(response.data, "manifest", subject="catalog")
    layers = manifest.get("layers")
    config = manifest.get("config")
    if (
        manifest.get("mediaType") != MANIFEST_MEDIA_TYPE
        or not isinstance(config, dict)
        or config.get("mediaType") != CATALOG_CONFIG_MEDIA_TYPE
        or not isinstance(layers, list)
        or len(layers) != 1
        or not isinstance(layers[0], dict)
        or layers[0].get("mediaType") != CATALOG_LAYER_MEDIA_TYPE
    ):
        raise CatalogError("published catalog manifest has unexpected media types")
    sequence = _sequence(manifest)
    return client.read_blob(layers[0], limit=MAX_CATALOG_BYTES), response.digest, sequence


def _sequence(manifest: dict[str, Any]) -> int:
    annotations = manifest.get("annotations")
    raw = annotations.get(SEQUENCE_ANNOTATION) if isinstance(annotations, dict) else None
    if not isinstance(raw, str) or not raw.isdigit():
        raise CatalogError("published catalog manifest has no publication sequence")
    return int(raw)


TagResolver = Callable[[str, str], "str | None"]


def resolve_tag(url: str, tag: str, *, timeout: float = 20) -> str | None:
    """Return the commit a tag names, "" when the tag is absent, None when unreachable."""
    reference = f"refs/tags/{tag}"
    # `--tags` lets the server filter to tags; without it Mathlib's ref
    # advertisement alone takes tens of seconds. Annotated tags are peeled.
    outcome = run_git("ls-remote", "--tags", url, reference, f"{reference}^{{}}", timeout=timeout)
    if not outcome.ok:
        return None
    refs: dict[str, str] = {}
    for line in outcome.stdout.splitlines():
        commit, _, name = line.partition("\t")
        refs[name.strip()] = commit.strip()
    return refs.get(f"{reference}^{{}}") or refs.get(reference) or ""


def _allowed_sources(bundled: Catalog) -> frozenset[str]:
    return frozenset(
        canonical_git_url(package.url)
        for entry in bundled.entries
        for package in entry.lock.packages
    )


def _trusted_source(url: str, allowed: frozenset[str]) -> bool:
    canonical = canonical_git_url(url)
    return canonical in allowed or any(
        canonical.startswith(f"https://github.com/{owner}/") for owner in TRUSTED_OWNERS
    )


def verify_entry(entry: CatalogEntry, allowed: frozenset[str], resolve: TagResolver) -> str:
    """Classify a published entry as verified, rejected, or unknown (unreachable)."""
    if any(not _trusted_source(package.url, allowed) for package in entry.lock.packages):
        return "rejected"
    alias, separator, version = entry.id.partition("-v")
    if not separator or alias not in PACKAGE_ALIASES:
        return "verified"
    owner, repository, _command = PACKAGE_ALIASES[alias]
    root_url = canonical_git_url(f"https://github.com/{owner}/{repository}")
    root = next(
        (package for package in entry.lock.packages if canonical_git_url(package.url) == root_url),
        None,
    )
    if root is None:
        return "rejected"
    commit = resolve(root.url, f"v{version}")
    if commit is None:
        return "unknown"
    return "verified" if commit == root.revision else "rejected"


def _write_cache(path: Path, envelope: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".remote-", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(canonical_json_bytes(envelope))
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_state(home: str | os.PathLike[str] | None) -> tuple[float, str]:
    try:
        value = json.loads(state_path(home).read_bytes())
        if isinstance(value, dict) and value.get("schema") == STATE_SCHEMA:
            return float(value["next_attempt_at"]), str(value["status"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return 0.0, "none"


def _write_state(home: str | os.PathLike[str] | None, next_attempt_at: float, status: str) -> None:
    _write_cache(
        state_path(home),
        {"schema": STATE_SCHEMA, "next_attempt_at": next_attempt_at, "status": status},
    )


def refresh_catalog(
    home: str | os.PathLike[str] | None,
    repositories: tuple[OCIRepository, ...],
    *,
    verifier: SignatureVerifier | None = None,
    force: bool = False,
    max_age_seconds: float = REFRESH_INTERVAL_SECONDS,
    retry_after_seconds: float = RETRY_AFTER_FAILURE_SECONDS,
    timeout: float = 10,
    tag_resolver: TagResolver | None = None,
) -> RefreshOutcome:
    """Fetch the newest published catalog when due; never raises for remote failures."""
    if not remote_catalog_enabled():
        return RefreshOutcome("disabled")
    now = time.time()
    next_attempt_at, last_status = _read_state(home)
    cached = load_cached(home)
    if not force and now < next_attempt_at:
        return RefreshOutcome(
            "fresh" if last_status == "ok" else "backoff",
            library=cached.library if cached is not None else None,
            manifest_digest=cached.manifest_digest if cached is not None else None,
        )
    resolve = tag_resolver or resolve_tag
    before = effective_catalog(home)
    errors: list[str] = []
    for repository in repositories:
        try:
            data, digest, sequence = fetch_catalog(repository, verifier=verifier, timeout=timeout)
            value = json.loads(data)
            generated_at, entries = _remote_entries(value)
        except (LeanRuntimeError, CatalogError, OSError, ValueError) as exc:
            errors.append(f"{repository.display}: {exc}")
            continue
        if cached is not None and sequence < cached.sequence:
            # A replayed older publication must not roll this home back.
            _write_state(home, now + max_age_seconds, "ok")
            return RefreshOutcome("kept-newer", library=repository.display, manifest_digest=digest)
        bundled = default_catalog()
        bundled_ids = {entry.id for entry in bundled.entries}
        bundled_locks = {entry.lock.lock_id for entry in bundled.entries}
        allowed = _allowed_sources(bundled)
        previous = cached.verified if cached is not None else {}
        verified: dict[str, str] = {}
        rejected: list[str] = []
        unverified: list[str] = []
        for entry in entries:
            if entry.id in bundled_ids or entry.lock.lock_id in bundled_locks:
                continue
            if previous.get(entry.id) == entry.lock.lock_id:
                verified[entry.id] = entry.lock.lock_id
                continue
            verdict = verify_entry(entry, allowed, resolve)
            if verdict == "verified":
                verified[entry.id] = entry.lock.lock_id
            elif verdict == "rejected":
                rejected.append(entry.id)
            else:
                unverified.append(entry.id)
        _write_cache(
            cache_path(home),
            {
                "schema": CACHE_SCHEMA,
                "fetched_at": now,
                "manifest_digest": digest,
                "library": repository.display,
                "sequence": sequence,
                "verified": verified,
                "catalog": value,
            },
        )
        complete = not unverified
        _write_state(
            home,
            now + (max_age_seconds if complete else retry_after_seconds),
            "ok" if complete else "partial",
        )
        after = effective_catalog(home)
        known = {entry.id for entry in before.entries}
        added = tuple(sorted(entry.id for entry in after.entries if entry.id not in known))
        unchanged = (
            cached is not None
            and digest == cached.manifest_digest
            and {entry.id for entry in after.entries} == known
        )
        return RefreshOutcome(
            "unchanged" if unchanged else "updated",
            added,
            repository.display,
            digest,
            rejected=tuple(sorted(rejected)),
            unverified=tuple(sorted(unverified)),
        )
    _write_state(home, now + retry_after_seconds, "failed")
    return RefreshOutcome("unavailable", errors=tuple(errors))


@dataclass(frozen=True, slots=True)
class CatalogPublication:
    reference: str
    manifest_digest: str
    catalog_digest: str
    entries: int
    sequence: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "manifest_digest": self.manifest_digest,
            "catalog_digest": self.catalog_digest,
            "entries": self.entries,
            "sequence": self.sequence,
        }


def publish_catalog(
    catalog_path: str | os.PathLike[str],
    repository: OCIRepository,
    *,
    sequence: int | None = None,
    credential: RegistryCredential | None = None,
) -> CatalogPublication:
    """Publish one strictly validated catalog under the mutable catalog tag.

    `sequence` must increase with every publication (CI uses the publishing
    commit's time). Republishing an older sequence is refused, so re-running an
    old workflow cannot roll the published catalog back.
    """
    catalog = Catalog.from_file(Path(catalog_path))
    selected_sequence = int(time.time()) if sequence is None else sequence
    if selected_sequence < 0:
        raise CatalogError("catalog publication sequence must be nonnegative")
    selected = credential or RegistryCredential.discover(repository)
    client = OCIRegistryClient(repository, username=selected.username, password=selected.password)
    try:
        current = client.manifest(CATALOG_REFERENCE)
    except DownloadUnavailable:
        current = None
    if current is not None:
        published = _sequence(json_object(current.data, "manifest", subject="catalog"))
        if published > selected_sequence:
            raise CatalogError(
                f"published catalog sequence {published} is newer than {selected_sequence}; "
                "refusing to roll the published catalog back"
            )
    with tempfile.TemporaryDirectory(prefix="lean-runtime-catalog-") as temporary:
        root = Path(temporary)
        layer_path = root / "catalog.json"
        layer_path.write_bytes(catalog.canonical_bytes())
        config_path = root / "config.json"
        config_path.write_bytes(
            canonical_json_bytes(
                {
                    "schema": CATALOG_SCHEMA,
                    "catalog_digest": catalog.digest,
                    "generated_at": catalog.generated_at,
                    "entries": len(catalog.entries),
                }
            )
        )
        layer = blob_descriptor_path(layer_path, CATALOG_LAYER_MEDIA_TYPE)
        config = blob_descriptor_path(config_path, CATALOG_CONFIG_MEDIA_TYPE)
        client.upload_blob(config_path, str(config["digest"]))
        client.upload_blob(layer_path, str(layer["digest"]))
    manifest = canonical_json_bytes(
        {
            "schemaVersion": 2,
            "mediaType": MANIFEST_MEDIA_TYPE,
            "config": config,
            "layers": [layer],
            "annotations": {
                "org.lean-runtime.artifact.kind": "discovery-catalog",
                "org.lean-runtime.catalog-digest": catalog.digest,
                SEQUENCE_ANNOTATION: str(selected_sequence),
            },
        }
    )
    digest = client.publish_manifest(CATALOG_REFERENCE, manifest, MANIFEST_MEDIA_TYPE)
    return CatalogPublication(
        CATALOG_REFERENCE, digest, catalog.digest, len(catalog.entries), selected_sequence
    )
