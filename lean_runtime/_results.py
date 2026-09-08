"""Internal rules for turning process outcomes into execution results.

One place answers the questions every operation used to answer on its own:

* A :class:`BackendResult` describes one invocation; :func:`from_backend`
  turns it into an :class:`ExecutionResult` for that invocation alone.
* An operation made of several invocations (support modules before an
  entrypoint, a dependency build before a retried check) reports the whole
  operation: ``elapsed_seconds`` covers everything, ``timings`` are exclusive
  phases that sum to at most that total, and provenance is the final
  invocation's.
* A preparatory failure is the operation's result, with what came before it
  preserved in the transcript.
* Only a compiler that returned a status has a verdict; timeouts and
  cancellations have none, and signal death or a fatal fault is a crash.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from .backends import BackendResult
from .diagnostics import error_diagnostic, map_diagnostic_paths, parse_diagnostics
from .models import ExecutionProvenance, ExecutionResult, PhaseTiming, TimingPhase


def from_backend(
    raw: BackendResult,
    *,
    command: Sequence[str],
    cwd: Path,
    toolchain: str,
    provenance: ExecutionProvenance | None,
    timings: tuple[PhaseTiming, ...] = (),
    path_map: Mapping[str, str] | None = None,
    subject: str = "Lean execution",
) -> ExecutionResult:
    """The result of exactly one invocation.

    ``timings`` are the phases that preceded the invocation; the invocation's
    own ``execution`` phase is appended.
    """
    combined = "\n".join(part for part in (raw.stdout, raw.stderr) if part)
    diagnostics = map_diagnostic_paths(parse_diagnostics(combined), path_map)
    if raw.timed_out:
        diagnostics += (error_diagnostic(f"{subject} exceeded its time limit"),)
    elif raw.cancelled:
        diagnostics += (error_diagnostic(f"{subject} was cancelled"),)
    elif raw.signalled:
        diagnostics += (error_diagnostic(f"{subject} was killed by signal {-raw.exit_code}"),)
    return ExecutionResult(
        ok=raw.exit_code == 0,
        exit_code=raw.exit_code,
        toolchain=toolchain,
        command=tuple(command),
        cwd=str(cwd),
        stdout=raw.stdout,
        stderr=raw.stderr,
        elapsed_seconds=raw.elapsed_seconds,
        timed_out=raw.timed_out,
        cancelled=raw.cancelled,
        output_truncated=raw.output_truncated,
        diagnostics=diagnostics,
        provenance=provenance,
        timings=(*timings, PhaseTiming("execution", round(raw.elapsed_seconds * 1000))),
    )


def combine_invocations(
    preliminary: Sequence[BackendResult], final: BackendResult
) -> BackendResult:
    """Fold earlier invocations of one operation into its final one.

    Used when several compiler runs make up a single check (support modules,
    then the entrypoint). The transcript concatenates in order, time adds up,
    and any interruption or truncation anywhere marks the whole operation.
    ``final`` may itself be the invocation that failed part-way through.
    """
    if not preliminary:
        return final
    return BackendResult(
        exit_code=final.exit_code,
        stdout="".join([item.stdout for item in preliminary] + [final.stdout]),
        stderr="".join([item.stderr for item in preliminary] + [final.stderr]),
        elapsed_seconds=sum(item.elapsed_seconds for item in preliminary) + final.elapsed_seconds,
        timed_out=final.timed_out or any(item.timed_out for item in preliminary),
        cancelled=final.cancelled or any(item.cancelled for item in preliminary),
        output_truncated=final.output_truncated
        or any(item.output_truncated for item in preliminary),
        enforced_policy_fields=final.enforced_policy_fields,
    )


def after_preparation(
    result: ExecutionResult,
    *,
    preparation: Sequence[tuple[TimingPhase, ExecutionResult]],
) -> ExecutionResult:
    """Report ``result`` as the outcome of an operation that first ran ``preparation``.

    Each preparatory result becomes one exclusive timing phase measured by
    that result's own elapsed time, placed before ``result``'s phases, and the
    operation's ``elapsed_seconds`` is the sum. ``result``'s verdict,
    transcript and provenance are untouched: the final invocation is the one
    that decides.
    """
    if not preparation:
        return result
    phases = tuple(
        PhaseTiming(phase, round(item.elapsed_seconds * 1000)) for phase, item in preparation
    )
    return replace(
        result,
        elapsed_seconds=sum(item.elapsed_seconds for _phase, item in preparation)
        + result.elapsed_seconds,
        timings=(*phases, *result.timings),
    )
