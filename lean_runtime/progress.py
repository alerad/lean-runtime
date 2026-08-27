"""Turn a subprocess's output lines into structured progress events.

Lake reports every build step as ``[n/m] Building X`` (prefixed with ``✔``,
``✖`` or ``⚠`` once finished), so those lines become ``process.progress``
events carrying ``current``/``total``. Every other non-empty line becomes a
throttled ``process.output`` heartbeat, which is what keeps ``lake exe cache
get`` and similar chatty-but-unstructured tools from looking hung.
"""

from __future__ import annotations

import inspect
import re
import time
from collections.abc import Callable
from typing import Any

_LAKE_STEP = re.compile(r"^\s*(?:[✔✖⚠√×!?]\s*)?\[(\d+)/(\d+)\]\s*(?P<detail>.*?)\s*$")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_MAX_LINE = 200


class OutputProgress:
    """Feed subprocess output lines in; structured events come out.

    ``emit`` has the ``EventEmitter.emit`` signature. Progress events are
    rate-limited to ``progress_interval`` seconds (the final step is always
    emitted), heartbeats to ``output_interval`` seconds.
    """

    def __init__(
        self,
        emit: Callable[..., None],
        *,
        label: str,
        clock: Callable[[], float] = time.monotonic,
        progress_interval: float = 0.1,
        output_interval: float = 1.0,
    ) -> None:
        self._emit = emit
        self._label = label
        self._clock = clock
        self._progress_interval = progress_interval
        self._output_interval = output_interval
        self._last_progress_at: float | None = None
        self._last_output_at: float | None = None
        self._pending_step: tuple[int, int, str] | None = None
        self._emitted_step: tuple[int, int, str] | None = None

    def line(self, text: str) -> None:
        text = _ANSI.sub("", text).strip()
        if not text:
            return
        now = self._clock()
        match = _LAKE_STEP.match(text)
        if match is not None:
            step = (int(match.group(1)), int(match.group(2)), match.group("detail")[:_MAX_LINE])
            self._pending_step = step
            due = (
                self._last_progress_at is None
                or now - self._last_progress_at >= self._progress_interval
            )
            if due or step[0] >= step[1]:
                self._emit_step(step, now)
            return
        if self._last_output_at is None or now - self._last_output_at >= self._output_interval:
            self._last_output_at = now
            self._emit(
                "process.output",
                f"{self._label}: {text[:_MAX_LINE]}",
                phase="execution",
                label=self._label,
                line=text[:_MAX_LINE],
            )

    def finish(self) -> None:
        """Flush the last step that rate limiting held back."""
        if self._pending_step is not None and self._pending_step != self._emitted_step:
            self._emit_step(self._pending_step, self._clock())

    def _emit_step(self, step: tuple[int, int, str], now: float) -> None:
        current, total, detail = step
        self._last_progress_at = now
        self._emitted_step = step
        data: dict[str, Any] = {
            "label": self._label,
            "current": current,
            "total": total,
            "detail": detail,
        }
        self._emit(
            "process.progress",
            f"{self._label}: {current}/{total} {detail}".rstrip(),
            phase="execution",
            **data,
        )


class CountedProgress:
    """Emit one counted event kind for a loop, rate-limited to ``interval`` seconds.

    The final step is always emitted so a bar reaches 100%.
    """

    def __init__(
        self,
        emit: Callable[..., None],
        kind: str,
        label: str,
        total: int,
        *,
        phase: str | None = None,
        unit: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        interval: float = 0.2,
    ) -> None:
        self._emit = emit
        self.kind = kind
        self.label = label
        self.total = max(total, 0)
        self.current = 0
        self._phase = phase
        self._unit = unit
        self._clock = clock
        self._interval = interval
        self._last_at: float | None = None

    def start(self, detail: str = "") -> None:
        """Announce the work before its first potentially slow step."""
        self._emit_current(detail, self._clock())

    def advance(self, detail: str = "", *, to: int | None = None) -> None:
        self.current = min(self.total, self.current + 1 if to is None else to)
        now = self._clock()
        due = self._last_at is None or now - self._last_at >= self._interval
        if not due and self.current < self.total:
            return
        self._emit_current(detail, now)

    def _emit_current(self, detail: str, now: float) -> None:
        self._last_at = now
        suffix = f" {detail}" if detail else ""
        data: dict[str, Any] = {
            "label": self.label,
            "current": self.current,
            "total": self.total,
            "detail": detail[:_MAX_LINE],
        }
        if self._unit is not None:
            data["unit"] = self._unit
        self._emit(
            self.kind,
            f"{self.label}: {self.current}/{self.total}{suffix}",
            phase=self._phase,
            **data,
        )


def observer_arguments(backend: Any, observer: OutputProgress) -> dict[str, Any]:
    """``on_output`` for backends that accept it; nothing for older ones."""
    try:
        supported = "on_output" in inspect.signature(backend.execute).parameters
    except (TypeError, ValueError):
        supported = False
    return {"on_output": observer.line} if supported else {}
