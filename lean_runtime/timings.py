"""Stable timing presentation shared by CLI surfaces."""

from __future__ import annotations

from collections.abc import Iterable

from .models import PhaseTiming


def render_timings(timings: Iterable[PhaseTiming]) -> str:
    rows = tuple(item for item in timings if item.performed)
    if not rows:
        return "Timings\n  no measured phases"
    width = max(len(item.phase) for item in rows)
    return "Timings\n" + "\n".join(
        f"  {item.phase.replace('_', ' '):<{width}}  {item.duration_ms} ms" for item in rows
    )
