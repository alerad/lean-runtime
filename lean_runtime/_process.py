"""Internal subprocess seam: bounded output, timeouts, cancellation, tree shutdown.

Every child the runtime starts, whether Lean, Lake, Git, elan or a helper,
goes through :func:`run_process`. Output is captured under one byte budget;
an optional observer sees complete lines under a bounded carry buffer; a
timeout or a cancel event stops the whole process group; readers can never
hang the caller because a grandchild kept a pipe open.
"""

from __future__ import annotations

import importlib
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

# ``resource`` is POSIX-only; resolve it once at import time so the forked
# child in ``preexec_fn`` never imports anything.
_resource: Any = importlib.import_module("resource") if os.name != "nt" else None


def git_command(*arguments: str) -> list[str]:
    """Return a Git command that can materialize Lean's deep source trees.

    ``core.longpaths`` is meaningful on Git for Windows and harmless on other
    platforms. Supplying it per invocation avoids mutable global Git settings.
    """
    return ["git", "-c", "core.longpaths=true", *arguments]


DEFAULT_MAX_OUTPUT_BYTES = 10_000_000
DEFAULT_GIT_TIMEOUT_SECONDS = 600.0
_OBSERVER_CARRY_LIMIT = 65_536
_READER_GRACE_SECONDS = 5.0
_POLL_INTERVAL_SECONDS = 0.02
_LINE_BREAK = re.compile(rb"\r\n|\r|\n")

EXIT_TIMED_OUT = 124
EXIT_CANCELLED = 130


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """Per-process address-space and CPU ceilings (POSIX only)."""

    memory_mb: int | None = None
    cpu_seconds: int | None = None

    def __bool__(self) -> bool:
        return self.memory_mb is not None or self.cpu_seconds is not None


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    """What one finished child process produced."""

    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    elapsed_seconds: float
    timed_out: bool
    cancelled: bool
    output_truncated: bool

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.cancelled

    @property
    def output(self) -> str:
        return self.stdout + self.stderr

    @property
    def signalled(self) -> bool:
        """The child died from a signal rather than exiting on its own."""
        return self.exit_code < 0 and not self.timed_out and not self.cancelled


class ProcessFailure(Exception):
    """A child did not complete successfully; callers convert to domain errors."""

    def __init__(self, outcome: ProcessOutcome, purpose: str | None = None) -> None:
        self.outcome = outcome
        self.purpose = purpose
        what = purpose or " ".join(outcome.command[:3])
        if outcome.timed_out:
            reason = "timed out"
        elif outcome.cancelled:
            reason = "was cancelled"
        else:
            reason = f"exited {outcome.exit_code}"
        super().__init__(f"{what} {reason}")


class OutputBudget:
    """Thread-safe byte budget shared by every stream of one process."""

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


