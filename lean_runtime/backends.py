"""Execution backends and the trusted local implementation."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TextIO, cast

from ._process import (
    OutputBudget,
    ProcessOutcome,
    ResourceLimits,
    kill_tree,
    run_process,
    shutdown,
    spawn_options,
    stop_tree,
)
from .errors import PolicyError
from .policies import ExecutionPolicy


@dataclass(frozen=True, slots=True)
class BackendResult:
    exit_code: int
    stdout: str
    stderr: str
    elapsed_seconds: float
    timed_out: bool
    cancelled: bool
    output_truncated: bool
    enforced_policy_fields: tuple[str, ...]

    @classmethod
    def from_outcome(
        cls, outcome: ProcessOutcome, enforced_policy_fields: tuple[str, ...]
    ) -> BackendResult:
        return cls(
            exit_code=outcome.exit_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            elapsed_seconds=outcome.elapsed_seconds,
            timed_out=outcome.timed_out,
            cancelled=outcome.cancelled,
            output_truncated=outcome.output_truncated,
            enforced_policy_fields=enforced_policy_fields,
        )

    @property
    def signalled(self) -> bool:
        """The process died from a signal rather than returning a status."""
        return self.exit_code < 0 and not self.timed_out and not self.cancelled


class Backend(Protocol):
    name: str

    def execute(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        policy: ExecutionPolicy,
        cancel: threading.Event | None = None,
    ) -> BackendResult: ...


class InteractiveTextReader(Protocol):
    @property
    def closed(self) -> bool: ...

    def read(self, size: int = -1) -> str: ...

    def readline(self, size: int = -1) -> str: ...

    def fileno(self) -> int: ...

    def close(self) -> None: ...


class InteractiveProcess(Protocol):
    """Live standard-I/O streams plus managed process finalization."""

    stdin: TextIO
    stdout: InteractiveTextReader
    stderr: InteractiveTextReader

    def poll(self) -> int | None: ...

    def finish(self) -> BackendResult: ...


class _TranscriptReader:
    """Mirror caller-consumed text into the bounded execution transcript."""

    def __init__(self, stream: TextIO, budget: OutputBudget, chunks: list[bytes]) -> None:
        self._stream = stream
        self._budget = budget
        self._chunks = chunks

    @property
    def closed(self) -> bool:
        return self._stream.closed

    def fileno(self) -> int:
        return self._stream.fileno()

    def _record(self, value: str) -> str:
        if value:
            kept = self._budget.take(value.encode("utf-8"))
            if kept:
                self._chunks.append(kept)
        return value

    def read(self, size: int = -1) -> str:
        return self._record(self._stream.read(size))

    def readline(self, size: int = -1) -> str:
        return self._record(self._stream.readline(size))

    def close(self) -> None:
        self._stream.close()


class _LocalInteractiveProcess:
    def __init__(
        self,
        process: subprocess.Popen[str],
        *,
        policy: ExecutionPolicy,
        enforced_policy_fields: tuple[str, ...],
    ) -> None:
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        self._process = process
        self._policy = policy
        self._enforced_policy_fields = enforced_policy_fields
        self._started = time.monotonic()
        self._timed_out = threading.Event()
        self._finished = threading.Event()
        self._budget = OutputBudget(policy.max_output_bytes)
        self._stdout_chunks: list[bytes] = []
        self._stderr_chunks: list[bytes] = []
        self.stdin = cast(TextIO, process.stdin)
        self.stdout: InteractiveTextReader = _TranscriptReader(
            cast(TextIO, process.stdout), self._budget, self._stdout_chunks
        )
        self.stderr: InteractiveTextReader = _TranscriptReader(
            cast(TextIO, process.stderr), self._budget, self._stderr_chunks
        )
        self._monitor = threading.Thread(
            target=self._enforce_timeout,
            name=f"lean-runtime-process-{process.pid}",
            daemon=True,
        )
        self._monitor.start()

    def _enforce_timeout(self) -> None:
        remaining = self._policy.timeout_seconds - (time.monotonic() - self._started)
        if remaining > 0 and self._finished.wait(remaining):
            return
        if self._process.poll() is not None:
            return
        self._timed_out.set()
        shutdown(self._process)

    def poll(self) -> int | None:
        return self._process.poll()

    @staticmethod
    def _remaining(reader: InteractiveTextReader) -> None:
        with suppress(OSError, ValueError):
            reader.read()

    def finish(self) -> BackendResult:
        if not self.stdin.closed:
            self.stdin.close()
        cancelled = False
        try:
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            cancelled = True
            shutdown(self._process)
        self._finished.set()
        self._monitor.join(timeout=3)
        self._remaining(self.stdout)
        self._remaining(self.stderr)
        self.stdout.close()
        self.stderr.close()
        timed_out = self._timed_out.is_set()
        return BackendResult(
            exit_code=124 if timed_out else 130 if cancelled else int(self._process.returncode),
            stdout=b"".join(self._stdout_chunks).decode("utf-8", errors="replace"),
            stderr=b"".join(self._stderr_chunks).decode("utf-8", errors="replace"),
            elapsed_seconds=time.monotonic() - self._started,
            timed_out=timed_out,
            cancelled=cancelled and not timed_out,
            output_truncated=self._budget.truncated,
            enforced_policy_fields=self._enforced_policy_fields,
        )


class LocalBackend:
    """Trusted local subprocess execution with bounded captured output."""

    name = "local"

    @staticmethod
    def _process_options(
        policy: ExecutionPolicy,
    ) -> tuple[tuple[str, ...], ResourceLimits | None]:
        if policy.network == "disabled":
            raise PolicyError("the local backend cannot enforce network isolation")
        enforced = ["timeout_seconds", "max_output_bytes"]
        limits: ResourceLimits | None = None
        if policy.memory_mb or policy.cpu_seconds:
            if os.name == "nt":
                raise PolicyError("the local Windows backend cannot enforce memory or CPU limits")
            limits = ResourceLimits(memory_mb=policy.memory_mb, cpu_seconds=policy.cpu_seconds)
            if policy.memory_mb is not None:
                enforced.append("memory_mb")
            if policy.cpu_seconds is not None:
                enforced.append("cpu_seconds")
        return tuple(enforced), limits

    def spawn_interactive(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        policy: ExecutionPolicy,
    ) -> InteractiveProcess:
        """Spawn a trusted local process with live text pipes."""
        enforced, limits = self._process_options(policy)
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **spawn_options(limits),
        )
        return _LocalInteractiveProcess(
            process,
            policy=policy,
            enforced_policy_fields=enforced,
        )

    def execute(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        policy: ExecutionPolicy,
        cancel: threading.Event | None = None,
        on_output: Callable[[str], None] | None = None,
        on_bytes: Callable[[str, bytes], None] | None = None,
    ) -> BackendResult:
        enforced, limits = self._process_options(policy)
        outcome = run_process(
            command,
            cwd=cwd,
            environment=environment,
            timeout=policy.timeout_seconds,
            cancel=cancel,
            max_output_bytes=policy.max_output_bytes,
            on_output=on_output,
            on_bytes=on_bytes,
            limits=limits,
        )
        return BackendResult.from_outcome(outcome, enforced)

    @staticmethod
    def _stop(process: subprocess.Popen[Any]) -> None:
        stop_tree(process)

    @staticmethod
    def _kill(process: subprocess.Popen[Any]) -> None:
        kill_tree(process)
