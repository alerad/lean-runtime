from __future__ import annotations

from dataclasses import dataclass

import pytest

from lean_runtime import EnvironmentError, ExecutionResult
from lean_runtime.lake import ROOT_MODULE
from lean_runtime.verification import _probe


@dataclass
class ProbeEnvironment:
    result: ExecutionResult
    observed_source: str | None = None
    observed_filename: str | None = None

    def check(self, source: str, *, filename: str, policy: object) -> ExecutionResult:
        self.observed_source = source
        self.observed_filename = filename
        return self.result


def execution(ok: bool) -> ExecutionResult:
    return ExecutionResult(
        ok=ok,
        exit_code=0 if ok else 1,
        stdout="" if ok else "verification rejected",
        stderr="",
        elapsed_seconds=0.01,
        command=("lean",),
        cwd="/tmp",
        toolchain="leanprover/lean4:v4.33.0",
    )


def test_verification_probe_uses_environment_check_not_lake() -> None:
    environment = ProbeEnvironment(execution(True))

    _probe(environment)  # type: ignore[arg-type]

    assert environment.observed_source == f"import {ROOT_MODULE}\n"
    assert environment.observed_filename == "LeanRuntimeVerification.lean"
    assert environment.observed_filename != f"{ROOT_MODULE}.lean"


def test_verification_probe_surfaces_compiler_failure() -> None:
    environment = ProbeEnvironment(execution(False))

    with pytest.raises(EnvironmentError, match="verification rejected"):
        _probe(environment)  # type: ignore[arg-type]
