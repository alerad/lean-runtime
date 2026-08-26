"""Turn a subprocess's output lines into structured progress events.

Lake reports every build step as ``[n/m] Building X`` (prefixed with ``✔``,
``✖`` or ``⚠`` once finished), so those lines become ``process.progress``
events carrying ``current``/``total``. Every other non-empty line becomes a
throttled ``process.output`` heartbeat, which is what keeps ``lake exe cache
get`` and similar chatty-but-unstructured tools from looking hung.
"""

from __future__ import annotations

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
