"""Stable result models returned by the runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Severity = Literal["error", "warning", "information", "unknown"]
TimingPhase = Literal[
    "discovery",
    "toolchain",
    "resolution",
    "source_acquisition",
    "cache_lookup",
    "cache_download",
    "artifact_hydration",
    "build",
    "publication",
    "environment_open",
    "instance_creation",
    "input_staging",
    "command_preparation",
    "workspace_lock",
    "header_snapshot",
    "execution",
    "result_collection",
    "result_publication",
    "cleanup",
]
TIMING_PHASES = frozenset(
    {
        "discovery",
        "toolchain",
        "resolution",
        "source_acquisition",
        "cache_lookup",
        "cache_download",
        "artifact_hydration",
        "build",
        "publication",
        "environment_open",
        "instance_creation",
        "input_staging",
        "command_preparation",
        "workspace_lock",
        "header_snapshot",
        "execution",
        "result_collection",
        "result_publication",
        "cleanup",
    }
)


@dataclass(frozen=True, slots=True)
class PhaseTiming:
    """One stable, user-facing operation phase measured with a monotonic clock."""

    phase: TimingPhase
    duration_ms: int
    performed: bool = True

    def __post_init__(self) -> None:
        if self.phase not in TIMING_PHASES:
            raise ValueError(f"unsupported timing phase: {self.phase!r}")
        if self.duration_ms < 0:
            raise ValueError("timing duration must be nonnegative")


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One compiler diagnostic parsed from Lean's textual output."""

    severity: Severity
    message: str
    file: str | None = None
    line: int | None = None
    column: int | None = None

    @property
    def location(self) -> str | None:
        """Return ``file:line:column`` when the diagnostic names a position."""
        if self.file is None:
            return None
        if self.line is None:
            return self.file
        if self.column is None:
            return f"{self.file}:{self.line}"
        return f"{self.file}:{self.line}:{self.column}"

    def __repr__(self) -> str:
        summary = self.message.splitlines()[0] if self.message else ""
        if len(summary) > 60:
            summary = summary[:57] + "..."
        location = f", {self.location}" if self.location is not None else ""
        return f"Diagnostic({self.severity}{location}, {summary!r})"


@dataclass(frozen=True, slots=True)
class PackageProvenance:
    name: str
    url: str
    revision: str
    tree_hash: str


@dataclass(frozen=True, slots=True)
class ProjectProvenance:
    root: str
    workspace_digest: str
    lakefile_digest: str
    manifest_digest: str | None
    git_revision: str | None
    git_dirty: bool | None
    workspace_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ExecutionProvenance:
    environment_id: str | None
    execution_id: str
    request_digest: str
    lock_id: str | None
    toolchain: str
    packages: tuple[PackageProvenance, ...]
    platform: dict[str, str]
    backend: str
    requested_policy: dict[str, Any]
    enforced_policy_fields: tuple[str, ...]
    source_digest: str
    started_at: str
    project: ProjectProvenance | None = None
    program_id: str | None = None
    program_copy_id: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """The reproducible outcome of one Lean or Lake invocation."""

    ok: bool
    exit_code: int
    toolchain: str
    command: tuple[str, ...]
    cwd: str
    stdout: str
    stderr: str
    elapsed_seconds: float
    timed_out: bool = False
    cancelled: bool = False
    output_truncated: bool = False
    diagnostics: tuple[Diagnostic, ...] = field(default_factory=tuple)
    provenance: ExecutionProvenance | None = None
    timings: tuple[PhaseTiming, ...] = field(default_factory=tuple)
    hints: tuple[str, ...] = field(default_factory=tuple)

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        """Return only error-severity diagnostics."""
        return tuple(item for item in self.diagnostics if item.severity == "error")

    @property
    def warnings(self) -> tuple[Diagnostic, ...]:
        """Return only warning-severity diagnostics."""
        return tuple(item for item in self.diagnostics if item.severity == "warning")

    @property
    def first_error(self) -> Diagnostic | None:
        """Return the first error diagnostic, if any."""
        return next(iter(self.errors), None)

    @property
    def environment_id(self) -> str | None:
        return self.provenance.environment_id if self.provenance else None

    @property
    def execution_id(self) -> str | None:
        return self.provenance.execution_id if self.provenance else None

    @property
    def lock_id(self) -> str | None:
        return self.provenance.lock_id if self.provenance else None

    def __repr__(self) -> str:
        parts = [
            f"ok={self.ok}",
            f"errors={len(self.errors)}",
            f"elapsed={self.elapsed_seconds:.2f}s",
        ]
        first = self.first_error
        if first is not None and first.location is not None:
            parts.append(f"first_error={first.location!r}")
        if self.timed_out:
            parts.append("timed_out=True")
        if self.cancelled:
            parts.append("cancelled=True")
        return f"ExecutionResult({', '.join(parts)})"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return asdict(self)

    def raise_for_error(self) -> ExecutionResult:
        """Return this result when accepted, otherwise raise ``LeanCheckError``."""
        if not self.ok:
            from .errors import LeanCheckError

            raise LeanCheckError(self)
        return self
