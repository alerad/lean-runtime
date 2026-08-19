"""Exceptions raised by :mod:`lean_runtime`."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ExecutionResult


class LeanRuntimeError(RuntimeError):
    """Base class for runtime infrastructure failures."""


class LeanCheckError(LeanRuntimeError):
    """A completed Lean invocation rejected its input."""

    def __init__(self, result: ExecutionResult) -> None:
        self.result = result
        diagnostic = next(
            (item.message for item in result.diagnostics if item.severity == "error"), None
        )
        detail = diagnostic or result.stderr.strip() or result.stdout.strip()
        message = f"Lean check failed with exit code {result.exit_code}"
        super().__init__(f"{message}: {detail}" if detail else message)


class ToolchainError(LeanRuntimeError):
    """A Lean toolchain could not be resolved or installed."""


class ProjectError(LeanRuntimeError):
    """A Lean project is missing required configuration."""


class ProjectNotFoundError(ProjectError):
    """No pinned Lean project contains the requested path."""


class SpecificationError(LeanRuntimeError):
    """An environment specification is invalid."""


class ResolutionError(LeanRuntimeError):
    """Lake could not resolve an environment specification."""

    def __init__(
        self,
        message: str,
        *,
        phase: str = "resolution",
        command: tuple[str, ...] = (),
        exit_code: int | None = None,
        output: str = "",
    ) -> None:
        super().__init__(message)
        self.phase = phase
        self.command = command
        self.exit_code = exit_code
        self.output = output


class EnvironmentError(LeanRuntimeError):
    """A content-addressed environment could not be opened or built."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "environment_error",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.retryable = retryable


class DownloadUnavailable(EnvironmentError):
    """A prebuilt cache had no usable artifact or could not be reached."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "remote_candidate_unavailable",
        retryable: bool = True,
    ) -> None:
        super().__init__(message, reason_code=reason_code, retryable=retryable)


class RegistryRequestError(DownloadUnavailable):
    """An OCI registry request failed with machine-readable retry semantics."""

    def __init__(
        self,
        message: str,
        *,
        operation: str,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(
            message,
            reason_code=(
                "remote_candidate_missing" if status_code == 404 else "remote_candidate_unavailable"
            ),
            retryable=retryable,
        )
        self.operation = operation
        self.status_code = status_code
        self.retryable = retryable


class CredentialAcquisitionError(EnvironmentError):
    """A configured credential provider could not produce usable credentials."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        failure_kind: str,
        retryable: bool,
    ) -> None:
        super().__init__(
            message,
            reason_code=f"credential_{failure_kind}",
            retryable=retryable,
        )
        self.provider = provider
        self.failure_kind = failure_kind


class PublicationError(EnvironmentError):
    """A required publication did not reach a remotely verified terminal state."""

    def __init__(
        self,
        message: str,
        *,
        phase: str,
        registry: str,
        status_code: int | None = None,
        retryable: bool = False,
        published: bool = False,
        partial: bool = False,
        credential_source: str = "anonymous",
        username: str | None = None,
        hint: str | None = None,
        attempted_provider: str | None = None,
        auth_failure_kind: str | None = None,
    ) -> None:
        super().__init__(message)
        self.phase = phase
        self.registry = registry
        self.status_code = status_code
        self.retryable = retryable
        self.published = published
        self.partial = partial
        self.credential_source = credential_source
        self.username = username
        self.hint = hint
        self.attempted_provider = attempted_provider
        self.auth_failure_kind = auth_failure_kind

    @property
    def exit_code(self) -> int:
        if self.partial:
            return 5
        if self.phase == "credential_acquisition":
            return 4 if self.retryable else 3
        if self.status_code in {401, 403}:
            return 3
        if self.retryable:
            return 4
        return 5

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "registry": self.registry,
            "status_code": self.status_code,
            "retryable": self.retryable,
            "published": self.published,
            "partial": self.partial,
            "credential_source": self.credential_source,
            "username": self.username,
            "hint": self.hint,
            "attempted_provider": self.attempted_provider,
            "auth_failure_kind": self.auth_failure_kind,
            "message": str(self),
        }


class DownloadLimitExceeded(EnvironmentError):
    """An acquisition needs more bytes than the configured download limit.

    Deliberately not a :class:`DownloadUnavailable`: exceeding a policy limit
    must fail the operation instead of silently falling back to another
    library or a source build.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, reason_code="download_limit_exceeded", retryable=False)


class MaterializationError(EnvironmentError):
    """Locked sources or build artifacts could not be materialized."""

    def __init__(
        self,
        message: str,
        *,
        phase: str,
        command: tuple[str, ...] = (),
        exit_code: int | None = None,
        output: str = "",
    ) -> None:
        super().__init__(message)
        self.phase = phase
        self.command = command
        self.exit_code = exit_code
        self.output = output


class PolicyError(LeanRuntimeError):
    """An execution policy cannot be enforced by the selected backend."""
