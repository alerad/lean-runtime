"""Bounded discovery policy."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .errors import PolicyError


def _positive_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyError(f"{name} must be a positive finite number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise PolicyError(f"{name} must be a positive finite number")
    return number


@dataclass(frozen=True, slots=True)
class DiscoveryPolicy:
    """Limits and preferences for a discovery search.

    ``max_total_seconds`` bounds aggregate compiler-probe time.
    ``max_wall_seconds`` covers the complete operation, including acquisition,
    while ``acquisition_timeout_seconds`` additionally bounds one acquisition.
    """

    max_candidates: int = 3
    max_total_seconds: float = 90.0
    max_wall_seconds: float = 1800.0
    channels: tuple[str, ...] = ("stable",)
    allow_download: bool = True
    allow_source_build: bool = False
    max_remote_acquisitions: int = 2
    candidate_timeout_seconds: float | None = None
    acquisition_timeout_seconds: float = 1800.0

    def __post_init__(self) -> None:
        if isinstance(self.max_candidates, bool) or not isinstance(self.max_candidates, int):
            raise PolicyError("max_candidates must be a positive integer")
        if self.max_candidates <= 0:
            raise PolicyError("max_candidates must be a positive integer")
        if (
            isinstance(self.max_remote_acquisitions, bool)
            or not isinstance(self.max_remote_acquisitions, int)
            or self.max_remote_acquisitions < 0
        ):
            raise PolicyError("max_remote_acquisitions must be a nonnegative integer")
        _positive_number(self.max_total_seconds, "max_total_seconds")
        _positive_number(self.max_wall_seconds, "max_wall_seconds")
        _positive_number(self.acquisition_timeout_seconds, "acquisition_timeout_seconds")
        if self.candidate_timeout_seconds is not None:
            _positive_number(self.candidate_timeout_seconds, "candidate_timeout_seconds")
        if not self.channels or any(
            not item or not isinstance(item, str) for item in self.channels
        ):
            raise PolicyError("channels must contain non-empty strings")
        if len(self.channels) != len(set(self.channels)):
            raise PolicyError("channels must not contain duplicates")
        boolean_fields = (
            "allow_download",
            "allow_source_build",
        )
        if any(not isinstance(getattr(self, name), bool) for name in boolean_fields):
            raise PolicyError("discovery policy flags must be Boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_candidates": self.max_candidates,
            "max_total_seconds": self.max_total_seconds,
            "max_wall_seconds": self.max_wall_seconds,
            "channels": list(self.channels),
            "allow_download": self.allow_download,
            "allow_source_build": self.allow_source_build,
            "max_remote_acquisitions": self.max_remote_acquisitions,
            "candidate_timeout_seconds": self.candidate_timeout_seconds,
            "acquisition_timeout_seconds": self.acquisition_timeout_seconds,
        }
