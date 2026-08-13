"""Deterministic filtering and ranking of exact environments."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from .analyzer import SourceEvidence
from .candidate import (
    AvailabilityObservation,
    Candidate,
    CandidateReason,
    DiscoveryPlan,
    ExcludedCandidate,
)
from .catalog import Catalog, CatalogEntry
from .policy import DiscoveryPolicy

FILTER_CHANNEL = "FILTER_CHANNEL"
FILTER_TOOLCHAIN_MISMATCH = "FILTER_TOOLCHAIN_MISMATCH"
FILTER_REQUIRED_PACKAGE_MISMATCH = "FILTER_REQUIRED_PACKAGE_MISMATCH"
FILTER_MODULE_ABSENT = "FILTER_MODULE_ABSENT"
FILTER_CORE_ONLY_EXTERNAL_PACKAGES = "FILTER_CORE_ONLY_EXTERNAL_PACKAGES"
RANK_EXACT_MODULE_MATCH = "RANK_EXACT_MODULE_MATCH"
RANK_ROOT_IMPORT_MATCH = "RANK_ROOT_IMPORT_MATCH"
RANK_PACKAGE_HINT = "RANK_PACKAGE_HINT"
RANK_LOCAL_AVAILABLE = "RANK_LOCAL_AVAILABLE"
RANK_LIBRARY_AVAILABLE = "RANK_LIBRARY_AVAILABLE"
RANK_STABLE_CHANNEL = "RANK_STABLE_CHANNEL"
RANK_CORE_ONLY = "RANK_CORE_ONLY"


def _package_name(reference: str) -> str:
    value = reference.rsplit("@", 1)[0]
    if value.startswith("github:"):
        value = value.removeprefix("github:").rstrip("/").rsplit("/", 1)[-1]
    return value.lower().removesuffix(".git")


def _root_package_matches(entry: CatalogEntry, root: str) -> bool:
    root_lower = root.lower()
    return root_lower in entry.package_names or any(
        package.root_module == root for package in entry.lock.packages
    )


class Planner:
    """Plan a bounded search without opening or executing environments."""

    def plan(
        self,
        evidence: SourceEvidence,
        catalog: Catalog,
        policy: DiscoveryPolicy,
        availability: Mapping[str, AvailabilityObservation] | None = None,
    ) -> DiscoveryPlan:
        observations = availability or {}
        if evidence.lock_hint is not None:
            return DiscoveryPlan(
                evidence=evidence,
                catalog_digest=catalog.digest,
                policy=policy,
                candidates=(),
                excluded=(),
                explicit_lock=evidence.lock_hint,
                total_plausible_candidates=0,
            )

        required_packages = frozenset(_package_name(item) for item in evidence.package_hints)
        ranked: list[tuple[int, int, float, str, CatalogEntry, tuple[CandidateReason, ...]]] = []
        excluded: list[ExcludedCandidate] = []
        for entry in catalog.entries:
            filter_reasons: list[CandidateReason] = []
            if entry.channel not in policy.channels:
                filter_reasons.append(
                    CandidateReason(FILTER_CHANNEL, f"channel {entry.channel!r} is not enabled")
                )
            if evidence.toolchain_hint and entry.toolchain != evidence.toolchain_hint:
                filter_reasons.append(
                    CandidateReason(
                        FILTER_TOOLCHAIN_MISMATCH,
                        f"requires {evidence.toolchain_hint}, candidate uses {entry.toolchain}",
                    )
                )
            missing_packages = sorted(required_packages - entry.package_names)
            if missing_packages:
                filter_reasons.append(
                    CandidateReason(
                        FILTER_REQUIRED_PACKAGE_MISMATCH,
                        f"missing required packages: {', '.join(missing_packages)}",
                    )
                )
            missing_modules = sorted(set(evidence.imports) - entry.modules)
            if missing_modules:
                filter_reasons.append(
                    CandidateReason(
                        FILTER_MODULE_ABSENT,
                        f"module inventory lacks: {', '.join(missing_modules)}",
                    )
                )
            if evidence.appears_core_only and entry.lock.packages:
                filter_reasons.append(
                    CandidateReason(
                        FILTER_CORE_ONLY_EXTERNAL_PACKAGES,
                        "core-only source does not require external packages",
                    )
                )
            if filter_reasons:
                excluded.append(
                    ExcludedCandidate(entry.id, entry.lock.lock_id, tuple(filter_reasons))
                )
                continue

            score = 0
            reasons: list[CandidateReason] = []
            if evidence.imports:
                score += 100
                reasons.append(
                    CandidateReason(
                        RANK_EXACT_MODULE_MATCH,
                        f"all {len(evidence.imports)} imported modules are present",
                    )
                )
            matching_roots = sorted(
                root for root in evidence.root_namespaces if _root_package_matches(entry, root)
            )
            if matching_roots:
                score += 40
                reasons.append(
                    CandidateReason(
                        RANK_ROOT_IMPORT_MATCH,
                        f"root imports match packages: {', '.join(matching_roots)}",
                    )
                )
            if required_packages:
                score += 25
                reasons.append(
                    CandidateReason(RANK_PACKAGE_HINT, "all explicit package hints match")
                )
            observation = observations.get(entry.lock.lock_id, AvailabilityObservation())
            if policy.prefer_local and observation.local:
                score += 15
                reasons.append(CandidateReason(RANK_LOCAL_AVAILABLE, "environment is local"))
            if policy.prefer_downloadable and observation.downloadable:
                score += 10
                reasons.append(
                    CandidateReason(RANK_LIBRARY_AVAILABLE, "environment is downloadable")
                )
            if entry.channel == "stable":
                score += 5
                reasons.append(CandidateReason(RANK_STABLE_CHANNEL, "stable catalog channel"))
            if evidence.appears_core_only and not entry.lock.packages:
                score += 100
                reasons.append(CandidateReason(RANK_CORE_ONLY, "source appears core-only"))
            created_at = datetime.fromisoformat(entry.created_at.replace("Z", "+00:00"))
            ranked.append(
                (
                    -score,
                    len(entry.lock.packages),
                    -created_at.timestamp(),
                    entry.id,
                    entry,
                    tuple(reasons),
                )
            )

        # Prefer the smallest exact environment when otherwise-equivalent
        # candidates cover the same imports. This keeps an extension package
        # such as LeanCert discoverable without making ordinary Mathlib files
        # download and open the larger Mathlib-plus-LeanCert environment.
        ranked.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
        selected = ranked[: policy.max_candidates]
        candidates = tuple(
            Candidate(rank=index, entry=item[4], score=-item[0], reasons=item[5])
            for index, item in enumerate(selected, start=1)
        )
        excluded.sort(key=lambda item: item.entry_id)
        return DiscoveryPlan(
            evidence=evidence,
            catalog_digest=catalog.digest,
            policy=policy,
            candidates=candidates,
            excluded=tuple(excluded),
            total_plausible_candidates=len(ranked),
        )
