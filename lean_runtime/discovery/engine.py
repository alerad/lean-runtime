"""Bounded authoritative candidate orchestration."""

from __future__ import annotations

import threading
import time

from ..errors import LeanRuntimeError
from .candidate import DiscoveryPlan
from .probe import CandidateProbe, ProbeIntegrityFailure, ProbeUnavailable
from .result import (
    AttemptStatus,
    CandidateAttempt,
    DiscoveryCompletion,
    DiscoveryConfidence,
    DiscoveryOutcome,
    DiscoveryResult,
    ResultDiagnostic,
)

DISCOVERY_FOUND = "DISCOVERY_FOUND"
DISCOVERY_CANDIDATE_LIMIT = "DISCOVERY_CANDIDATE_LIMIT"
DISCOVERY_ACQUISITION_LIMIT = "DISCOVERY_ACQUISITION_LIMIT"
DISCOVERY_TIME_LIMIT = "DISCOVERY_TIME_LIMIT"
DISCOVERY_NO_CANDIDATES = "DISCOVERY_NO_CANDIDATES"
DISCOVERY_EXHAUSTED = "DISCOVERY_EXHAUSTED"
DISCOVERY_EXPLICIT_LOCK = "DISCOVERY_EXPLICIT_LOCK"
DISCOVERY_CANCELLED = "DISCOVERY_CANCELLED"
DISCOVERY_RUNTIME_ERROR = "DISCOVERY_RUNTIME_ERROR"
CANDIDATE_LEAN_REJECTED = "CANDIDATE_LEAN_REJECTED"
CANDIDATE_UNAVAILABLE = "CANDIDATE_UNAVAILABLE"
CANDIDATE_INTEGRITY_FAILURE = "CANDIDATE_INTEGRITY_FAILURE"
CANDIDATE_TIMEOUT = "CANDIDATE_TIMEOUT"
CANDIDATE_CANCELLED = "CANDIDATE_CANCELLED"
CANDIDATE_RUNTIME_ERROR = "CANDIDATE_RUNTIME_ERROR"


class _AttemptCancellation:
    def __init__(self, timeout_seconds: float, external: threading.Event | None) -> None:
        self._timeout_seconds = timeout_seconds
        self._deadline: float | None = None
        self.cancel = threading.Event()
        self.timed_out = threading.Event()
        self._stop = threading.Event()
        self._timer = threading.Timer(timeout_seconds, self._expire)
        self._timer.daemon = True
        self._relay: threading.Thread | None = None
        if external is not None:
            self._relay = threading.Thread(
                target=self._relay_external,
                args=(external,),
                daemon=True,
            )

    def _expire(self) -> None:
        self.timed_out.set()
        self.cancel.set()

    def _relay_external(self, external: threading.Event) -> None:
        while not self._stop.wait(0.05):
            if external.is_set():
                self.cancel.set()
                return

    def __enter__(self) -> _AttemptCancellation:
        self._deadline = time.monotonic() + self._timeout_seconds
        self._timer.start()
        if self._relay is not None:
            self._relay.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._timer.cancel()
        if self._relay is not None:
            self._relay.join(timeout=0.1)

    def expired(self) -> bool:
        """Return deadline expiry even when the timer callback is scheduled late."""
        return self.timed_out.is_set() or (
            self._deadline is not None and time.monotonic() >= self._deadline
        )


def _diagnostic(code: str, detail: str) -> tuple[ResultDiagnostic, ...]:
    return (ResultDiagnostic(code=code, detail=detail),)


