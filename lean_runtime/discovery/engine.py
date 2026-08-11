"""Bounded authoritative candidate orchestration."""

from __future__ import annotations

import threading
import time

from ..errors import LeanRuntimeError
from .candidate import DiscoveryPlan
from .probe import CandidateProbe, ProbeIntegrityFailure, ProbeUnavailable
from .result import AttemptStatus, CandidateAttempt, DiscoveryResult, ResultDiagnostic

DISCOVERY_FOUND = "DISCOVERY_FOUND"
DISCOVERY_CANDIDATE_LIMIT = "DISCOVERY_CANDIDATE_LIMIT"
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
        self._timer.start()
        if self._relay is not None:
            self._relay.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._timer.cancel()
        if self._relay is not None:
            self._relay.join(timeout=0.1)


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
            plan=plan,
            attempts=(),
            duration_seconds=time.monotonic() - started,
            diagnostics=_diagnostic(
                DISCOVERY_NO_CANDIDATES, "catalog contains no plausible candidate"
            ),
        )

    deadline = started + plan.policy.max_total_seconds
    if probe is None:
        raise ValueError("an authoritative candidate probe is required")
    attempts: list[CandidateAttempt] = []
    for candidate in plan.candidates:
        if cancel is not None and cancel.is_set():
            return DiscoveryResult(
                status="cancelled",
                confidence="exhausted",
                plan=plan,
                attempts=tuple(attempts),
                duration_seconds=time.monotonic() - started,
                diagnostics=_diagnostic(DISCOVERY_CANCELLED, "discovery was cancelled"),
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return DiscoveryResult(
                status="not_found",
                confidence="exhausted",
                plan=plan,
                attempts=tuple(attempts),
                duration_seconds=time.monotonic() - started,
                diagnostics=_diagnostic(DISCOVERY_TIME_LIMIT, "total search budget expired"),
            )
        attempt_started = time.monotonic()
        acquisition_timeout = plan.policy.acquisition_timeout_seconds
        with _AttemptCancellation(acquisition_timeout, cancel) as acquisition_cancel:
            try:
                # Acquisition (downloads, toolchain installs, builds) is
                # budgeted separately; every acquisition outcome, including
                # failures, credits its elapsed time back to the search budget.
                try:
                    acquired = probe.acquire(
                        candidate,
                        timeout_seconds=acquisition_timeout,
                        cancel=acquisition_cancel.cancel,
                    )
                finally:
                    deadline += time.monotonic() - attempt_started
            except Exception as exc:  # defensive adapter boundary
                attempt, action = _failed_attempt(
                    exc,
                    candidate_id=candidate.entry.id,
                    lock_id=candidate.entry.lock.lock_id,
                    attempt_started=attempt_started,
                    cancelled_externally=cancel is not None and cancel.is_set(),
                    timed_out=acquisition_cancel.timed_out.is_set(),
                )
                attempts.append(attempt)
                if action == "continue":
                    continue
                return DiscoveryResult(
                    status="cancelled" if action == "cancelled" else "failed",
                    confidence="exhausted",
                    plan=plan,
                    attempts=tuple(attempts),
                    duration_seconds=time.monotonic() - started,
                    diagnostics=_diagnostic(DISCOVERY_RUNTIME_ERROR, str(exc))
                    if action == "generic_failed"
                    else attempt.diagnostics,
                )
        remaining = deadline - time.monotonic()
        timeout = min(
            remaining,
            plan.policy.candidate_timeout_seconds or remaining,
        )
        with _AttemptCancellation(timeout, cancel) as attempt_cancel:
            try:
                outcome = probe.check(
                    acquired,
                    source,
                    timeout_seconds=timeout,
                    cancel=attempt_cancel.cancel,
                )
            except Exception as exc:  # defensive adapter boundary
                attempt, action = _failed_attempt(
                    exc,
                    candidate_id=candidate.entry.id,
                    lock_id=candidate.entry.lock.lock_id,
                    attempt_started=attempt_started,
                    cancelled_externally=cancel is not None and cancel.is_set(),
                    timed_out=attempt_cancel.timed_out.is_set(),
                )
                attempts.append(attempt)
                if action == "continue":
                    continue
                return DiscoveryResult(
                    status="cancelled" if action == "cancelled" else "failed",
                    confidence="exhausted",
                    plan=plan,
                    attempts=tuple(attempts),
                    duration_seconds=time.monotonic() - started,
                    diagnostics=_diagnostic(DISCOVERY_RUNTIME_ERROR, str(exc))
                    if action == "generic_failed"
                    else attempt.diagnostics,
                )

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
                plan=plan,
                attempts=tuple(attempts),
                duration_seconds=time.monotonic() - started,
                diagnostics=attempt.diagnostics,
            )
        if cancel is not None and cancel.is_set():
            outcome_status: AttemptStatus = "cancelled"
            code = CANDIDATE_CANCELLED
            detail = "candidate execution was cancelled"
        elif result.timed_out or attempt_cancel.timed_out.is_set():
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
                plan=plan,
                attempts=tuple(attempts),
                duration_seconds=time.monotonic() - started,
                diagnostics=attempt.diagnostics,
            )

    if time.monotonic() >= deadline:
        reason = ResultDiagnostic(DISCOVERY_TIME_LIMIT, "total search budget expired")
    elif plan.truncated:
        reason = ResultDiagnostic(
            DISCOVERY_CANDIDATE_LIMIT,
            f"tested {len(attempts)} candidates; additional plausible candidates were bounded",
        )
    else:
        reason = ResultDiagnostic(
            DISCOVERY_EXHAUSTED,
            f"Lean did not accept the source in any of {len(attempts)} tested candidates",
        )
    rejection_diagnostics = tuple(
        diagnostic
        for attempt in attempts
        if attempt.status == "lean_rejected"
        for diagnostic in attempt.diagnostics
    )
    return DiscoveryResult(
        status="not_found",
        confidence="exhausted",
        plan=plan,
        attempts=tuple(attempts),
        duration_seconds=time.monotonic() - started,
        diagnostics=(reason, *rejection_diagnostics),
    )
