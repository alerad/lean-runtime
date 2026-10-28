from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import make_entry

from lean_runtime import EnvironmentLock, LockedPackage
from lean_runtime.discovery import Catalog, CatalogEntry, default_catalog, remote
from lean_runtime.errors import DownloadUnavailable
from lean_runtime.oci import ManifestResponse, OCIRepository

REPOSITORY = OCIRepository.parse("oci://registry.example/lean/cache")
MATHLIB = "https://github.com/leanprover-community/mathlib4.git"


@pytest.fixture(autouse=True)
def _remote_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(remote.SOURCE_ENVIRONMENT)
    remote._EFFECTIVE.clear()


def _entry(
    entry_id: str,
    marker: str,
    *,
    url: str = MATHLIB,
    extra: tuple[str, ...] = (),
) -> CatalogEntry:
    """A Mathlib release entry whose packages come from the given sources."""
    packages = [
        LockedPackage(
            name="mathlib",
            url=url,
            revision=marker * 40,
            tree_hash=marker * 40,
            source_id=f"source_{marker * 64}",
            root_module="Mathlib",
        ),
        *(
            LockedPackage(
                name=f"dep{index}",
                url=dependency,
                revision="e" * 40,
                tree_hash="e" * 40,
                source_id=f"source_{'e' * 63}{index}",
                root_module=f"Dep{index}",
            )
            for index, dependency in enumerate(extra)
        ),
    ]
    lock = EnvironmentLock(
        toolchain="leanprover/lean4:v4.99.0",
        spec_digest=f"spec_{marker * 64}",
        root_lakefile=f'name = "fixture-{marker}"\n',
        root_module="",
        manifest={"version": "1.1.0", "packagesDir": ".lake/packages", "packages": []},
        packages=tuple(packages),
    )
    return CatalogEntry(
        id=entry_id,
        channel="stable",
        toolchain=lock.toolchain,
        lock=lock,
        created_at="2026-10-01T00:00:00Z",
    )


def _tags(**tags: str):
    """A tag resolver over fixed `tag=commit` pairs; unknown tags are absent."""

    def resolve(url: str, tag: str) -> str | None:
        return tags.get(tag.replace(".", "_"), "")

    return resolve


def _unreachable(url: str, tag: str) -> str | None:
    return None


def _published(*entries: Any, generated_at: str = "2026-10-01T00:00:00Z") -> Catalog:
    return Catalog(generated_at=generated_at, entries=tuple(entries))


class FakeRegistry:
    """In-memory OCI registry shared by every client the module creates."""

    blobs: dict[str, bytes] = {}
    manifests: dict[str, bytes] = {}
    available = True
    requests = 0

    def __init__(self, repository: OCIRepository, **_kwargs: object) -> None:
        self.repository = repository

    def upload_blob(self, path: Path, digest: str) -> None:
        self.blobs[digest] = path.read_bytes()

    def publish_manifest(self, reference: str, data: bytes, media_type: str) -> str:
        self.manifests[reference] = data
        return _digest(data)

    def manifest(self, reference: str) -> ManifestResponse:
        type(self).requests += 1
        if not self.available or reference not in self.manifests:
            raise DownloadUnavailable("registry unavailable")
        data = self.manifests[reference]
        return ManifestResponse(data, remote.MANIFEST_MEDIA_TYPE, _digest(data))

    def read_blob(self, descriptor: dict[str, Any], *, limit: int) -> bytes:
        return self.blobs[descriptor["digest"]]


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> type[FakeRegistry]:
    FakeRegistry.blobs = {}
    FakeRegistry.manifests = {}
    FakeRegistry.available = True
    FakeRegistry.requests = 0
    monkeypatch.setattr(remote, "OCIRegistryClient", FakeRegistry)
    return FakeRegistry


def _publish(tmp_path: Path, catalog: Catalog, sequence: int = 100) -> None:
    path = tmp_path / f"published-{catalog.digest[7:19]}-{sequence}.json"
    catalog.write(path)
    credential = remote.RegistryCredential(None, None, "anonymous")
    remote.publish_catalog(path, REPOSITORY, sequence=sequence, credential=credential)


def _refresh(home: Path, *, resolve: Any = None, force: bool = True) -> remote.RefreshOutcome:
    return remote.refresh_catalog(
        home, (REPOSITORY,), force=force, tag_resolver=resolve or _tags(v99_0_0="d" * 40)
    )


