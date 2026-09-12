"""Published artifact size references, never evidence authorizing reuse."""

from __future__ import annotations

import re
import threading
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from ._project_identity import canonical_git_url
from .errors import LeanRuntimeError
from .oci import OCIEnvironmentCache

if TYPE_CHECKING:
    from .discovery.catalog import Catalog


def _catalog() -> Catalog:
    from .discovery.defaults import default_catalog

    return default_catalog()


@dataclass(frozen=True, slots=True)
class PublishedPackageSize:
    package: str
    url: str
    revision: str
    subdir: str | None
    toolchain: str
    artifact_bytes: int
    packed_bytes: int
    registry: str
    lock_id: str
    config_digest: str
    capability: str = "check"
    layout: str = "lean-check-artifacts"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SizeReferenceRequest:
    entry: dict[str, Any]
    toolchain: str


@dataclass(frozen=True, slots=True)
class SizeReferenceUse:
    action_ids: tuple[str, ...]
    projects: tuple[str, ...]
    request: SizeReferenceRequest
    sample: PublishedPackageSize

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_ids": list(self.action_ids),
            "projects": list(self.projects),
            "requested_package": str(self.request.entry["name"]),
            "requested_revision": self.request.entry["rev"],
            "requested_toolchain": self.request.toolchain,
            "included_in_totals": False,
            "relationship": reference_relationship(self.sample, self.request),
            "different_revision": self.sample.revision != self.request.entry["rev"],
            "different_toolchain": self.sample.toolchain != self.request.toolchain,
            "sample": self.sample.to_dict(),
        }


def release_distance(first: str, second: str) -> int | None:
    a, b = re.search(r"v(\d+)\.(\d+)\.(\d+)", first), re.search(r"v(\d+)\.(\d+)\.(\d+)", second)
    if a is None or b is None or a[1] != b[1]:
        return None
    return abs(int(a[2]) - int(b[2]))


def reference_relationship(sample: PublishedPackageSize, request: SizeReferenceRequest) -> str:
    if sample.revision == request.entry.get("rev"):
        return (
            "same_revision_same_toolchain"
            if sample.toolchain == request.toolchain
            else "same_revision_other_toolchain"
        )
    distance = release_distance(sample.toolchain, request.toolchain)
    return "nearby_release" if distance is not None and distance <= 2 else "distant_release"


def published_package_sizes(
    libraries: tuple[OCIEnvironmentCache, ...],
    requests: tuple[SizeReferenceRequest, ...] | None = None,
    *,
    cancel: threading.Event | None = None,
) -> tuple[PublishedPackageSize, ...]:
    """Read verified OCI metadata only; never pull packs or install toolchains.

    Catalog membership is discovery, not proof that a publication exists. Only
    successfully validated manifests/configs supply size samples. Unavailable
    registries simply leave estimation incomplete.
    """
    # Discovery imports Runtime; load it only after module initialization.

    samples: list[PublishedPackageSize] = []
    for entry in sorted(_catalog().entries, key=lambda e: e.created_timestamp, reverse=True):
        if cancel is not None and cancel.is_set():
            break
        if requests is not None and not any(
            canonical_git_url(package.url) == canonical_git_url(str(request.entry["url"]))
            and (package.subdir or "") == (request.entry.get("subDir") or "")
            for package in entry.lock.packages
            for request in requests
        ):
            continue
        if not entry.lock.packages:
            continue
        for library in libraries:
            try:
                # Empty roots select no frames, avoiding artifact reads/downloads.
                plan = library.plan_capsule(entry.lock, (), capabilities=frozenset())
            except (LeanRuntimeError, OSError, ValueError):
                continue
            for package in entry.lock.packages:
                artifacts = {
                    artifact.path: artifact
                    for module in plan.capsule.modules
                    if module.package == package.name
                    for artifact in module.artifacts
                    if artifact.capability == "check"
                }
                packs = [
                    p
                    for p, _ in plan.packs
                    if p.package == package.name and p.capability == "check"
                ]
                if not artifacts or not packs:
                    continue
                samples.append(
                    PublishedPackageSize(
                        package.name,
                        package.url,
                        package.revision,
                        package.subdir,
                        entry.toolchain,
                        sum(a.size for a in artifacts.values()),
                        sum(p.size for p in packs),
                        library.repository.display,
                        entry.lock.lock_id,
                        str(plan.config_descriptor["digest"]),
                    )
                )
            break
        if requests and all(
            any(
                sample.revision == request.entry["rev"]
                and sample.toolchain == request.toolchain
                and canonical_git_url(sample.url) == canonical_git_url(str(request.entry["url"]))
                and (sample.subdir or "") == (request.entry.get("subDir") or "")
                for sample in samples
            )
            for request in requests
        ):
            break
    return tuple(samples)


def select_size_reference(
    samples: tuple[PublishedPackageSize, ...],
    entry: dict[str, Any],
    toolchain: str,
) -> PublishedPackageSize | None:
    candidates = [
        s
        for s in samples
        if canonical_git_url(s.url) == canonical_git_url(str(entry.get("url", "")))
        and s.capability == "check"
        and s.layout == "lean-check-artifacts"
        and (s.subdir or "") == (entry.get("subDir") or "")
        and (
            s.revision == entry.get("rev")
            or s.toolchain == toolchain
            or (
                release_distance(s.toolchain, toolchain) is not None
                and (release_distance(s.toolchain, toolchain) or 0) <= 2
            )
        )
    ]
    # Stable ordering retains the newest catalog publication on a tie. A
    # matching source revision is preferable to a different revision reference.
    return max(
        candidates,
        key=lambda s: (
            s.revision == entry.get("rev"),
            s.toolchain == toolchain,
            -(release_distance(s.toolchain, toolchain) or 0),
        ),
        default=None,
    )
