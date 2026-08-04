from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from lean_runtime.matrix import MatrixEntry, MatrixResult
from lean_runtime.models import ExecutionResult
from lean_runtime.profiling import ProfileReport
from lean_runtime.wire import serialize_execution_v1, serialize_matrix_v1, serialize_profile_v1

SCHEMAS = Path(__file__).parents[1] / "schemas"


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
    )
    schema = json.loads((SCHEMAS / "execution-v1.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(serialize_execution_v1(result))

    profile_schema = json.loads((SCHEMAS / "profile-v1.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(profile_schema).validate(
        serialize_profile_v1(ProfileReport("Main.lean", 0, (result,), 0.01))
    )
    matrix_schema = json.loads((SCHEMAS / "matrix-v1.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(matrix_schema).validate(
        serialize_matrix_v1(MatrixResult((MatrixEntry("core", result),), 0.01))
    )