def _failed_attempt(
    exc: Exception,
    *,
    candidate_id: str,
    lock_id: str,
    attempt_started: float,
    cancelled_externally: bool,
    timed_out: bool,
) -> tuple[CandidateAttempt, str]:
    """Map one probe exception to an attempt and an action: continue, failed, or cancelled."""
    duration = time.monotonic() - attempt_started
    if isinstance(exc, ProbeIntegrityFailure):
        status: AttemptStatus = "environment_integrity_failure"
        code = CANDIDATE_INTEGRITY_FAILURE
        action = "failed"
    elif isinstance(exc, ProbeUnavailable):
        if cancelled_externally:
            status = "cancelled"
            code = CANDIDATE_CANCELLED
            action = "cancelled"
        elif timed_out:
            status = "timeout"
            code = CANDIDATE_TIMEOUT
            action = "continue"
        else:
            status = "environment_unavailable"
            code = CANDIDATE_UNAVAILABLE
            action = "continue"
    elif isinstance(exc, LeanRuntimeError):
        if cancelled_externally:
            status = "cancelled"
            code = CANDIDATE_CANCELLED
            action = "cancelled"
        elif timed_out:
            status = "timeout"
            code = CANDIDATE_TIMEOUT
            action = "continue"
        else:
            status = "runtime_error"
            code = CANDIDATE_RUNTIME_ERROR
            action = "failed"
    else:  # defensive adapter boundary
        status = "runtime_error"
        code = CANDIDATE_RUNTIME_ERROR
        action = "generic_failed"
    attempt = CandidateAttempt(
        candidate_id=candidate_id,
        lock_id=lock_id,
        status=status,
        duration_seconds=duration,
        diagnostics=_diagnostic(code, str(exc)),
    )
    return attempt, action


def _rejection_detail(stderr: str, stdout: str) -> str:
    return stderr.strip() or stdout.strip() or "Lean rejected the source"


