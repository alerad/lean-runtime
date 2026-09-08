"""Structured progress events emitted by long-running runtime operations."""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Iterator
from concurrent.futures import Executor, Future
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import partial
from typing import Any, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """One observable lifecycle transition."""

    kind: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    phase: str | None = None
    current_bytes: int | None = None
    total_bytes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for key in ("phase", "current_bytes", "total_bytes"):
            if value[key] is None:
                del value[key]
        return value


EventCallback = Callable[[RuntimeEvent], None]


class EventEmitter:
    """Null-safe adapter shared by runtime components.

    Observers are strictly observational: an exception raised by the callback
    is recorded on the emitter (first failure kept, all counted) and never
    propagates into the operation being observed, so a broken renderer cannot
    fail a publication after its commit or trigger a rollback.
    """

    def __init__(self, callback: EventCallback | None = None) -> None:
        self.callback = callback
        self.observer_failures = 0
        self.first_observer_failure: BaseException | None = None

    def emit(
        self,
        kind: str,
        message: str,
        *,
        phase: str | None = None,
        current_bytes: int | None = None,
        total_bytes: int | None = None,
        **data: Any,
    ) -> None:
        if self.callback is None:
            return
        event = RuntimeEvent(
            kind=kind,
            message=message,
            data=data,
            phase=phase,
            current_bytes=current_bytes,
            total_bytes=total_bytes,
        )
        try:
            self.callback(event)
        except Exception as exc:  # noqa: BLE001 - observers must never break operations
            self.observer_failures += 1
            if self.first_observer_failure is None:
                self.first_observer_failure = exc


_current_emitter: contextvars.ContextVar[EventEmitter | None] = contextvars.ContextVar(
    "lean_runtime_events", default=None
)
_NULL_EMITTER = EventEmitter(None)


def current() -> EventEmitter:
    """The emitter of the runtime driving this context, or a null emitter.

    Deep helpers such as tree hashing and import parsing report counted
    progress through this instead of threading an emitter through every
    signature between them and the runtime.
    """
    return _current_emitter.get() or _NULL_EMITTER


def activate(emitter: EventEmitter) -> contextvars.Token[EventEmitter | None]:
    """Make ``emitter`` the one :func:`current` returns in this context."""
    return _current_emitter.set(emitter)


@contextmanager
def activated(emitter: EventEmitter) -> Iterator[EventEmitter]:
    """Scope ``emitter`` as the current one for the duration of the block."""
    token = _current_emitter.set(emitter)
    try:
        yield emitter
    finally:
        _current_emitter.reset(token)


def submit(
    executor: Executor, function: Callable[..., T], /, *args: Any, **kwargs: Any
) -> Future[T]:
    """Submit work to a pool with the caller's context (and so its emitter) attached.

    Pool threads do not inherit context variables; without this, deep helpers
    in the worker would report progress to a null emitter.
    """
    context = contextvars.copy_context()
    return executor.submit(context.run, partial(function, *args, **kwargs))
