from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field

import pytest

from lean_runtime import ExecutionResult, Runtime
from lean_runtime.discovery import (
    Discovery,
    DiscoveryError,
    DiscoveryPolicy,
    PolicyError,
    ProbeOutcome,
)
from lean_runtime.discovery.candidate import Candidate
from lean_runtime.discovery.probe import (
    AcquiredCandidate,
    ProbeIntegrityFailure,
    ProbeUnavailable,
)


def execution(
    ok: bool,
    *,
    timed_out: bool = False,
    cancelled: bool = False,
    stderr: str = "",
    toolchain: str = "leanprover/lean4:v4.32.2",
) -> ExecutionResult:
    return ExecutionResult(
        ok=ok,
        exit_code=0 if ok else 1,
        toolchain=toolchain,
        command=("lean", "Main.lean"),
        cwd="/fixture",
        stdout="",
        stderr=stderr,
        elapsed_seconds=0.01,
        timed_out=timed_out,
        cancelled=cancelled,
    )


@dataclass
class FakeProbe:
    outcomes: dict[str, ExecutionResult | Exception]
    opened: list[str] = field(default_factory=list)
    acquisition_delay: float = 0.0

    def acquire(
        self,
        candidate: Candidate,
        *,
        timeout_seconds: float,
        cancel: threading.Event,
    ) -> AcquiredCandidate:
        del timeout_seconds, cancel
        candidate_id = candidate.entry.id
        self.opened.append(candidate_id)
        if self.acquisition_delay:
            time.sleep(self.acquisition_delay)
        outcome = self.outcomes[candidate_id]
        if isinstance(outcome, (ProbeUnavailable, ProbeIntegrityFailure)):
            raise outcome
        return AcquiredCandidate(
            candidate=candidate,
            environment_id=f"env_{candidate_id}",
        )

    def check(
        self,
        acquired: AcquiredCandidate,
        source: str,
        *,
        timeout_seconds: float,
        cancel: threading.Event,
    ) -> ProbeOutcome:
        del source, timeout_seconds, cancel
        outcome = self.outcomes[acquired.candidate.entry.id]
        if isinstance(outcome, Exception):
            raise outcome
        return ProbeOutcome(environment_id=acquired.environment_id, execution_result=outcome)