def discover(
    source: str,
    plan: DiscoveryPlan,
    probe: CandidateProbe | None,
    *,
    cancel: threading.Event | None = None,
) -> DiscoveryResult:
    """Execute a plan sequentially and return only compiler-backed success."""

    started = time.monotonic()
    if plan.explicit_lock is not None:
        return DiscoveryResult(
            status="invalid_request",
            confidence="exhausted",
            outcome="failed",
            completion="complete",
            plan=plan,
            attempts=(),
            duration_seconds=time.monotonic() - started,
            diagnostics=_diagnostic(
                DISCOVERY_EXPLICIT_LOCK,
                "source already declares an exact Runtime lock; open it directly",
            ),
        )
    if not plan.candidates:
        return DiscoveryResult(
            status="not_found",
            confidence="exhausted",
            outcome="no_candidate",
            completion="complete",
            plan=plan,
            attempts=(),
            duration_seconds=time.monotonic() - started,
            diagnostics=_diagnostic(
                DISCOVERY_NO_CANDIDATES, "catalog contains no plausible candidate"
            ),
        )

    wall_deadline = started + plan.policy.max_wall_seconds
    probe_remaining = plan.policy.max_total_seconds
    if probe is None:
        raise ValueError("an authoritative candidate probe is required")
    attempts: list[CandidateAttempt] = []
    remote_acquisitions = 0
    for candidate in plan.planned_candidates:
        if cancel is not None and cancel.is_set():
            return DiscoveryResult(
                status="cancelled",
                confidence="exhausted",
                outcome="cancelled",
                completion="complete",
                plan=plan,
                attempts=tuple(attempts),
                duration_seconds=time.monotonic() - started,
                diagnostics=_diagnostic(DISCOVERY_CANCELLED, "discovery was cancelled"),
            )
        remaining_wall = wall_deadline - time.monotonic()
        if remaining_wall <= 0 or probe_remaining <= 0:
            return DiscoveryResult(
                status="not_found",
                confidence="inconclusive",
                outcome="inconclusive",
                completion="time_limit",
                plan=plan,
                attempts=tuple(attempts),
                duration_seconds=time.monotonic() - started,
                diagnostics=_diagnostic(DISCOVERY_TIME_LIMIT, "total wall-clock budget expired"),
            )
        attempt_started = time.monotonic()
        candidate_is_local = any(
            reason.code == "RANK_LOCAL_AVAILABLE" for reason in candidate.reasons
        )
        if not candidate_is_local and remote_acquisitions >= plan.policy.max_remote_acquisitions:
            return DiscoveryResult(
                status="not_found",
                confidence="inconclusive",
                outcome="inconclusive",
                completion="acquisition_limit",
                plan=plan,
                attempts=tuple(attempts),
                duration_seconds=time.monotonic() - started,
                diagnostics=_diagnostic(
                    DISCOVERY_ACQUISITION_LIMIT,
                    "remote acquisition limit reached",
                ),
            )
        if not candidate_is_local:
            remote_acquisitions += 1
        acquisition_timeout = min(
            plan.policy.acquisition_timeout_seconds,
            max(0.001, remaining_wall),
        )
        with _AttemptCancellation(acquisition_timeout, cancel) as acquisition_cancel:
            try:
                acquired = probe.acquire(
                    candidate,
                    timeout_seconds=acquisition_timeout,
                    cancel=acquisition_cancel.cancel,
                )
            except Exception as exc:  # defensive adapter boundary
                attempt, action = _failed_attempt(
                    exc,
                    candidate_id=candidate.entry.id,
                    lock_id=candidate.entry.lock.lock_id,
                    attempt_started=attempt_started,
                    cancelled_externally=cancel is not None and cancel.is_set(),
                    timed_out=acquisition_cancel.expired() or time.monotonic() >= wall_deadline,
                )
                attempts.append(attempt)
                if action == "continue":
                    continue
                return DiscoveryResult(
                    status="cancelled" if action == "cancelled" else "failed",
                    confidence="exhausted",
                    outcome="cancelled" if action == "cancelled" else "failed",
                    completion="complete",
                    plan=plan,
                    attempts=tuple(attempts),
                    duration_seconds=time.monotonic() - started,
                    diagnostics=_diagnostic(DISCOVERY_RUNTIME_ERROR, str(exc))
                    if action == "generic_failed"
                    else attempt.diagnostics,
                )
        remaining_wall = wall_deadline - time.monotonic()
        if remaining_wall <= 0:
            attempts.append(
                CandidateAttempt(
                    candidate_id=candidate.entry.id,
                    lock_id=candidate.entry.lock.lock_id,
                    status="timeout",
                    duration_seconds=time.monotonic() - attempt_started,
                    acquisition=acquired.acquisition,
                    environment_id=acquired.environment_id,
                    diagnostics=_diagnostic(
                        CANDIDATE_TIMEOUT,
                        "global wall-clock budget expired after acquisition",
                    ),
                )
            )
            return DiscoveryResult(
                status="not_found",
                confidence="inconclusive",
                outcome="inconclusive",
                completion="time_limit",
                plan=plan,
                attempts=tuple(attempts),
                duration_seconds=time.monotonic() - started,
                diagnostics=_diagnostic(DISCOVERY_TIME_LIMIT, "total wall-clock budget expired"),
            )
        # The candidate timeout and remaining global wall budget are both
        # authoritative; whichever is smaller bounds this Lean invocation.
        timeout = min(
            plan.policy.candidate_timeout_seconds or probe_remaining,
            probe_remaining,
            remaining_wall,
        )
        check_started = time.monotonic()
        with _AttemptCancellation(timeout, cancel) as attempt_cancel:
            try:
                outcome = probe.check(
                    acquired,
                    source,
                    timeout_seconds=timeout,
                    cancel=attempt_cancel.cancel,
                )
            except Exception as exc:  # defensive adapter boundary
                probe_remaining -= time.monotonic() - check_started
                attempt, action = _failed_attempt(
                    exc,
                    candidate_id=candidate.entry.id,
                    lock_id=candidate.entry.lock.lock_id,
                    attempt_started=attempt_started,
                    cancelled_externally=cancel is not None and cancel.is_set(),
                    timed_out=attempt_cancel.expired() or time.monotonic() >= wall_deadline,
                )
                attempts.append(attempt)
                if action == "continue":
                    continue
                return DiscoveryResult(
                    status="cancelled" if action == "cancelled" else "failed",
                    confidence="exhausted",
                    outcome="cancelled" if action == "cancelled" else "failed",
                    completion="complete",
                    plan=plan,
                    attempts=tuple(attempts),
                    duration_seconds=time.monotonic() - started,
                    diagnostics=_diagnostic(DISCOVERY_RUNTIME_ERROR, str(exc))
                    if action == "generic_failed"
                    else attempt.diagnostics,
                )

        probe_remaining -= time.monotonic() - check_started

        result = outcome.execution_result
        attempt_duration = time.monotonic() - attempt_started
        identity_error: str | None = None
        if result.lock_id is not None and result.lock_id != candidate.entry.lock.lock_id:
            identity_error = "Runtime execution lock does not match the candidate lock"
        elif result.environment_id is not None and result.environment_id != outcome.environment_id:
            identity_error = "Runtime execution environment does not match the opened environment"
        elif result.toolchain != candidate.entry.toolchain:
            identity_error = "Runtime execution toolchain does not match the candidate toolchain"
        if identity_error is not None:
            attempt = CandidateAttempt(
                candidate_id=candidate.entry.id,
                lock_id=candidate.entry.lock.lock_id,
                status="environment_integrity_failure",
                duration_seconds=attempt_duration,
                acquisition=outcome.acquisition,
                environment_id=outcome.environment_id,
                execution_id=result.execution_id,
                execution_result=result,
                diagnostics=_diagnostic(CANDIDATE_INTEGRITY_FAILURE, identity_error),
            )
            attempts.append(attempt)
            return DiscoveryResult(
                status="failed",
                confidence="exhausted",
                outcome="failed",
                completion="complete",
                plan=plan,
                attempts=tuple(attempts),
                duration_seconds=time.monotonic() - started,
                diagnostics=attempt.diagnostics,
            )
        if cancel is not None and cancel.is_set():
            outcome_status: AttemptStatus = "cancelled"
            code = CANDIDATE_CANCELLED
            detail = "candidate execution was cancelled"
        elif result.timed_out or attempt_cancel.expired() or time.monotonic() >= wall_deadline:
            outcome_status = "timeout"
            code = CANDIDATE_TIMEOUT
            detail = "candidate execution exceeded its time budget"
        elif result.cancelled:
            outcome_status = "cancelled"
            code = CANDIDATE_CANCELLED
            detail = "candidate execution was cancelled"
        elif result.ok:
            outcome_status = "compiled"
            code = DISCOVERY_FOUND
            detail = "Lean accepted the source in this exact environment"
        else:
            outcome_status = "lean_rejected"
            code = CANDIDATE_LEAN_REJECTED
            detail = _rejection_detail(result.stderr, result.stdout)
        attempt = CandidateAttempt(
            candidate_id=candidate.entry.id,
            lock_id=candidate.entry.lock.lock_id,
            status=outcome_status,
            duration_seconds=attempt_duration,
            acquisition=outcome.acquisition,
            environment_id=outcome.environment_id,
            execution_id=result.execution_id,
            execution_result=result,
            diagnostics=_diagnostic(code, detail),
        )
        attempts.append(attempt)
        if outcome_status == "compiled":
            return DiscoveryResult(
                status="found",
                confidence="compiled",
                outcome="found",
                completion="complete",
                plan=plan,
                selected_candidate=candidate,
                lock=candidate.entry.lock,
                execution_result=result,
                attempts=tuple(attempts),
                duration_seconds=time.monotonic() - started,
                diagnostics=_diagnostic(DISCOVERY_FOUND, detail),
            )
        if outcome_status == "cancelled":
            return DiscoveryResult(
                status="cancelled",
                confidence="exhausted",
                outcome="cancelled",
                completion="complete",
                plan=plan,
                attempts=tuple(attempts),
                duration_seconds=time.monotonic() - started,
                diagnostics=attempt.diagnostics,
            )

    if time.monotonic() >= wall_deadline or probe_remaining <= 0:
        reason = ResultDiagnostic(DISCOVERY_TIME_LIMIT, "total wall-clock budget expired")
        completion: DiscoveryCompletion = "time_limit"
        final_outcome: DiscoveryOutcome = "inconclusive"
        confidence: DiscoveryConfidence = "inconclusive"
    elif plan.truncated:
        reason = ResultDiagnostic(
            DISCOVERY_CANDIDATE_LIMIT,
            f"tested {len(attempts)} candidates; additional plausible candidates were bounded",
        )
        completion = "candidate_limit"
        final_outcome = "inconclusive"
        confidence = "inconclusive"
    else:
        reason = ResultDiagnostic(
            DISCOVERY_EXHAUSTED,
            f"Lean did not accept the source in any of {len(attempts)} tested candidates",
        )
        completion = "complete"
        final_outcome = (
            "source_rejected"
            if any(attempt.status == "lean_rejected" for attempt in attempts)
            else "no_candidate"
        )
        confidence = "exhausted"
    rejection_diagnostics = tuple(
        diagnostic
        for attempt in attempts
        if attempt.status == "lean_rejected"
        for diagnostic in attempt.diagnostics
    )
    timeout_diagnostics = tuple(
        diagnostic
        for attempt in attempts
        if attempt.status == "timeout"
        for diagnostic in attempt.diagnostics
    )
    return DiscoveryResult(
        status="not_found",
        confidence=confidence,
        outcome=final_outcome,
        completion=completion,
        plan=plan,
        attempts=tuple(attempts),
        duration_seconds=time.monotonic() - started,
        diagnostics=(*timeout_diagnostics, reason, *rejection_diagnostics),
    )
