"""Execution backends and the trusted local implementation."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

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


def _drain(stream: BinaryIO, budget: _OutputBudget, chunks: list[bytes]) -> None:
    while True:
        chunk = stream.read(65_536)
        if not chunk:
            return
        kept = budget.take(chunk)
        if kept:
            chunks.append(kept)


class LocalBackend:
    """Trusted local subprocess execution with bounded captured output."""

    name = "local"

    def execute(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        policy: ExecutionPolicy,
        cancel: threading.Event | None = None,
    ) -> BackendResult:
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

        started = time.monotonic()
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
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
            threading.Thread(target=_drain, args=(process.stdout, budget, stdout_chunks)),
            threading.Thread(target=_drain, args=(process.stderr, budget, stderr_chunks)),
        ]
        for reader in readers:
            reader.start()
        timed_out = False
        cancelled = False
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
    def _stop(process: subprocess.Popen[bytes]) -> None:
        try:
            if os.name == "nt":
                process.terminate()
            else:
                getattr(os, "killpg")(process.pid, signal.SIGTERM)  # noqa: B009
        except ProcessLookupError:
            pass

    @staticmethod
    def _kill(process: subprocess.Popen[bytes]) -> None:
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
