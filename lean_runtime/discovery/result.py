"""Authoritative discovery results and candidate attempt records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ..lockfiles import EnvironmentLock
from ..models import ExecutionResult
from .candidate import Candidate, DiscoveryPlan
from .errors import DiscoveryError

RESULT_SCHEMA = "lean-runtime.discovery.result/v1"
DiscoveryStatus = Literal["found", "not_found", "invalid_request", "cancelled", "failed"]
DiscoveryConfidence = Literal["compiled", "exhausted", "inconclusive"]
DiscoveryOutcome = Literal[
    "found",
    "source_rejected",
    "no_candidate",
    "inconclusive",
    "cancelled",
    "failed",
]
DiscoveryCompletion = Literal[
    "complete",
    "candidate_limit",
    "time_limit",
    "acquisition_limit",
]
Acquisition = Literal["local", "downloaded", "source_built", "unknown"]
AttemptStatus = Literal[
    "compiled",
    "lean_rejected",
    "environment_unavailable",
    "environment_integrity_failure",
    "timeout",
    "cancelled",
    "runtime_error",
]


@dataclass(frozen=True, slots=True)
class ResultDiagnostic:
    code: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class CandidateAttempt:
    candidate_id: str
    lock_id: str
    status: AttemptStatus
    duration_seconds: float
    acquisition: Acquisition = "unknown"
    environment_id: str | None = None
    execution_id: str | None = None
    execution_result: ExecutionResult | None = None
    diagnostics: tuple[ResultDiagnostic, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.duration_seconds < 0:
            raise ValueError("candidate attempt duration must be nonnegative")
        if self.status == "compiled" and (
            self.execution_result is None or not self.execution_result.ok
        ):
            raise ValueError("a compiled attempt requires a successful Runtime execution")

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "lock_id": self.lock_id,
            "status": self.status,
            "duration_seconds": self.duration_seconds,
            "acquisition": self.acquisition,
            "environment_id": self.environment_id,
            "execution_id": self.execution_id,
            "execution_result": (
                self.execution_result.to_dict() if self.execution_result is not None else None
            ),
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    status: DiscoveryStatus
    confidence: DiscoveryConfidence
    plan: DiscoveryPlan
    attempts: tuple[CandidateAttempt, ...]
    duration_seconds: float
    outcome: DiscoveryOutcome
    completion: DiscoveryCompletion
    selected_candidate: Candidate | None = None
    lock: EnvironmentLock | None = None
    execution_result: ExecutionResult | None = None
    diagnostics: tuple[ResultDiagnostic, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.duration_seconds < 0:
            raise ValueError("discovery duration must be nonnegative")
        if self.status == "found":
            if (
                self.confidence != "compiled"
                or self.outcome != "found"
                or self.completion != "complete"
                or self.selected_candidate is None
                or self.lock is None
                or self.execution_result is None
                or not self.execution_result.ok
            ):
                raise ValueError("a found result requires a successful compiled execution")
            if self.lock.lock_id != self.selected_candidate.entry.lock.lock_id:
                raise ValueError("selected lock does not match the selected candidate")
            if (
                not self.attempts
                or self.attempts[-1].status != "compiled"
                or self.attempts[-1].candidate_id != self.selected_candidate.entry.id
                or self.attempts[-1].lock_id != self.lock.lock_id
            ):
                raise ValueError("a found result must end with its selected compiled attempt")
        elif self.confidence == "compiled":
            raise ValueError("only a found result may have compiled confidence")
        elif self.outcome == "inconclusive" and self.confidence != "inconclusive":
            raise ValueError("an inconclusive outcome requires inconclusive confidence")
        elif self.completion != "complete" and self.outcome != "inconclusive":
            raise ValueError("a bounded incomplete search must have inconclusive outcome")
        elif (
            self.selected_candidate is not None
            or self.lock is not None
            or self.execution_result is not None
        ):
            raise ValueError("an unsuccessful result cannot select an environment")

    @property
    def lock_id(self) -> str | None:
        return self.lock.lock_id if self.lock is not None else None

    @property
    def environment_id(self) -> str | None:
        if self.execution_result is not None and self.execution_result.environment_id is not None:
            value = self.execution_result.environment_id
            return value if isinstance(value, str) else None
        compiled = next((item for item in self.attempts if item.status == "compiled"), None)
        return compiled.environment_id if compiled is not None else None

    @property
    def rejection_attempt(self) -> CandidateAttempt | None:
        """Return the highest-ranked attempt for which Lean rejected the source."""
        return next(
            (
                attempt
                for attempt in self.attempts
                if attempt.status == "lean_rejected" and attempt.execution_result is not None
            ),
            None,
        )

    @property
    def best_rejection(self) -> CandidateAttempt | None:
        return self.rejection_attempt

    def raise_for_error(self) -> DiscoveryResult:
        if self.status != "found":
            details = [item.detail for item in self.diagnostics]
            detail = ": ".join(dict.fromkeys(details)) if details else self.status
            raise DiscoveryError(f"Lean environment discovery did not succeed: {detail}")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RESULT_SCHEMA,
            "status": self.status,
            "outcome": self.outcome,
            "completion": self.completion,
            "confidence": self.confidence,
            "catalog_digest": self.plan.catalog_digest,
            "selected_candidate": (
                self.selected_candidate.to_dict() if self.selected_candidate is not None else None
            ),
            "lock": self.lock.to_dict() if self.lock is not None else None,
            "lock_id": self.lock_id,
            "environment_id": self.environment_id,
            "execution_result": (
                self.execution_result.to_dict() if self.execution_result is not None else None
            ),
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "duration_seconds": self.duration_seconds,
            "plan": self.plan.to_dict(),
        }
