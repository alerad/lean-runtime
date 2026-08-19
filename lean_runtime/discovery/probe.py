"""The narrow boundary between discovery orchestration and Lean Runtime."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol

from ..environments import Environment
from ..errors import (
    DownloadUnavailable,
    EnvironmentError,
    MaterializationError,
    ToolchainError,
)
from ..events import RuntimeEvent
from ..models import ExecutionResult
from ..policies import ExecutionPolicy
from ..runtime import Runtime
from .candidate import Candidate
from .result import Acquisition


class ProbeUnavailable(RuntimeError):
    """The exact candidate cannot be acquired under the active Runtime policy."""


class ProbeIntegrityFailure(RuntimeError):
    """Runtime rejected the identity or integrity of candidate material."""


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    environment_id: str
    execution_result: ExecutionResult
    acquisition: Acquisition = "unknown"


@dataclass(frozen=True, slots=True)
class AcquiredCandidate:
    """A candidate whose exact environment is ready for compiler probes."""

    candidate: Candidate
    environment_id: str
    acquisition: Acquisition = "unknown"
    handle: object | None = None


class CandidateProbe(Protocol):
    def acquire(
        self,
        candidate: Candidate,
        *,
        timeout_seconds: float,
        cancel: threading.Event,
    ) -> AcquiredCandidate: ...

    def check(
        self,
        acquired: AcquiredCandidate,
        source: str,
        *,
        timeout_seconds: float,
        cancel: threading.Event,
    ) -> ProbeOutcome: ...


@dataclass(slots=True)
class LeanRuntimeProbe:
    runtime: Runtime
    events: list[RuntimeEvent] | None = None
    import_roots: tuple[str, ...] = ()
    filename: str = "Main.lean"
    allow_source_build: bool = False

    def acquire(
        self,
        candidate: Candidate,
        *,
        timeout_seconds: float,
        cancel: threading.Event,
    ) -> AcquiredCandidate:
        event_offset = len(self.events) if self.events is not None else 0
        emitter = getattr(self.runtime, "events", None)
        if emitter is not None:
            remembered = any(
                reason.code in {"RANK_EXACT_DECISION", "RANK_HEADER_DECISION"}
                for reason in candidate.reasons
            )
            emitter.emit(
                "discovery.candidate_started",
                f"Trying {candidate.entry.id}",
                candidate=candidate.entry.id,
                toolchain=candidate.entry.toolchain,
                remembered=remembered,
            )
        try:
            environment = self.runtime.open_exact(
                candidate.entry.lock,
                build_timeout=timeout_seconds,
                import_roots=self.import_roots,
                cancel=cancel,
                allow_source_build=self.allow_source_build,
            )
            # Acquisition is complete only when the exact candidate can invoke
            # Lean without downloading or installing anything during the
            # compiler probe.
            self.runtime.toolchains.ensure(candidate.entry.toolchain, cancel=cancel)
        except MaterializationError as exc:
            if exc.phase in {"lock-validation", "source-validation", "bundle-validation"}:
                raise ProbeIntegrityFailure(str(exc)) from exc
            if exc.phase == "acquisition":
                raise ProbeUnavailable(str(exc)) from exc
            raise
        except (DownloadUnavailable, ToolchainError) as exc:
            raise ProbeUnavailable(str(exc)) from exc
        except EnvironmentError as exc:
            if exc.retryable:
                raise ProbeUnavailable(str(exc)) from exc
            if self.runtime.availability == "required":
                raise ProbeIntegrityFailure(str(exc)) from exc
            raise
        acquisition: Acquisition = "unknown"
        if self.events is not None:
            kinds = {event.kind for event in self.events[event_offset:]}
            if "library.verified" in kinds:
                acquisition = "downloaded"
            elif "environment.build_started" in kinds:
                acquisition = "source_built"
            else:
                acquisition = "local"
        return AcquiredCandidate(
            candidate=candidate,
            environment_id=environment.id,
            acquisition=acquisition,
            handle=environment,
        )

    def check(
        self,
        acquired: AcquiredCandidate,
        source: str,
        *,
        timeout_seconds: float,
        cancel: threading.Event,
    ) -> ProbeOutcome:
        environment = acquired.handle
        if not isinstance(environment, Environment):
            raise ProbeUnavailable("acquired candidate does not hold an open environment")
        try:
            result = environment.check(
                source,
                filename=self.filename,
                policy=ExecutionPolicy(timeout_seconds=timeout_seconds),
                cancel=cancel,
            )
        except DownloadUnavailable as exc:
            raise ProbeUnavailable(str(exc)) from exc
        return ProbeOutcome(
            environment_id=environment.id,
            execution_result=result,
            acquisition=acquired.acquisition,
        )
