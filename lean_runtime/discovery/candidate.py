"""Candidate and planning result models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .analyzer import SourceEvidence
from .catalog import CatalogEntry
from .policy import DiscoveryPolicy

PLAN_SCHEMA = "lean-runtime.discovery.plan/v1"


@dataclass(frozen=True, slots=True)
class CandidateReason:
    code: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class Candidate:
    rank: int
    entry: CatalogEntry
    reasons: tuple[CandidateReason, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "entry_id": self.entry.id,
            "lock_id": self.entry.lock.lock_id,
            "toolchain": self.entry.toolchain,
            "channel": self.entry.channel,
            "reasons": [reason.to_dict() for reason in self.reasons],
        }


@dataclass(frozen=True, slots=True)
class ExcludedCandidate:
    entry_id: str
    lock_id: str
    reasons: tuple[CandidateReason, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "lock_id": self.lock_id,
            "reasons": [reason.to_dict() for reason in self.reasons],
        }


@dataclass(frozen=True, slots=True)
class DiscoveryPlan:
    """A deterministic heuristic plan. It is not evidence of compilation."""

    evidence: SourceEvidence
    catalog_digest: str
    policy: DiscoveryPolicy
    candidates: tuple[Candidate, ...]
    excluded: tuple[ExcludedCandidate, ...]
    explicit_lock: str | None = None
    total_plausible_candidates: int = 0

    @property
    def truncated(self) -> bool:
        return self.total_plausible_candidates > len(self.planned_candidates)

    @property
    def planned_candidates(self) -> tuple[Candidate, ...]:
        """Candidates the bounded executor is permitted to start."""

        return self.candidates[: self.policy.max_candidates]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PLAN_SCHEMA,
            "confidence": "heuristic_only",
            "catalog_digest": self.catalog_digest,
            "evidence": self.evidence.to_dict(),
            "policy": self.policy.to_dict(),
            "explicit_lock": self.explicit_lock,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "excluded": [candidate.to_dict() for candidate in self.excluded],
            "total_plausible_candidates": self.total_plausible_candidates,
            "candidate_limit": self.policy.max_candidates,
            "truncated": self.truncated,
        }
