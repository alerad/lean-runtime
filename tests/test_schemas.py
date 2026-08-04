from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from lean_runtime.matrix import MatrixEntry, MatrixResult
from lean_runtime.models import ExecutionResult, PhaseTiming
from lean_runtime.profiling import ProfileReport
from lean_runtime.wire import (
    envelope,
    error,
    serialize_execution_v1,
    serialize_matrix_v1,
    serialize_profile_v1,
)

SCHEMAS = Path(__file__).parents[1] / "schemas"


def _validator(name: str) -> Draft202012Validator:
    documents = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(SCHEMAS.glob("*-v1.schema.json"))
    ]
    registry = Registry().with_resources(
        (document["$id"], Resource.from_contents(document)) for document in documents
    )
    selected = next(item for item in documents if item["$id"].endswith(f"/{name}"))
    return Draft202012Validator(selected, registry=registry)


def test_every_v1_schema_compiles_eagerly() -> None:
    paths = sorted(SCHEMAS.glob("*-v1.schema.json"))
    assert {path.stem for path in paths} == {
        "diff-v1.schema",
        "execution-v1.schema",
        "gc-v1.schema",
        "inspect-v1.schema",
        "matrix-v1.schema",
        "profile-v1.schema",
        "verify-v1.schema",
    }
    for path in paths:
        Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))


def test_execution_success_fixture_matches_v1_schema() -> None:
    result = ExecutionResult(
        ok=True,
        exit_code=0,
        toolchain="leanprover/lean4:v4.32.0",
        command=("lean", "Main.lean"),
        cwd="/tmp",
        stdout="",
        stderr="",
        elapsed_seconds=0.01,
        timings=(PhaseTiming("execution", 10),),
    )
    _validator("execution-v1.schema.json").validate(serialize_execution_v1(result))

    _validator("profile-v1.schema.json").validate(
        serialize_profile_v1(ProfileReport("Main.lean", 0, (result,), 0.01))
    )
    _validator("matrix-v1.schema.json").validate(
        serialize_matrix_v1(MatrixResult((MatrixEntry("core", result),), 0.01))
    )


def test_every_v1_schema_accepts_its_closed_error_envelope() -> None:
    schemas = {
        "diff": "lean-runtime.diff/v1",
        "execution": "lean-runtime.execution/v1",
        "gc": "lean-runtime.gc/v1",
        "inspect": "lean-runtime.inspect/v1",
        "matrix": "lean-runtime.matrix/v1",
        "profile": "lean-runtime.profile/v1",
        "verify": "lean-runtime.verify/v1",
    }
    for name, identifier in schemas.items():
        _validator(f"{name}-v1.schema.json").validate(
            envelope(
                identifier,
                ok=False,
                data={},
                errors=[error("operation_failed", "expected failure")],
            )
        )


def test_inspect_and_gc_success_fixtures_are_closed() -> None:
    inspect = envelope(
        "lean-runtime.inspect/v1",
        ok=True,
        data={
            "subject": "environment.lock.json",
            "subject_kind": "lock",
            "lock_id": "lock_abc",
            "environment": None,
            "package_locks": [],
            "decisions": [],
        },
    )
    _validator("inspect-v1.schema.json").validate(inspect)
    gc = envelope(
        "lean-runtime.gc/v1",
        ok=True,
        data={
            "environments": {
                "candidates": [],
                "removed": [],
                "retained": [],
                "dry_run": True,
            },
            "oci_blobs": None,
        },
    )
    _validator("gc-v1.schema.json").validate(gc)


def test_timing_phase_vocabulary_and_duration_are_bounded() -> None:
    with pytest.raises(ValueError, match="unsupported timing phase"):
        PhaseTiming("other", 1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="nonnegative"):
        PhaseTiming("execution", -1)
