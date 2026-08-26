from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field, replace

import pytest
from jsonschema import Draft202012Validator

from lean_runtime import EnvironmentError, ExecutionResult
from lean_runtime.discovery import (
    Discovery,
    DiscoveryError,
    DiscoveryPolicy,
    ProbeOutcome,
    engine,
    schema_path,
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


def _with_local(plan, *entry_ids: str):  # type: ignore[no-untyped-def]
    """Mark selected candidates as locally materialized without reordering."""
    from lean_runtime.discovery.candidate import CandidateReason

    return replace(
        plan,
        candidates=tuple(
            replace(
                candidate,
                reasons=(
                    *candidate.reasons,
                    CandidateReason("RANK_LOCAL_AVAILABLE", "environment is local"),
                ),
            )
            if candidate.entry.id in entry_ids
            else candidate
            for candidate in plan.candidates
        ),
    )


def test_first_rejection_second_compilation_is_authoritative(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    # A proof-level rejection bounds the march to already-local candidates;
    # a local second compilation remains the authoritative rescue.
    probe = FakeProbe(
        {
            "mathlib-new": execution(False, stderr="type mismatch"),
            "mathlib-old": execution(True),
        }
    )
    discovery = Discovery(catalog=sample_catalog, probe=probe)
    plan = _with_local(discovery.plan("import Mathlib\n"), "mathlib-old")
    result = engine.discover("import Mathlib\n", plan, probe)
    assert result.status == "found"
    assert result.confidence == "compiled"
    assert result.selected_candidate is not None
    assert result.selected_candidate.entry.id == "mathlib-old"
    assert result.environment_id == "env_mathlib-old"
    assert result.attempts[-1].acquisition == "unknown"
    assert [item.status for item in result.attempts] == ["lean_rejected", "compiled"]
    assert probe.opened == ["mathlib-new", "mathlib-old"]
    Draft202012Validator(json.loads(schema_path("result-v1.schema.json").read_text())).validate(
        result.to_dict()
    )


def test_proof_level_rejection_never_downloads_another_candidate(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    probe = FakeProbe(
        {
            "mathlib-new": execution(False, stderr="unsolved goals\n⊢ False"),
            "mathlib-old": execution(True),
        }
    )
    result = Discovery(catalog=sample_catalog, probe=probe).discover_and_check("import Mathlib\n")
    assert result.status == "not_found"
    assert result.outcome == "source_rejected"
    assert result.completion == "complete"
    assert probe.opened == ["mathlib-new"]
    assert any(item.code == "DISCOVERY_VERDICT_BOUNDED" for item in result.diagnostics)


def test_proof_level_rejection_still_marches_through_offline_source_builds(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    # An offline plan acquires candidates by deliberate local source builds,
    # not downloads, so a proof-level rejection keeps the full march: a newer
    # candidate may reject what an older candidate legitimately compiles.
    probe = FakeProbe(
        {
            "mathlib-new": execution(False, stderr="type mismatch"),
            "mathlib-old": execution(True),
        }
    )
    result = Discovery(
        catalog=sample_catalog,
        policy=DiscoveryPolicy(allow_download=False, allow_source_build=True),
        probe=probe,
    ).discover_and_check("import Mathlib\n")
    assert result.status == "found"
    assert result.selected_candidate is not None
    assert result.selected_candidate.entry.id == "mathlib-old"
    assert probe.opened == ["mathlib-new", "mathlib-old"]


def test_identifier_rejection_keeps_marching_to_remote_candidates(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    probe = FakeProbe(
        {
            "mathlib-new": execution(False, stderr="unknown constant 'List.nthLe'"),
            "mathlib-old": execution(True),
        }
    )
    result = Discovery(catalog=sample_catalog, probe=probe).discover_and_check("import Mathlib\n")
    assert result.status == "found"
    assert result.selected_candidate is not None
    assert result.selected_candidate.entry.id == "mathlib-old"
    assert probe.opened == ["mathlib-new", "mathlib-old"]


def test_module_missing_everywhere_stops_after_one_candidate(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    probe = FakeProbe(
        {
            "mathlib-new": execution(False, stderr="unknown module prefix 'ConstantsEmbedding'"),
            "mathlib-old": execution(True),
        }
    )
    result = Discovery(catalog=sample_catalog, probe=probe).discover_and_check("import Mathlib\n")
    assert result.status == "not_found"
    assert result.outcome == "source_rejected"
    assert result.completion == "complete"
    assert probe.opened == ["mathlib-new"]
    assert result.diagnostics[0].code == "DISCOVERY_MODULE_UNAVAILABLE"
    assert "ConstantsEmbedding" in result.diagnostics[0].detail


def test_module_provided_by_another_candidate_keeps_marching(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    probe = FakeProbe(
        {
            "mathlib-new": execution(False, stderr="unknown module 'Mathlib.Legacy'"),
            "mathlib-old": execution(True),
        }
    )
    result = Discovery(catalog=sample_catalog, probe=probe).discover_and_check("import Mathlib\n")
    assert result.status == "found"
    assert result.selected_candidate is not None
    assert result.selected_candidate.entry.id == "mathlib-old"
    assert probe.opened == ["mathlib-new", "mathlib-old"]


def test_candidate_budget_never_opens_bounded_candidate(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    probe = FakeProbe({"mathlib-new": execution(False)})
    result = Discovery(
        catalog=sample_catalog,
        policy=DiscoveryPolicy(max_candidates=1),
        probe=probe,
    ).discover_and_check("import Mathlib\n")
    assert result.status == "not_found"
    assert result.outcome == "inconclusive"
    assert result.completion == "candidate_limit"
    assert result.confidence == "inconclusive"
    assert len(result.attempts) == 1
    assert result.diagnostics[0].code == "DISCOVERY_CANDIDATE_LIMIT"
    assert probe.opened == ["mathlib-new"]


def test_complete_rejection_is_distinct_from_an_incomplete_search(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    probe = FakeProbe(
        {
            "mathlib-new": execution(False),
            "mathlib-old": execution(False),
            "core": execution(False),
        }
    )
    result = Discovery(
        catalog=sample_catalog,
        policy=DiscoveryPolicy(max_candidates=3, max_remote_acquisitions=3),
        probe=probe,
    ).discover_and_check("import Mathlib\n")
    assert result.outcome == "source_rejected"
    assert result.completion == "complete"
    assert result.confidence == "exhausted"


def test_remote_acquisition_limit_is_truthful(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    probe = FakeProbe(
        {
            "mathlib-new": execution(False),
            "mathlib-old": execution(True),
        }
    )
    result = Discovery(
        catalog=sample_catalog,
        policy=DiscoveryPolicy(max_remote_acquisitions=1),
        probe=probe,
    ).discover_and_check("import Mathlib\n")
    assert result.outcome == "inconclusive"
    assert result.completion == "acquisition_limit"
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
    assert result.diagnostics[0].code == "CANDIDATE_TIMEOUT"
    assert "time budget" in result.diagnostics[0].detail
    assert result.duration_seconds < 0.5


def test_attempt_deadline_is_authoritative_when_timer_callback_is_delayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 100.0}
    monkeypatch.setattr(engine.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(threading.Timer, "start", lambda _timer: None)

    with engine._AttemptCancellation(0.2, None) as cancellation:
        clock["now"] = 100.21
        assert cancellation.expired()
        assert not cancellation.timed_out.is_set()


def test_slow_acquisition_consumes_global_wall_budget(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    probe = FakeProbe({"mathlib-new": execution(True)}, acquisition_delay=0.3)
    result = Discovery(
        catalog=sample_catalog,
        policy=DiscoveryPolicy(
            max_candidates=1,
            max_total_seconds=1,
            max_wall_seconds=0.2,
        ),
        probe=probe,
    ).discover_and_check("import Mathlib\n")
    assert result.status == "not_found"
    assert result.outcome == "inconclusive"
    assert result.completion == "time_limit"


def test_slow_failed_acquisition_consumes_global_wall_budget(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    probe = FakeProbe(
        {
            "mathlib-new": ProbeUnavailable("not published"),
            "mathlib-old": execution(True),
        },
        acquisition_delay=0.25,
    )
    result = Discovery(
        catalog=sample_catalog,
        policy=DiscoveryPolicy(max_total_seconds=1, max_wall_seconds=0.2),
        probe=probe,
    ).discover_and_check("import Mathlib\n")
    assert result.status == "not_found"
    assert result.completion == "time_limit"
    assert [item.status for item in result.attempts] == ["timeout"]


@dataclass
class StuckAcquisitionProbe:
    def acquire(
        self,
        candidate: Candidate,
        *,
        timeout_seconds: float,
        cancel: threading.Event,
    ) -> AcquiredCandidate:
        del candidate, timeout_seconds
        while not cancel.wait(0.005):
            pass
        raise ProbeUnavailable("download stalled")

    def check(
        self,
        acquired: AcquiredCandidate,
        source: str,
        *,
        timeout_seconds: float,
        cancel: threading.Event,
    ) -> ProbeOutcome:
        raise AssertionError("a stuck acquisition must never reach check")


def test_acquisition_timeout_bounds_a_stuck_download(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    result = Discovery(
        catalog=sample_catalog,
        policy=DiscoveryPolicy(
            max_candidates=1,
            max_total_seconds=5,
            acquisition_timeout_seconds=0.05,
        ),
        probe=StuckAcquisitionProbe(),
    ).discover_and_check("import Mathlib\n")
    assert result.status == "not_found"
    assert result.attempts[0].status == "timeout"
    assert result.duration_seconds < 0.5


def test_lean_runtime_probe_passes_the_acquisition_budget_to_builds(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    from lean_runtime.discovery.probe import LeanRuntimeProbe

    captured: dict[str, float] = {}

    class Toolchains:
        def ensure(
            self,
            toolchain: str,
            *,
            cancel: threading.Event | None = None,
        ) -> str:
            assert cancel is not None
            captured["toolchain_ready"] = 1
            return toolchain

    def open_exact(  # type: ignore[no-untyped-def]
        lock, *, build_timeout, import_roots=(), cancel=None, allow_source_build=None
    ):
        del lock, import_roots, cancel
        captured["build_timeout"] = build_timeout
        captured["allow_source_build"] = float(bool(allow_source_build))
        return SimpleNamespace(id="env_fake")

    runtime = SimpleNamespace(
        open_exact=open_exact,
        availability="auto",
        toolchains=Toolchains(),
    )
    probe = LeanRuntimeProbe(runtime=runtime)  # type: ignore[arg-type]
    candidate = Discovery(catalog=sample_catalog).plan("import Mathlib\n").candidates[0]
    acquired = probe.acquire(candidate, timeout_seconds=123.0, cancel=threading.Event())
    assert captured["build_timeout"] == 123.0
    assert captured["toolchain_ready"] == 1
    assert captured["allow_source_build"] == 0
    assert acquired.environment_id == "env_fake"


def test_core_only_probe_uses_the_exact_toolchain_without_an_environment_artifact(
    sample_catalog,
) -> None:  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    from lean_runtime.discovery.probe import LeanRuntimeProbe

    calls: list[str] = []

    class Toolchains:
        def ensure(self, toolchain: str, **_kwargs: object) -> str:
            calls.append(f"ensure:{toolchain}")
            return toolchain

    def check(_source: str, *, toolchain: str, **_kwargs: object) -> ExecutionResult:
        calls.append(f"check:{toolchain}")
        return execution(True, toolchain=toolchain)

    runtime = SimpleNamespace(toolchains=Toolchains(), check=check)
    candidate = Discovery(catalog=sample_catalog).plan("example : True := trivial\n").candidates[0]
    probe = LeanRuntimeProbe(runtime=runtime)  # type: ignore[arg-type]

    acquired = probe.acquire(candidate, timeout_seconds=10, cancel=threading.Event())
    outcome = probe.check(
        acquired,
        "example : True := trivial\n",
        timeout_seconds=10,
        cancel=threading.Event(),
    )

    assert outcome.execution_result.ok
    assert calls == [
        f"ensure:{candidate.entry.toolchain}",
        f"check:{candidate.entry.toolchain}",
    ]


def test_probe_uses_typed_retryability_not_error_text(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    from lean_runtime.discovery.probe import LeanRuntimeProbe

    candidate = Discovery(catalog=sample_catalog).plan("import Mathlib\n").candidates[0]

    def retryable_open(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise EnvironmentError("temporary transport failure", retryable=True)

    retryable = SimpleNamespace(
        open_exact=retryable_open,
        availability="auto",
        toolchains=SimpleNamespace(ensure=lambda *_args, **_kwargs: None),
    )
    with pytest.raises(ProbeUnavailable, match="temporary transport"):
        LeanRuntimeProbe(runtime=retryable).acquire(  # type: ignore[arg-type]
            candidate, timeout_seconds=10, cancel=threading.Event()
        )

    def terminal_open(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise EnvironmentError("artifact unavailable but integrity failed")

    terminal = SimpleNamespace(
        open_exact=terminal_open,
        availability="auto",
        toolchains=SimpleNamespace(ensure=lambda *_args, **_kwargs: None),
    )
    with pytest.raises(EnvironmentError, match="artifact unavailable"):
        LeanRuntimeProbe(runtime=terminal).acquire(  # type: ignore[arg-type]
            candidate, timeout_seconds=10, cancel=threading.Event()
        )


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
    result = Discovery(
        catalog=sample_catalog,
        policy=DiscoveryPolicy(channels=("nightly",)),
    ).discover_and_check("import Missing.Module\n")
    assert result.status == "not_found"
    assert result.outcome == "no_candidate"
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


def test_probe_types_are_publicly_importable() -> None:
    from lean_runtime.discovery import AcquiredCandidate as PublicAcquired
    from lean_runtime.discovery import CandidateProbe as PublicProbe

    assert PublicAcquired is AcquiredCandidate
    assert PublicProbe is not None


def test_lean_runtime_probe_preserves_the_user_filename(monkeypatch, sample_catalog) -> None:  # type: ignore[no-untyped-def]
    import lean_runtime.discovery.probe as probe_module
    from lean_runtime.discovery.probe import LeanRuntimeProbe

    captured: dict[str, str] = {}

    class FakeEnvironment:
        id = "env_fixture"
        sparse = False

        def check(self, _source, *, filename, policy, cancel, _declaration_hints=True):
            del policy, cancel, _declaration_hints
            captured["filename"] = filename
            return execution(True)

    monkeypatch.setattr(probe_module, "Environment", FakeEnvironment)
    candidate = Discovery(catalog=sample_catalog).plan("import Mathlib\n").candidates[0]
    acquired = AcquiredCandidate(candidate, "env_fixture", handle=FakeEnvironment())
    LeanRuntimeProbe(runtime=object(), filename="Tight.lean").check(  # type: ignore[arg-type]
        acquired,
        "import Mathlib\n",
        timeout_seconds=10,
        cancel=threading.Event(),
    )
    assert captured["filename"] == "Tight.lean"


def test_discovery_enriches_only_the_terminal_best_rejection(
    monkeypatch, sample_catalog, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    import lean_runtime.discovery.api as api_module

    static = Discovery(catalog=sample_catalog, policy=DiscoveryPolicy(max_candidates=1)).plan(
        "import Mathlib\n"
    )
    rejected = engine.discover(
        "import Mathlib\n",
        static,
        FakeProbe({"mathlib-new": execution(False, stderr="unknown identifier `missing`")}),
    )

    class HintRuntime:
        home = tmp_path / "runtime"
        availability = "required"
        libraries = ()
        calls = 0

        def exact_ready_locally(self, *_args, **_kwargs) -> bool:
            return False

        def with_declaration_hints(self, _lock, result, **_kwargs):
            self.calls += 1
            return replace(result, hints=("resolved after discovery",))

    runtime = HintRuntime()
    monkeypatch.setattr(api_module, "discover", lambda *_args, **_kwargs: rejected)

    result = Discovery(catalog=sample_catalog, runtime=runtime).discover_and_check(
        "import Mathlib\n"
    )

    assert runtime.calls == 1
    assert result.best_rejection is not None
    assert result.best_rejection.execution_result is not None
    assert result.best_rejection.execution_result.hints == ("resolved after discovery",)


def test_discovery_cross_version_hints_use_retained_indexes_only(
    monkeypatch, sample_catalog, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    import lean_runtime.discovery.api as api_module

    static = Discovery(catalog=sample_catalog, policy=DiscoveryPolicy(max_candidates=1)).plan(
        "import Mathlib\n"
    )
    rejected = engine.discover(
        "import Mathlib\n",
        static,
        FakeProbe({"mathlib-new": execution(False, stderr="unknown identifier `missing`")}),
    )

    class HintRuntime:
        home = tmp_path / "runtime"
        availability = "required"
        libraries = ()
        calls: list[tuple[str, bool]] = []

        def exact_ready_locally(self, *_args, **_kwargs) -> bool:
            return False

        def with_declaration_hints(self, _lock, result, **kwargs):
            label = str(kwargs["environment_label"])
            allow_download = bool(kwargs["allow_download"])
            self.calls.append((label, allow_download))
            if label == "mathlib-old":
                return replace(result, hints=("available in the retained older index",))
            return result

    runtime = HintRuntime()
    monkeypatch.setattr(api_module, "discover", lambda *_args, **_kwargs: rejected)

    result = Discovery(catalog=sample_catalog, runtime=runtime).discover_and_check(
        "import Mathlib\n"
    )

    assert runtime.calls[:2] == [("mathlib-new", True), ("mathlib-old", False)]
    assert result.best_rejection is not None
    assert result.best_rejection.execution_result is not None
    assert result.best_rejection.execution_result.hints == (
        "available in the retained older index",
    )
