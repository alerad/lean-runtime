from __future__ import annotations

from dataclasses import dataclass

import pytest

from lean_runtime import EnvironmentError, ExecutionResult
from lean_runtime.lake import ROOT_MODULE
from lean_runtime.lockfiles import EnvironmentLock
from lean_runtime.verification import _probe


@dataclass
class ProbeEnvironment:
    result: ExecutionResult
    lock: EnvironmentLock
    observed_source: str | None = None
    observed_filename: str | None = None
    observed_allow_sparse_acquisition: bool | None = None
    sparse: bool = False
    _record: dict[str, object] | None = None

    def check(
        self,
        source: str,
        *,
        filename: str,
        policy: object,
        _allow_sparse_acquisition: bool = True,
    ) -> ExecutionResult:
        self.observed_source = source
        self.observed_filename = filename
        self.observed_allow_sparse_acquisition = _allow_sparse_acquisition
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
    environment = ProbeEnvironment(execution(True), lock(packages=True))

    _probe(environment)  # type: ignore[arg-type]

    assert environment.observed_source == f"import {ROOT_MODULE}\n"
    assert environment.observed_filename == "LeanRuntimeVerification.lean"
    assert environment.observed_filename != f"{ROOT_MODULE}.lean"


def test_verification_probe_surfaces_compiler_failure() -> None:
    environment = ProbeEnvironment(execution(False), lock(packages=True))

    with pytest.raises(EnvironmentError, match="verification rejected"):
        _probe(environment)  # type: ignore[arg-type]


def lock(*, packages: bool) -> EnvironmentLock:
    value = {
        "schema": "lean-runtime-environment-lock/1",
        "spec_digest": "spec_" + "0" * 64,
        "toolchain": "leanprover/lean4:v4.33.0",
        "root_lakefile": 'name = "probe"\n',
        "root_module": "",
        "manifest": {},
        "packages": [],
    }
    if packages:
        value["packages"] = [
            {
                "name": "sample",
                "url": "https://example.invalid/sample",
                "revision": "0" * 40,
                "tree_hash": "0" * 40,
                "source_id": "source_" + "0" * 64,
                "requested_revision": "main",
                "source": "git",
                "inherited": False,
                "artifact_command": [],
                "subdir": None,
                "root_module": "Sample",
            }
        ]
    return EnvironmentLock.from_dict(value)


def test_core_verification_probe_does_not_import_synthetic_root() -> None:
    environment = ProbeEnvironment(execution(True), lock(packages=False))

    _probe(environment)  # type: ignore[arg-type]

    assert environment.observed_source == "example : True := by trivial\n"


def test_offline_sparse_probe_uses_retained_module_without_acquisition() -> None:
    environment = ProbeEnvironment(
        execution(True),
        lock(packages=True),
        sparse=True,
        _record={"origin": {"modules": ["Sample.Retained", "Sample.Dependency"]}},
    )

    _probe(environment, offline=True)  # type: ignore[arg-type]

    assert environment.observed_source == "import Sample.Retained\n"
    assert environment.observed_allow_sparse_acquisition is False
