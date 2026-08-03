"""Structured progress events emitted by long-running runtime operations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """One observable lifecycle transition."""

    kind: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


EventCallback = Callable[[RuntimeEvent], None]


class EventEmitter:
    """Null-safe adapter shared by runtime components."""

    def __init__(self, callback: EventCallback | None = None) -> None:
        self.callback = callback

    def emit(self, kind: str, message: str, **data: Any) -> None:
        if self.callback is not None:
            self.callback(RuntimeEvent(kind=kind, message=message, data=data))