def _ids(home: Path) -> set[str]:
    return {entry.id for entry in remote.effective_catalog(home).entries}


def test_merge_is_additive_and_never_redefines_bundled_entries() -> None:
    bundled = _published(make_entry("mathlib-v1", "a", packages=("mathlib",)))
    redefined = make_entry("mathlib-v1", "b", packages=("mathlib",))
    duplicate_lock = make_entry("alias", "a", packages=("mathlib",))
    added = make_entry("mathlib-v2", "c", packages=("mathlib",))
    merged = remote.merge_catalogs(
        bundled, (redefined, duplicate_lock, added), "2026-10-01T00:00:00Z"
    )
    assert [entry.id for entry in merged.entries] == ["mathlib-v1", "mathlib-v2"]
    assert merged.entries[0].lock.lock_id == bundled.entries[0].lock.lock_id
    assert remote.merge_catalogs(bundled, (redefined,), "2026-10-01T00:00:00Z") is bundled


def test_remote_entries_skip_unrepresentable_entries() -> None:
    good = make_entry("mathlib-v2", "c", packages=("mathlib",)).to_dict()
    future = {**good, "id": "mathlib-v3", "lock": {"schema": "future"}}
    generated_at, entries = remote._remote_entries(
        {
            "schema": remote.CATALOG_SCHEMA,
            "generated_at": "2026-10-01T00:00:00Z",
            "entries": [good, future, "junk"],
        }
    )
    assert generated_at == "2026-10-01T00:00:00Z"
    assert [entry.id for entry in entries] == ["mathlib-v2"]


def test_published_catalog_round_trips_into_the_effective_catalog(
    tmp_path: Path, registry: type[FakeRegistry]
) -> None:
    _publish(tmp_path, _published(*default_catalog().entries, _entry("mathlib-v99.0.0", "d")))
    home = tmp_path / "home"

    outcome = _refresh(home)

    assert outcome.status == "updated"
    assert outcome.added == ("mathlib-v99.0.0",)
    assert _ids(home) == {*(entry.id for entry in default_catalog().entries), "mathlib-v99.0.0"}
    assert _refresh(home, force=False).status == "fresh"
    assert _refresh(home).status == "unchanged"


def test_entries_from_untrusted_sources_are_never_offered(
    tmp_path: Path, registry: type[FakeRegistry]
) -> None:
    hijack = _entry("mathlib-v99.0.0", "d", url="https://github.com/attacker/mathlib4.git")
    smuggled = _entry("core-v99", "f", extra=("https://example.invalid/payload.git",))
    lean_org = _entry("core-v98", "a", extra=("https://github.com/leanprover/new-dependency",))
    _publish(tmp_path, _published(hijack, smuggled, lean_org))
    home = tmp_path / "home"

    outcome = _refresh(home)

    assert outcome.rejected == ("core-v99", "mathlib-v99.0.0")
    assert "core-v98" in _ids(home)
    assert not {"mathlib-v99.0.0", "core-v99"} & _ids(home)


def test_release_entries_must_pin_the_commit_their_tag_names(
    tmp_path: Path, registry: type[FakeRegistry]
) -> None:
    _publish(tmp_path, _published(_entry("mathlib-v99.0.0", "d"), _entry("mathlib-v98.0.0", "b")))
    home = tmp_path / "home"

    # v99.0.0 matches its tag; v98.0.0's tag names a different commit.
    outcome = _refresh(home, resolve=_tags(v99_0_0="d" * 40, v98_0_0="c" * 40))

    assert outcome.added == ("mathlib-v99.0.0",)
    assert outcome.rejected == ("mathlib-v98.0.0",)


def test_unverifiable_entries_wait_and_are_retried(
    tmp_path: Path, registry: type[FakeRegistry]
) -> None:
    _publish(tmp_path, _published(_entry("mathlib-v99.0.0", "d")))
    home = tmp_path / "home"

    first = _refresh(home, resolve=_unreachable)
    assert first.unverified == ("mathlib-v99.0.0",)
    assert "mathlib-v99.0.0" not in _ids(home)
    assert _refresh(home, resolve=_unreachable, force=False).status == "backoff"

    second = _refresh(home)
    assert second.added == ("mathlib-v99.0.0",)
    # Verified entries are remembered; a later unreachable tag service does not hide them.
    assert _refresh(home, resolve=_unreachable).unverified == ()
    assert "mathlib-v99.0.0" in _ids(home)


