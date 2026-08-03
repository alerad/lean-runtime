"""Backend-independent execution policy requests."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

NetworkPolicy = Literal["inherit", "disabled"]


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    timeout_seconds: float = 120
    max_output_bytes: int = 1_000_000
    memory_mb: int | None = None
    cpu_seconds: int | None = None
    network: NetworkPolicy = "inherit"

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_output_bytes < 0:
            raise ValueError("max_output_bytes must be nonnegative")
        if self.memory_mb is not None and self.memory_mb <= 0:
            raise ValueError("memory_mb must be positive")
        if self.cpu_seconds is not None and self.cpu_seconds <= 0:
            raise ValueError("cpu_seconds must be positive")
        if self.network not in {"inherit", "disabled"}:
            raise ValueError(f"unsupported network policy: {self.network!r}")

    @classmethod
    def trusted_local(cls, **kwargs: Any) -> ExecutionPolicy:
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
