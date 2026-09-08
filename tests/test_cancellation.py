from __future__ import annotations

import asyncio
import threading

import pytest

from lean_runtime._cancellation import run_cancellable


def test_cancellation_reaches_the_worker_and_survives_cleanup_errors() -> None:
    observed: dict[str, object] = {}

    def worker(*, cancel: threading.Event) -> str:
        observed["cancelled"] = cancel.wait(5)
        raise RuntimeError("cleanup failed")

    async def scenario() -> None:
        task = asyncio.ensure_future(run_cancellable(worker))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert observed["cancelled"] is True


def test_normal_completion_returns_the_result() -> None:
    def worker(value: int, *, cancel: threading.Event) -> int:
        return value * 2

    assert asyncio.run(run_cancellable(worker, 21)) == 42
