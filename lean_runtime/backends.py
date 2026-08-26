"""Execution backends and the trusted local implementation."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Protocol, TextIO, cast

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


class _OutputBudget:
    def __init__(self, limit: int) -> None:
        self.remaining = limit
        self.lock = threading.Lock()
        self.truncated = False

    def take(self, chunk: bytes) -> bytes:
        with self.lock:
            size = min(len(chunk), self.remaining)
            self.remaining -= size
            if size < len(chunk):
                self.truncated = True
            return chunk[:size]


_LINE_BREAK = re.compile(rb"\r\n|\r|\n")


def _drain(
    stream: BinaryIO,
    budget: _OutputBudget,
    chunks: list[bytes],
    on_output: Callable[[str], None] | None = None,
) -> None:
    pending = b""
    while True:
        chunk = stream.read(65_536)
        if not chunk:
            break
        kept = budget.take(chunk)
        if kept:
            chunks.append(kept)
        if on_output is None:
            continue
        # Progress bars redraw with bare carriage returns, so treat those as lines too.
        parts = _LINE_BREAK.split(pending + chunk)
        pending = parts.pop()
        for part in parts:
            _observe(on_output, part)
    if on_output is not None and pending:
        _observe(on_output, pending)


def _observe(on_output: Callable[[str], None], line: bytes) -> None:
    try:
        on_output(line.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - an observer must never break execution
        return


class _TranscriptReader:
    """Mirror caller-consumed text into the bounded execution transcript."""

    def __init__(self, stream: TextIO, budget: _OutputBudget, chunks: list[bytes]) -> None:
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
        self._budget = _OutputBudget(policy.max_output_bytes)
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
        LocalBackend._stop(self._process)
        try:
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            LocalBackend._kill(self._process)

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
            LocalBackend._stop(self._process)
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                LocalBackend._kill(self._process)
                self._process.wait()
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
    ) -> tuple[list[str], Callable[[], object] | None, int]:
        if policy.network == "disabled":
            raise PolicyError("the local backend cannot enforce network isolation")
        enforced = ["timeout_seconds", "max_output_bytes"]
        preexec = None
        if os.name != "nt" and (policy.memory_mb or policy.cpu_seconds):
            memory_mb = policy.memory_mb
            cpu_seconds = policy.cpu_seconds

            def apply_limits() -> None:
                import resource

                if memory_mb is not None:
                    limit = memory_mb * 1024 * 1024
                    getattr(resource, "setrlimit")(  # noqa: B009
                        getattr(resource, "RLIMIT_AS"),  # noqa: B009
                        (limit, limit),
                    )
                if cpu_seconds is not None:
                    getattr(resource, "setrlimit")(  # noqa: B009
                        getattr(resource, "RLIMIT_CPU"),  # noqa: B009
                        (cpu_seconds, cpu_seconds),
                    )

            preexec = apply_limits
            if memory_mb is not None:
                enforced.append("memory_mb")
            if cpu_seconds is not None:
                enforced.append("cpu_seconds")
        elif os.name == "nt" and (policy.memory_mb or policy.cpu_seconds):
            raise PolicyError("the local Windows backend cannot enforce memory or CPU limits")
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        return enforced, preexec, creationflags

    def spawn_interactive(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        policy: ExecutionPolicy,
    ) -> InteractiveProcess:
        """Spawn a trusted local process with live text pipes."""
        enforced, preexec, creationflags = self._process_options(policy)
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
            start_new_session=os.name != "nt",
            creationflags=creationflags,
            preexec_fn=preexec,
        )
        return _LocalInteractiveProcess(
            process,
            policy=policy,
            enforced_policy_fields=tuple(enforced),
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
    ) -> BackendResult:
        enforced, preexec, creationflags = self._process_options(policy)

        started = time.monotonic()
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=dict(environment),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
            creationflags=creationflags,
            preexec_fn=preexec,
        )
        assert process.stdout is not None and process.stderr is not None
        budget = _OutputBudget(policy.max_output_bytes)
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        readers = [
            threading.Thread(
                target=_drain, args=(process.stdout, budget, stdout_chunks, on_output)
            ),
            threading.Thread(
                target=_drain, args=(process.stderr, budget, stderr_chunks, on_output)
            ),
        ]
        for reader in readers:
            reader.start()
        timed_out = False
        cancelled = False
        try:
            while process.poll() is None:
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    self._stop(process)
                    break
                if time.monotonic() - started >= policy.timeout_seconds:
                    timed_out = True
                    self._stop(process)
                    break
                time.sleep(0.02)
        except BaseException:
            self._stop(process)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._kill(process)
                process.wait()
            for reader in readers:
                reader.join()
            raise
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._kill(process)
            process.wait()
        for reader in readers:
            reader.join()
        exit_code = 130 if cancelled else 124 if timed_out else int(process.returncode)
        return BackendResult(
            exit_code=exit_code,
            stdout=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
            stderr=b"".join(stderr_chunks).decode("utf-8", errors="replace"),
            elapsed_seconds=time.monotonic() - started,
            timed_out=timed_out,
            cancelled=cancelled,
            output_truncated=budget.truncated,
            enforced_policy_fields=tuple(enforced),
        )

    @staticmethod
    def _stop(process: subprocess.Popen[Any]) -> None:
        try:
            if os.name == "nt":
                process.terminate()
            else:
                getattr(os, "killpg")(process.pid, signal.SIGTERM)  # noqa: B009
        except ProcessLookupError:
            pass

    @staticmethod
    def _kill(process: subprocess.Popen[Any]) -> None:
        try:
            if os.name == "nt":
                process.kill()
            else:
                getattr(os, "killpg")(  # noqa: B009
                    process.pid,
                    getattr(signal, "SIGKILL"),  # noqa: B009
                )
        except ProcessLookupError:
            pass
