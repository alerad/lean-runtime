"""Backend-independent execution policy requests."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

NetworkPolicy = Literal["inherit", "disabled"]

_BYTE_UNITS = {
    "": 1,
    "b": 1,
    "k": 10**3,
    "kb": 10**3,
    "kib": 2**10,
    "m": 10**6,
    "mb": 10**6,
    "mib": 2**20,
    "g": 10**9,
    "gb": 10**9,
    "gib": 2**30,
    "t": 10**12,
    "tb": 10**12,
    "tib": 2**40,
}
_BYTE_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*([a-zA-Z]*)")


def parse_byte_size(value: str) -> int:
    """Parse a human byte size such as '500MiB', '1.5 GB', or '1048576'."""
    match = _BYTE_SIZE.fullmatch(value.strip())
    unit = _BYTE_UNITS.get(match.group(2).lower()) if match else None
    if match is None or unit is None:
        raise ValueError(f"invalid byte size: {value!r}")
    return int(float(match.group(1)) * unit)


def format_byte_size(value: int) -> str:
    """Format bytes for humans using binary units."""
    if value < 0:
        raise ValueError("byte size must be nonnegative")
    if value < 1024:
        return f"{value} B"
    scaled = float(value)
    for unit in ("KiB", "MiB", "GiB"):
        scaled /= 1024
        if scaled < 1024:
            return f"{scaled:.1f} {unit}".replace(".0 ", " ")
    return f"{scaled / 1024:.1f} TiB".replace(".0 ", " ")


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