def observe_line(on_output: Callable[[str], None], line: bytes) -> None:
    try:
        on_output(line.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - an observer must never break execution
        return


def drain(
    stream: BinaryIO,
    budget: OutputBudget,
    chunks: list[bytes],
    on_output: Callable[[str], None] | None = None,
    stop: threading.Event | None = None,
) -> None:
    """Copy ``stream`` into ``chunks`` under ``budget``; report lines to the observer.

    The observer's carry buffer is bounded: a child that writes megabytes
    without a line break has the partial line flushed to the observer in
    pieces rather than accumulated without limit.
    """
    pending = b""
    try:
        while True:
            if stop is not None and stop.is_set():
                break
            chunk = stream.read(65_536)
            if stop is not None and stop.is_set():
                break
            if not chunk:
                break
            kept = budget.take(chunk)
            if kept:
                chunks.append(kept)
            if on_output is None:
                continue
            # Progress bars redraw with bare carriage returns; treat those as lines too.
            parts = _LINE_BREAK.split(pending + chunk)
            pending = parts.pop()
            for part in parts:
                observe_line(on_output, part)
            if len(pending) > _OBSERVER_CARRY_LIMIT:
                observe_line(on_output, pending)
                pending = b""
    except (OSError, ValueError):
        pass
    if on_output is not None and pending and (stop is None or not stop.is_set()):
        observe_line(on_output, pending)


def _apply_limits(limits: ResourceLimits) -> Callable[[], None] | None:
    """A ``preexec_fn`` that only calls into an already imported module.

    ``preexec_fn`` runs in the forked child before ``exec`` while the parent
    may hold the import lock on another thread; importing there can deadlock.
    ``resource`` is therefore imported at module load and the closure does
    nothing but two ``setrlimit`` calls.
    """
    if not limits or _resource is None:
        return None
    memory_mb = limits.memory_mb
    cpu_seconds = limits.cpu_seconds
    setrlimit = _resource.setrlimit
    rlimit_as = _resource.RLIMIT_AS
    rlimit_cpu = _resource.RLIMIT_CPU

    def apply() -> None:
        if memory_mb is not None:
            ceiling = memory_mb * 1024 * 1024
            setrlimit(rlimit_as, (ceiling, ceiling))
        if cpu_seconds is not None:
            setrlimit(rlimit_cpu, (cpu_seconds, cpu_seconds))

    return apply


def spawn_options(limits: ResourceLimits | None) -> dict[str, Any]:
    """``Popen`` keyword arguments that place the child in its own tree."""
    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        options["start_new_session"] = True
        preexec = _apply_limits(limits) if limits is not None else None
        if preexec is not None:
            options["preexec_fn"] = preexec
    return options


def stop_tree(process: subprocess.Popen[Any]) -> None:
    """Ask the child's whole process group to terminate."""
    try:
        if sys.platform == "win32":
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        with suppress(OSError):
            process.terminate()


def kill_tree(process: subprocess.Popen[Any]) -> None:
    """Kill the child's whole process group without waiting."""
    try:
        if sys.platform == "win32":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        with suppress(OSError):
            process.kill()


def shutdown(process: subprocess.Popen[Any], *, grace_seconds: float = 2.0) -> None:
    """Terminate, then kill, the process tree until the direct child is reaped."""
    stop_tree(process)
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        kill_tree(process)
        process.wait()
    else:
        # Reaping the parent does not imply that its descendants exited.
        # They may ignore SIGTERM and close their pipes, bypassing reader cleanup.
        if os.name != "nt":
            kill_tree(process)


def _join_readers(
    process: subprocess.Popen[Any], readers: Sequence[threading.Thread], stop: threading.Event
) -> bool:
    """Drain or abandon the pipes within a deadline; return whether output was cut off.

    These pipes must be unbuffered: closing a BufferedReader waits for its
    read lock, which a blocked reader can hold indefinitely when a descendant
    has escaped the process group and still owns a pipe's write end.
    """
    deadline = time.monotonic() + _READER_GRACE_SECONDS
    for reader in readers:
        reader.join(timeout=max(0.0, deadline - time.monotonic()))
    if any(reader.is_alive() for reader in readers):
        kill_tree(process)
        deadline = time.monotonic() + 1.0
        for reader in readers:
            reader.join(timeout=max(0.0, deadline - time.monotonic()))
    truncated = any(reader.is_alive() for reader in readers)
    stop.set()
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            with suppress(OSError):
                stream.close()
    return truncated


def run_process(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
    timeout: float | None = None,
    cancel: threading.Event | None = None,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    on_output: Callable[[str], None] | None = None,
    merge_stderr: bool = False,
    limits: ResourceLimits | None = None,
) -> ProcessOutcome:
    """Run ``command`` to completion under the runtime's execution guarantees.

    Never raises for a non-zero exit, timeout or cancellation; those are
    reported in the outcome. ``OSError`` from a missing executable propagates
    for the caller to convert.
    """
    started = time.monotonic()
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        env=dict(environment) if environment is not None else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
        bufsize=0,
        **spawn_options(limits),
    )
    assert process.stdout is not None
    budget = OutputBudget(max_output_bytes)
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    stop_readers = threading.Event()
    readers = [
        threading.Thread(
            target=drain,
            args=(process.stdout, budget, stdout_chunks, on_output, stop_readers),
            name=f"lean-runtime-stdout-{process.pid}",
            daemon=True,
        )
    ]
    if process.stderr is not None:
        readers.append(
            threading.Thread(
                target=drain,
                args=(process.stderr, budget, stderr_chunks, on_output, stop_readers),
                name=f"lean-runtime-stderr-{process.pid}",
                daemon=True,
            )
        )
    for reader in readers:
        reader.start()
    timed_out = False
    cancelled = False
    try:
        while process.poll() is None:
            if cancel is not None and cancel.is_set():
                cancelled = True
                shutdown(process)
                break
            if timeout is not None and time.monotonic() - started >= timeout:
                timed_out = True
                shutdown(process)
                break
            time.sleep(_POLL_INTERVAL_SECONDS)
    except BaseException:
        shutdown(process)
        _join_readers(process, readers, stop_readers)
        raise
    readers_truncated = _join_readers(process, readers, stop_readers)
    if timed_out:
        exit_code = EXIT_TIMED_OUT
    elif cancelled:
        exit_code = EXIT_CANCELLED
    else:
        exit_code = int(process.returncode)
    return ProcessOutcome(
        command=tuple(command),
        exit_code=exit_code,
        stdout=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
        stderr=b"".join(stderr_chunks).decode("utf-8", errors="replace"),
        elapsed_seconds=time.monotonic() - started,
        timed_out=timed_out,
        cancelled=cancelled,
        output_truncated=budget.truncated or readers_truncated,
    )


def run_git(
    *arguments: str,
    cwd: Path | None = None,
    timeout: float | None = DEFAULT_GIT_TIMEOUT_SECONDS,
    cancel: threading.Event | None = None,
    environment: Mapping[str, str] | None = None,
) -> ProcessOutcome:
    """Run one portable Git command; never raises for Git's own failures."""
    return run_process(
        git_command(*arguments),
        cwd=cwd,
        environment=environment,
        timeout=timeout,
        cancel=cancel,
        max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
    )


def git_output(
    *arguments: str,
    cwd: Path | None = None,
    timeout: float | None = DEFAULT_GIT_TIMEOUT_SECONDS,
    cancel: threading.Event | None = None,
) -> str | None:
    """Stripped stdout of a Git query, or ``None`` when Git did not succeed."""
    outcome = run_git(*arguments, cwd=cwd, timeout=timeout, cancel=cancel)
    return outcome.stdout.strip() if outcome.ok else None
