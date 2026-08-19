"""Pure catalog filtering and deterministic candidate ordering."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from itertools import groupby

from .analyzer import SourceEvidence
from .candidate import Candidate, CandidateReason, DiscoveryPlan, ExcludedCandidate
from .catalog import Catalog, CatalogEntry
from .policy import DiscoveryPolicy

FILTER_CHANNEL = "FILTER_CHANNEL"
FILTER_TOOLCHAIN_MISMATCH = "FILTER_TOOLCHAIN_MISMATCH"
FILTER_REQUIRED_PACKAGE_MISMATCH = "FILTER_REQUIRED_PACKAGE_MISMATCH"
FILTER_CORE_ONLY_EXTERNAL_PACKAGES = "FILTER_CORE_ONLY_EXTERNAL_PACKAGES"
DEMOTE_MODULE_INVENTORY_MISS = "DEMOTE_MODULE_INVENTORY_MISS"
MATCH_MODULE_INVENTORY = "MATCH_MODULE_INVENTORY"
MATCH_ROOT_IMPORT = "MATCH_ROOT_IMPORT"
RANK_LOCAL_AVAILABLE = "RANK_LOCAL_AVAILABLE"
RANK_EXACT_DECISION = "RANK_EXACT_DECISION"
RANK_HEADER_DECISION = "RANK_HEADER_DECISION"
SKIP_EXACT_REJECTION = "SKIP_EXACT_REJECTION"

_CORE_ROOTS = frozenset({"Init", "Lean", "Std"})


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


def _has_reason(candidate: Candidate, code: str) -> bool:
    return any(reason.code == code for reason in candidate.reasons)


def _family(candidate: Candidate) -> tuple[str, ...]:
    packages = tuple(sorted(candidate.entry.package_names))
    return packages or ("<core>",)


def _diversify(
    candidates: list[Candidate],
    priority: Callable[[Candidate], object],
) -> list[Candidate]:
    """Round-robin environment families within otherwise-equal priority tiers."""

    result: list[Candidate] = []
    for _, tier_items in groupby(candidates, key=priority):
        families: dict[tuple[str, ...], list[Candidate]] = {}
        for candidate in tier_items:
            families.setdefault(_family(candidate), []).append(candidate)
        queues = list(families.values())
        while queues:
            remaining: list[list[Candidate]] = []
            for queue in queues:
                result.append(queue.pop(0))
                if queue:
                    remaining.append(queue)
            queues = remaining
    return result


class Planner:
    """Build a pure static plan, then order it using explicit local history."""

    def plan(
        self,
        evidence: SourceEvidence,
        catalog: Catalog,
        policy: DiscoveryPolicy,
    ) -> DiscoveryPlan:
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
        external_imports = tuple(
            module for module in evidence.imports if module.split(".", 1)[0] not in _CORE_ROOTS
        )
        inventory_matches = catalog.entry_ids_for_modules(external_imports)
        package_matches = catalog.entry_ids_for_packages(required_packages)
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
            if required_packages and entry.id not in package_matches:
                missing = sorted(required_packages - entry.package_names)
                filter_reasons.append(
                    CandidateReason(
                        FILTER_REQUIRED_PACKAGE_MISMATCH,
                        f"missing required packages: {', '.join(missing)}",
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

            reasons: list[CandidateReason] = []
            inventory_miss = bool(external_imports) and entry.id not in inventory_matches
            if inventory_miss:
                missing_modules = sorted(set(external_imports) - entry.modules)
                reasons.append(
                    CandidateReason(
                        DEMOTE_MODULE_INVENTORY_MISS,
                        f"declared inventory lacks: {', '.join(missing_modules)}",
                    )
                )
            elif external_imports:
                reasons.append(
                    CandidateReason(
                        MATCH_MODULE_INVENTORY,
                        "declared inventory contains every external import",
                    )
                )
            matching_roots = sorted(
                root for root in evidence.root_namespaces if _root_package_matches(entry, root)
            )
            if matching_roots:
                reasons.append(
                    CandidateReason(
                        MATCH_ROOT_IMPORT,
                        f"root imports match packages: {', '.join(matching_roots)}",
                    )
                )
            ranked.append(
                (
                    int(inventory_miss),
                    len(entry.lock.packages),
                    -entry.created_timestamp,
                    entry.id,
                    entry,
                    tuple(reasons),
                )
            )

        ranked.sort(key=lambda item: item[:4])
        static_candidates = [
            Candidate(rank=index, entry=item[4], reasons=item[5])
            for index, item in enumerate(ranked, start=1)
        ]
        static_candidates = _diversify(
            static_candidates,
            lambda candidate: _has_reason(candidate, DEMOTE_MODULE_INVENTORY_MISS),
        )
        candidates = tuple(
            replace(candidate, rank=index)
            for index, candidate in enumerate(static_candidates, start=1)
        )
        excluded.sort(key=lambda item: item.entry_id)
        return DiscoveryPlan(
            evidence=evidence,
            catalog_digest=catalog.digest,
            policy=policy,
            candidates=candidates,
            excluded=tuple(excluded),
            total_plausible_candidates=len(candidates),
        )

    def order(
        self,
        plan: DiscoveryPlan,
        *,
        local_lock_ids: frozenset[str] = frozenset(),
        preferred_lock_id: str | None = None,
        preferred_exact: bool = False,
        rejected_lock_ids: frozenset[str] = frozenset(),
    ) -> DiscoveryPlan:
        """Apply runtime/history facts without changing static plausibility."""

        candidates: list[Candidate] = []
        excluded = list(plan.excluded)
        # Exact negative history avoids repeating dead ends while unexplored
        # candidates remain. If every candidate was rejected previously, keep
        # the plan intact so this invocation can still return a fresh compiler
        # diagnostic rather than an opaque cache-only failure.
        effective_rejections = rejected_lock_ids
        if plan.candidates and all(
            candidate.entry.lock.lock_id in rejected_lock_ids for candidate in plan.candidates
        ):
            effective_rejections = frozenset()
        for candidate in plan.candidates:
            lock_id = candidate.entry.lock.lock_id
            if lock_id in effective_rejections:
                excluded.append(
                    ExcludedCandidate(
                        candidate.entry.id,
                        lock_id,
                        (
                            CandidateReason(
                                SKIP_EXACT_REJECTION,
                                "Lean previously rejected this exact source under this lock",
                            ),
                        ),
                    )
                )
                continue
            reasons = list(candidate.reasons)
            if lock_id == preferred_lock_id:
                reasons.append(
                    CandidateReason(
                        RANK_EXACT_DECISION if preferred_exact else RANK_HEADER_DECISION,
                        (
                            "this exact source previously compiled under the lock"
                            if preferred_exact
                            else "a source with the same context previously compiled under the lock"
                        ),
                    )
                )
            if lock_id in local_lock_ids:
                reasons.append(CandidateReason(RANK_LOCAL_AVAILABLE, "environment is local"))
            candidates.append(replace(candidate, reasons=tuple(reasons)))

        def key(candidate: Candidate) -> tuple[object, ...]:
            lock_id = candidate.entry.lock.lock_id
            exact = preferred_exact and lock_id == preferred_lock_id
            header = not preferred_exact and lock_id == preferred_lock_id
            return (
                not exact,
                lock_id not in local_lock_ids,
                not header,
                _has_reason(candidate, DEMOTE_MODULE_INVENTORY_MISS),
                len(candidate.entry.lock.packages),
                -candidate.entry.created_timestamp,
                candidate.entry.id,
            )

        candidates.sort(key=key)
        candidates = _diversify(candidates, lambda candidate: key(candidate)[:4])
        reranked = tuple(
            replace(candidate, rank=index) for index, candidate in enumerate(candidates, start=1)
        )
        return replace(
            plan,
            candidates=reranked,
            excluded=tuple(sorted(excluded, key=lambda item: item.entry_id)),
            total_plausible_candidates=len(reranked),
        )
