"""Public planning and authoritative discovery APIs."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field

from ..events import RuntimeEvent
from ..runtime import Runtime
from .analyzer import SourceEvidence, analyze_source
from .candidate import AvailabilityObservation, DiscoveryPlan
from .catalog import Catalog
from .engine import discover
from .errors import PolicyError
from .planner import Planner
from .policy import DiscoveryPolicy
from .probe import CandidateProbe, LeanRuntimeProbe
from .result import DiscoveryResult


@dataclass(slots=True)
class Discovery:
    catalog: Catalog
    policy: DiscoveryPolicy = field(default_factory=DiscoveryPolicy)
    availability: Mapping[str, AvailabilityObservation] = field(default_factory=dict)
    planner: Planner = field(default_factory=Planner)
    runtime: Runtime | None = None
    runtime_events: list[RuntimeEvent] | None = None
    probe: CandidateProbe | None = None

    def analyze(self, source: str) -> SourceEvidence:
        return analyze_source(source)

    def plan(self, source: str) -> DiscoveryPlan:
        return self.planner.plan(self.analyze(source), self.catalog, self.policy, self.availability)

    def _probe(self) -> CandidateProbe:
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
            )
        if not self.policy.allow_source_build and runtime.availability == "auto":
            raise PolicyError(
                "the injected Runtime permits source fallback but allow_source_build is false"
            )
        if not self.policy.allow_download and runtime.availability != "local" and runtime.libraries:
            raise PolicyError(
                "the injected Runtime has active libraries but allow_download is false"
            )
        return LeanRuntimeProbe(runtime, events)

    def discover_and_check(
        self,
        source: str,
        *,
        cancel: threading.Event | None = None,
    ) -> DiscoveryResult:
        selected_plan = self.plan(source)
        selected_probe = (
            None
            if selected_plan.explicit_lock is not None or not selected_plan.candidates
            else self._probe()
        )
        return discover(source, selected_plan, selected_probe, cancel=cancel)

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
) -> DiscoveryResult:
    return Discovery(
        catalog=catalog,
        policy=policy or DiscoveryPolicy(),
        runtime=runtime,
    ).discover_and_check(source)
