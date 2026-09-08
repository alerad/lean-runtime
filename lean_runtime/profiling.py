"""Repeated check profiles composed from ordinary execution results."""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass
from typing import Any, Protocol

from .models import ExecutionResult


class Checker(Protocol):
    """Anything that can check one Lean source: an environment or a project."""

    def check(self, source: str, *, filename: str = ...) -> ExecutionResult: ...


@dataclass(frozen=True, slots=True)
class ProfileReport:
    subject: str
    warmup: int
    results: tuple[ExecutionResult, ...]
    duration_seconds: float

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(item.ok for item in self.results)

    @property
    def durations_ms(self) -> tuple[int, ...]:
        return tuple(round(item.elapsed_seconds * 1000) for item in self.results)

    def statistics(self) -> dict[str, float | int | None]:
        values = self.durations_ms
        if not values:
            return {"min": None, "median": None, "mean": None, "p95": None, "max": None}
        ordered = sorted(values)
        p95 = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))] if len(values) >= 5 else None
        return {
            "min": min(values),
            "median": statistics.median(values),
            "mean": statistics.fmean(values),
            "p95": p95,
            "max": max(values),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "ok": self.ok,
            "warmup": self.warmup,
            "samples": len(self.results),
            "duration_ms": round(self.duration_seconds * 1000),
            "statistics_ms": self.statistics(),
            "results": [item.to_dict() for item in self.results],
        }


def run_profile(
    environment: Checker, source: str, *, filename: str, warmup: int, repeat: int
) -> ProfileReport:
    if warmup < 0 or repeat < 1 or warmup > 100 or repeat > 1000:
        raise ValueError("profile requires 0..100 warmups and 1..1000 samples")
    started = time.monotonic()
    for _ in range(warmup):
        result = environment.check(source, filename=filename)
        if not result.ok:
            # The failing warmup is the report: it says why no samples exist.
            return ProfileReport(filename, warmup, (result,), time.monotonic() - started)
    started = time.monotonic()
    results: list[ExecutionResult] = []
    for _ in range(repeat):
        result = environment.check(source, filename=filename)
        results.append(result)
        if not result.ok:
            break
    return ProfileReport(filename, warmup, tuple(results), time.monotonic() - started)
