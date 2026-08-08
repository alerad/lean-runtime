"""The narrow boundary between discovery orchestration and Lean Runtime."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol

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


class CandidateProbe(Protocol):
    def check(
        self,
        candidate: Candidate,
        source: str,
        *,
        timeout_seconds: float,
        cancel: threading.Event,
    ) -> ProbeOutcome: ...


@dataclass(slots=True)
class LeanRuntimeProbe:
    runtime: Runtime
    events: list[RuntimeEvent] | None = None

    def check(
        self,
        candidate: Candidate,
        source: str,
        *,
        timeout_seconds: float,
        cancel: threading.Event,
    ) -> ProbeOutcome:
        event_offset = len(self.events) if self.events is not None else 0
        try:
            environment = self.runtime.open_exact(candidate.entry.lock, cancel=cancel)
        except MaterializationError as exc:
            if exc.phase in {"lock-validation", "source-validation", "bundle-validation"}:
                raise ProbeIntegrityFailure(str(exc)) from exc
            if exc.phase == "acquisition":
                raise ProbeUnavailable(str(exc)) from exc
            raise
        except (DownloadUnavailable, ToolchainError) as exc:
            raise ProbeUnavailable(str(exc)) from exc
        except EnvironmentError as exc:
            detail = str(exc)
            if "unavailable" in detail or "no compatible downloadable environment" in detail:
                raise ProbeUnavailable(detail) from exc
            if self.runtime.availability == "required":
                raise ProbeIntegrityFailure(detail) from exc
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
        result = environment.check(
            source,
            policy=ExecutionPolicy(timeout_seconds=timeout_seconds),
            cancel=cancel,
        )
        return ProbeOutcome(
            environment_id=environment.id,
            execution_result=result,
            acquisition=acquisition,
        )
