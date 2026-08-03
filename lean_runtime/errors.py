"""Exceptions raised by :mod:`lean_runtime`."""


class LeanRuntimeError(RuntimeError):
    """Base class for runtime infrastructure failures."""


class ToolchainError(LeanRuntimeError):
    """A Lean toolchain could not be resolved or installed."""


class ProjectError(LeanRuntimeError):
    """A Lean project is missing required configuration."""


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