def test_failed_refresh_backs_off_and_keeps_the_retained_catalog(
    tmp_path: Path, registry: type[FakeRegistry]
) -> None:
    _publish(tmp_path, _published(_entry("mathlib-v99.0.0", "d")))
    home = tmp_path / "home"
    _refresh(home)
    registry.available = False

    outcome = _refresh(home)
    requests = registry.requests

    assert outcome.status == "unavailable"
    assert outcome.errors
    assert "mathlib-v99.0.0" in _ids(home)
    assert _refresh(home, force=False).status == "backoff"
    assert registry.requests == requests


def test_a_replayed_older_publication_is_refused(
    tmp_path: Path, registry: type[FakeRegistry]
) -> None:
    home = tmp_path / "home"
    _publish(tmp_path, _published(_entry("mathlib-v99.0.0", "d")), sequence=200)
    _refresh(home)
    replay = _published(generated_at="2026-12-01T00:00:00Z")
    replay.write(tmp_path / "replay.json")
    registry.manifests.clear()  # an attacker or stale mirror serving an old publication
    _publish(tmp_path, replay, sequence=100)

    assert _refresh(home).status == "kept-newer"
    assert "mathlib-v99.0.0" in _ids(home)


def test_a_newer_publication_retracts_entries(tmp_path: Path, registry: type[FakeRegistry]) -> None:
    home = tmp_path / "home"
    _publish(tmp_path, _published(_entry("mathlib-v99.0.0", "d")), sequence=100)
    _refresh(home)
    # The retraction keeps the old generated_at, as a plain git revert would.
    _publish(tmp_path, _published(), sequence=200)

    assert _refresh(home).status == "updated"
    assert "mathlib-v99.0.0" not in _ids(home)


def test_publishing_never_rolls_the_registry_back(
    tmp_path: Path, registry: type[FakeRegistry]
) -> None:
    _publish(tmp_path, _published(_entry("mathlib-v99.0.0", "d")), sequence=200)
    with pytest.raises(remote.CatalogError, match="newer"):
        _publish(tmp_path, _published(), sequence=100)


def test_manifest_with_unexpected_media_types_is_refused(
    tmp_path: Path, registry: type[FakeRegistry]
) -> None:
    registry.manifests[remote.CATALOG_REFERENCE] = json.dumps(
        {"mediaType": remote.MANIFEST_MEDIA_TYPE, "config": {"mediaType": "other"}, "layers": []}
    ).encode()
    assert _refresh(tmp_path / "home").status == "unavailable"
    assert remote.effective_catalog(tmp_path / "home") is default_catalog()


def test_signature_policy_applies_to_the_catalog_manifest(
    tmp_path: Path, registry: type[FakeRegistry]
) -> None:
    _publish(tmp_path, _published(_entry("mathlib-v99.0.0", "d")))

    class Rejecting:
        def verify(self, repository: OCIRepository, digest: str) -> None:
            raise DownloadUnavailable("signature verification failed")

    outcome = remote.refresh_catalog(tmp_path / "home", (REPOSITORY,), verifier=Rejecting())
    assert outcome.status == "unavailable"


def test_bundled_pin_disables_remote_entries(
    tmp_path: Path, registry: type[FakeRegistry], monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish(tmp_path, _published(_entry("mathlib-v99.0.0", "d")))
    home = tmp_path / "home"
    _refresh(home)
    monkeypatch.setenv(remote.SOURCE_ENVIRONMENT, "bundled")
    assert remote.effective_catalog(home) is default_catalog()
    assert _refresh(home).status == "disabled"


def test_resolve_tag_distinguishes_absent_from_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    peeled = f"{'1' * 40}\trefs/tags/v1\n{'2' * 40}\trefs/tags/v1^{{}}\n"
    responses = iter(
        [
            SimpleNamespace(ok=True, stdout=peeled),
            SimpleNamespace(ok=True, stdout=""),
            SimpleNamespace(ok=False, stdout=""),
        ]
    )
    monkeypatch.setattr(remote, "run_git", lambda *_a, **_k: next(responses))
    assert remote.resolve_tag(MATHLIB, "v1") == "2" * 40
    assert remote.resolve_tag(MATHLIB, "v1") == ""
    assert remote.resolve_tag(MATHLIB, "v1") is None
