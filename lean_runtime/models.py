"""Stable result models returned by the runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Severity = Literal["error", "warning", "information", "unknown"]


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One compiler diagnostic parsed from Lean's textual output."""

    severity: Severity
    message: str
    file: str | None = None
    line: int | None = None
    column: int | None = None


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

    @property
    def environment_id(self) -> str | None:
        return self.provenance.environment_id if self.provenance else None

    @property
    def execution_id(self) -> str | None:
        return self.provenance.execution_id if self.provenance else None

    @property
    def lock_id(self) -> str | None:
        return self.provenance.lock_id if self.provenance else None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return asdict(self)
