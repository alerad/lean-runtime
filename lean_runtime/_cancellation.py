"""Internal bridge between coroutine cancellation and thread-side cancel events."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


async def settle(task: asyncio.Future[Any]) -> None:
    """Wait for ``task`` to finish without letting its outcome replace ours.

    Used after a cancellation event has been signalled to the worker: the
    worker may finish normally, raise a domain error while unwinding, or
    report that it was cancelled. None of those is the caller's result; the
    caller is re-raising its own ``CancelledError``. The worker's exception is
    retrieved so the event loop does not log it as never consumed.
    """
    await asyncio.wait({task})
    if not task.cancelled():
        task.exception()


async def run_cancellable(function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run ``function(*args, cancel=event, **kwargs)`` in a thread.

    Cancelling the coroutine sets the event, waits for the worker to unwind,
    and then re-raises the cancellation. The worker is never abandoned mid-way
    through a store operation, and its cleanup exceptions never mask the
    cancellation.
    """
    cancel = threading.Event()
    task = asyncio.ensure_future(asyncio.to_thread(function, *args, cancel=cancel, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancel.set()
        await settle(task)
        raise