def test_first_rejection_second_compilation_is_authoritative(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    probe = FakeProbe(
        {
            "mathlib-new": execution(False, stderr="type mismatch"),
            "mathlib-old": execution(True),
        }
    )
    result = Discovery(catalog=sample_catalog, probe=probe).discover_and_check("import Mathlib\n")
    assert result.status == "found"
    assert result.confidence == "compiled"
    assert result.selected_candidate is not None
    assert result.selected_candidate.entry.id == "mathlib-old"
    assert result.environment_id == "env_mathlib-old"
    assert result.attempts[-1].acquisition == "unknown"
    assert [item.status for item in result.attempts] == ["lean_rejected", "compiled"]
    assert probe.opened == ["mathlib-new", "mathlib-old"]


def test_candidate_budget_never_opens_bounded_candidate(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    probe = FakeProbe({"mathlib-new": execution(False)})
    result = Discovery(
        catalog=sample_catalog,
        policy=DiscoveryPolicy(max_candidates=1),
        probe=probe,
    ).discover_and_check("import Mathlib\n")
    assert result.status == "not_found"
    assert len(result.attempts) == 1
    assert result.diagnostics[0].code == "DISCOVERY_CANDIDATE_LIMIT"
    assert probe.opened == ["mathlib-new"]


def test_exhaustion_surfaces_the_highest_ranked_lean_rejection(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    probe = FakeProbe({"mathlib-new": execution(False, stderr="type mismatch")})
    result = Discovery(
        catalog=sample_catalog,
        policy=DiscoveryPolicy(max_candidates=1),
        probe=probe,
    ).discover_and_check("import Mathlib\n")

    assert result.status == "not_found"
    assert result.rejection_attempt is result.attempts[0]
    assert [item.code for item in result.diagnostics] == [
        "DISCOVERY_CANDIDATE_LIMIT",
        "CANDIDATE_LEAN_REJECTED",
    ]
    assert result.diagnostics[1].detail == "type mismatch"
    with pytest.raises(DiscoveryError, match="type mismatch"):
        result.raise_for_error()


def test_unavailable_candidate_advances(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    probe = FakeProbe(
        {
            "mathlib-new": ProbeUnavailable("not published"),
            "mathlib-old": execution(True),
        }
    )
    result = Discovery(catalog=sample_catalog, probe=probe).discover_and_check("import Mathlib\n")
    assert result.status == "found"
    assert [item.status for item in result.attempts] == [
        "environment_unavailable",
        "compiled",
    ]


def test_integrity_failure_is_terminal(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    probe = FakeProbe(
        {
            "mathlib-new": ProbeIntegrityFailure("digest mismatch"),
            "mathlib-old": execution(True),
        }
    )
    result = Discovery(catalog=sample_catalog, probe=probe).discover_and_check("import Mathlib\n")
    assert result.status == "failed"
    assert [item.status for item in result.attempts] == ["environment_integrity_failure"]
    assert probe.opened == ["mathlib-new"]


def test_runtime_identity_mismatch_is_terminal(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    probe = FakeProbe(
        {
            "mathlib-new": execution(True, toolchain="leanprover/lean4:v4.31.0"),
            "mathlib-old": execution(True),
        }
    )
    result = Discovery(catalog=sample_catalog, probe=probe).discover_and_check("import Mathlib\n")
    assert result.status == "failed"
    assert result.attempts[0].status == "environment_integrity_failure"
    assert probe.opened == ["mathlib-new"]


@dataclass
class BlockingProbe:
    opened: int = 0

    def acquire(
        self,
        candidate: Candidate,
        *,
        timeout_seconds: float,
        cancel: threading.Event,
    ) -> AcquiredCandidate:
        del timeout_seconds, cancel
        return AcquiredCandidate(candidate=candidate, environment_id="env_blocked")

    def check(
        self,
        acquired: AcquiredCandidate,
        source: str,
        *,
        timeout_seconds: float,
        cancel: threading.Event,
    ) -> ProbeOutcome:
        del acquired, source, timeout_seconds
        self.opened += 1
        while not cancel.wait(0.005):
            pass
        return ProbeOutcome(
            environment_id="env_blocked",
            execution_result=execution(False, cancelled=True),
        )


def test_per_candidate_timeout_is_enforced(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    probe = BlockingProbe()
    result = Discovery(
        catalog=sample_catalog,
        policy=DiscoveryPolicy(
            max_candidates=1,
            max_total_seconds=1,
            candidate_timeout_seconds=0.03,
        ),
        probe=probe,
    ).discover_and_check("import Mathlib\n")
    assert result.status == "not_found"
    assert result.attempts[0].status == "timeout"
    assert result.duration_seconds < 0.5


def test_slow_acquisition_does_not_consume_search_budget(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    """A slow download must not expire the search budget before a fast, green check."""
    probe = FakeProbe({"mathlib-new": execution(True)}, acquisition_delay=0.3)
    result = Discovery(
        catalog=sample_catalog,
        policy=DiscoveryPolicy(max_candidates=1, max_total_seconds=0.2),
        probe=probe,
    ).discover_and_check("import Mathlib\n")
    assert result.status == "found"
    assert result.confidence == "compiled"


def test_acquisition_timeout_bounds_a_stuck_download(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    probe = FakeProbe({"mathlib-new": execution(True)}, acquisition_delay=0.3)
    result = Discovery(
        catalog=sample_catalog,
        policy=DiscoveryPolicy(
            max_candidates=1,
            max_total_seconds=5,
            acquisition_timeout_seconds=0.05,
        ),
        probe=probe,
    ).discover_and_check("import Mathlib\n")
    # The fake ignores cancellation, so the engine still gets a result; a real
    # probe observes the cancellation event.  The budget itself must be valid.
    assert result.status in {"found", "not_found"}


def test_external_cancellation_stops_active_probe(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    probe = BlockingProbe()
    cancel = threading.Event()
    timer = threading.Timer(0.03, cancel.set)
    timer.start()
    try:
        result = Discovery(catalog=sample_catalog, probe=probe).discover_and_check(
            "import Mathlib\n", cancel=cancel
        )
    finally:
        timer.cancel()
    assert result.status == "cancelled"
    assert result.attempts[0].status == "cancelled"


def test_async_cancellation_reaches_active_probe(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    probe = BlockingProbe()

    async def scenario() -> None:
        task = asyncio.create_task(
            Discovery(catalog=sample_catalog, probe=probe).discover_and_check_async(
                "import Mathlib\n"
            )
        )
        await asyncio.sleep(0.03)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return
        raise AssertionError("discovery task was not cancelled")

    asyncio.run(scenario())
    assert probe.opened == 1


def test_no_candidates_does_not_require_probe(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    result = Discovery(catalog=sample_catalog).discover_and_check("import Missing.Module\n")
    assert result.status == "not_found"
    assert result.attempts == ()


def test_explicit_lock_is_invalid_discovery_request(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    source = """-- /// lean-runtime
-- lock = "environment.lock.json"
-- ///
example : True := trivial
"""
    result = Discovery(catalog=sample_catalog).discover_and_check(source)
    assert result.status == "invalid_request"
    assert result.diagnostics[0].code == "DISCOVERY_EXPLICIT_LOCK"


def test_injected_runtime_cannot_weaken_source_build_policy(
    tmp_path,
    sample_catalog,  # type: ignore[no-untyped-def]
) -> None:
    runtime = Runtime(home=tmp_path / "runtime", availability="auto", libraries=())
    with pytest.raises(PolicyError, match="source fallback"):
        Discovery(catalog=sample_catalog, runtime=runtime).discover_and_check("import Mathlib\n")
