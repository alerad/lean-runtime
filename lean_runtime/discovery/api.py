"""Public pure planning and authoritative discovery APIs."""

from __future__ import annotations

import asyncio
import threading
from contextlib import suppress
from dataclasses import dataclass, field

from ..events import RuntimeEvent
from ..runtime import Runtime
from .analyzer import SourceEvidence, analyze_source
from .candidate import DiscoveryPlan
from .catalog import Catalog
from .engine import discover
from .errors import PolicyError
from .history import DecisionHint, DiscoveryHistory
from .planner import Planner
from .policy import DiscoveryPolicy
from .probe import CandidateProbe, LeanRuntimeProbe
from .result import DiscoveryResult


@dataclass(slots=True)
class Discovery:
    catalog: Catalog
    policy: DiscoveryPolicy = field(default_factory=DiscoveryPolicy)
    planner: Planner = field(default_factory=Planner)
    runtime: Runtime | None = None
    runtime_events: list[RuntimeEvent] | None = None
    probe: CandidateProbe | None = None
    filename: str = "Main.lean"

    def analyze(self, source: str) -> SourceEvidence:
        return analyze_source(source)

    def plan(self, source: str, *, evidence: SourceEvidence | None = None) -> DiscoveryPlan:
        """Return a pure static catalog plan without touching runtime state."""

        selected_evidence = evidence or self.analyze(source)
        return self.planner.plan(selected_evidence, self.catalog, self.policy)

    def _probe(self, import_roots: tuple[str, ...] = ()) -> CandidateProbe:
        if self.probe is not None and self.runtime is not None:
            raise PolicyError("configure either a Runtime or a custom probe, not both")
        if self.runtime_events is not None and self.runtime is None:
            raise PolicyError("runtime_events requires an injected Runtime")
        if self.probe is not None:
            return self.probe
        runtime = self.runtime
        events = self.runtime_events
        if runtime is None:
            availability = (
                "auto"
                if self.policy.allow_source_build
                else ("required" if self.policy.allow_download else "local")
            )
            libraries = None if self.policy.allow_download else ()
            events = []
            runtime = Runtime(
                availability=availability,
                libraries=libraries,
                on_event=events.append,
                allow_source_build=self.policy.allow_source_build,
            )
        if not self.policy.allow_download and runtime.availability != "local" and runtime.libraries:
            raise PolicyError(
                "the injected Runtime has active libraries but allow_download is false"
            )
        return LeanRuntimeProbe(
            runtime,
            events,
            import_roots,
            self.filename,
            self.policy.allow_source_build,
        )

    def order(
        self,
        source: str,
        evidence: SourceEvidence,
        plan: DiscoveryPlan,
        *,
        runtime: Runtime | None = None,
    ) -> tuple[DiscoveryPlan, DiscoveryHistory | None]:
        """Order a pure plan using cheap local state and compiler history."""

        selected_runtime = runtime or self.runtime
        if selected_runtime is None:
            return plan, None
        allowed = frozenset(candidate.entry.lock.lock_id for candidate in plan.candidates)
        history = DiscoveryHistory(selected_runtime.home)
        hint: DecisionHint | None = history.lookup(
            source,
            evidence,
            allowed_lock_ids=allowed,
        )
        local_lock_ids = frozenset(
            candidate.entry.lock.lock_id
            for candidate in plan.candidates
            if selected_runtime.exact_ready_locally(
                candidate.entry.lock,
                import_roots=evidence.imports,
            )
        )
        ordered = self.planner.order(
            plan,
            local_lock_ids=local_lock_ids,
            preferred_lock_id=hint.lock_id if hint is not None else None,
            preferred_exact=hint.exact_source if hint is not None else False,
            rejected_lock_ids=history.rejected_locks(source),
        )
        return ordered, history

    def has_local_history_hint(
        self,
        source: str,
        *,
        evidence: SourceEvidence | None = None,
    ) -> bool:
        """Return whether discovery can start directly with a known local lock."""

        if self.runtime is None:
            return False
        selected_evidence = evidence or self.analyze(source)
        plan = self.plan(source, evidence=selected_evidence)
        allowed = frozenset(candidate.entry.lock.lock_id for candidate in plan.candidates)
        hint = DiscoveryHistory(self.runtime.home).lookup(
            source,
            selected_evidence,
            allowed_lock_ids=allowed,
        )
        if hint is None:
            return False
        entry = self.catalog.entry_for_lock(hint.lock_id)
        return entry is not None and self.runtime.exact_ready_locally(
            entry.lock,
            import_roots=selected_evidence.imports,
        )

    def discover_and_check(
        self,
        source: str,
        *,
        evidence: SourceEvidence | None = None,
        cancel: threading.Event | None = None,
    ) -> DiscoveryResult:
        selected_evidence = evidence or self.analyze(source)
        static_plan = self.plan(source, evidence=selected_evidence)
        selected_probe = (
            None
            if static_plan.explicit_lock is not None or not static_plan.candidates
            else self._probe(static_plan.evidence.imports)
        )
        runtime_for_order = (
            selected_probe.runtime if isinstance(selected_probe, LeanRuntimeProbe) else None
        )
        selected_plan, history = (
            self.order(source, selected_evidence, static_plan, runtime=runtime_for_order)
            if runtime_for_order is not None
            else (static_plan, None)
        )
        result = discover(source, selected_plan, selected_probe, cancel=cancel)
        if history is not None:
            history.remember_rejections(
                source,
                tuple(
                    attempt.lock_id
                    for attempt in result.attempts
                    if attempt.status == "lean_rejected"
                ),
            )
            if result.status == "found" and result.lock_id is not None:
                history.remember_success(source, selected_evidence, result.lock_id)
        return result

    async def discover_and_check_async(self, source: str) -> DiscoveryResult:
        cancel = threading.Event()
        task = asyncio.create_task(
            asyncio.to_thread(self.discover_and_check, source, cancel=cancel)
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancel.set()
            with suppress(Exception):
                await asyncio.shield(task)
            raise


def plan(
    source: str,
    *,
    catalog: Catalog,
    policy: DiscoveryPolicy | None = None,
) -> DiscoveryPlan:
    return Discovery(catalog=catalog, policy=policy or DiscoveryPolicy()).plan(source)


def discover_and_check(
    source: str,
    *,
    catalog: Catalog,
    policy: DiscoveryPolicy | None = None,
    runtime: Runtime | None = None,
    filename: str = "Main.lean",
) -> DiscoveryResult:
    return Discovery(
        catalog=catalog,
        policy=policy or DiscoveryPolicy(),
        runtime=runtime,
        filename=filename,
    ).discover_and_check(source)
