from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from lean_runtime.matrix import MatrixEntry, MatrixResult
from lean_runtime.models import ExecutionResult, PhaseTiming
from lean_runtime.profiling import ProfileReport
from lean_runtime.run_cli import main as lean_run_main
from lean_runtime.wire import (
    envelope,
    error,
    serialize_check_batch_v1,
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
        "attestation-v1.schema",
        "check-batch-v1.schema",
        "comparison-v1.schema",
        "execution-v1.schema",
        "cleanup-v1.schema",
        "inspect-v1.schema",
        "matrix-v1.schema",
        "plan-v1.schema",
        "profile-v1.schema",
        "publication-v1.schema",
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
        timings=(PhaseTiming("discovery", 5), PhaseTiming("execution", 10)),
    )
    _validator("execution-v1.schema.json").validate(serialize_execution_v1(result))

    _validator("profile-v1.schema.json").validate(
        serialize_profile_v1(ProfileReport("Main.lean", 0, (result,), 0.01))
    )
    _validator("matrix-v1.schema.json").validate(
        serialize_matrix_v1(MatrixResult((MatrixEntry("core", result),), 0.01))
    )
    _validator("check-batch-v1.schema.json").validate(
        serialize_check_batch_v1([("Main.lean", result)], 0.01)
    )


def test_plan_success_fixture_matches_v1_schema() -> None:
    plan = envelope(
        "lean-runtime.plan/v1",
        ok=True,
        data={
            "lock_id": "lock_" + "a" * 64,
            "toolchain": "leanprover/lean4:v4.32.0",
            "environment_id": "env_" + "b" * 64,
            "environment_ready": False,
            "environment_download_bytes": 600 * 2**20,
            "toolchain_installed": True,
            "toolchain_download_bytes": 0,
            "toolchain_libraries": [],
            "max_download_bytes": 500 * 2**20,
            "download_bytes": 600 * 2**20,
            "download_bytes_complete": True,
            "libraries": [
                {
                    "library": "oci://ghcr.io/owner/cache",
                    "available": True,
                    "total_bytes": 700 * 2**20,
                    "cached_bytes": 100 * 2**20,
                    "download_bytes": 600 * 2**20,
                }
            ],
            "candidate": "mathlib-4.32",
        },
    )
    _validator("plan-v1.schema.json").validate(plan)


def test_every_v1_schema_accepts_its_closed_error_envelope() -> None:
    schemas = {
        "check-batch": "lean-runtime.check-batch/v1",
        "plan": "lean-runtime.plan/v1",
        "comparison": "lean-runtime.comparison/v1",
        "execution": "lean-runtime.execution/v1",
        "cleanup": "lean-runtime.cleanup/v1",
        "inspect": "lean-runtime.inspect/v1",
        "matrix": "lean-runtime.matrix/v1",
        "profile": "lean-runtime.profile/v1",
        "publication": "lean-runtime.publication/v1",
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
    routing = envelope(
        "lean-runtime.inspect/v1",
        ok=True,
        data={
            "decision": "automatic_discovery",
            "context": "catalog candidates",
            "subject": ["mathlib-v4.33.0", "mathlib-v4.32.2"],
            "plan": {"schema": "lean-runtime.discovery.plan/v1"},
        },
    )
    _validator("inspect-v1.schema.json").validate(routing)
    gc = envelope(
        "lean-runtime.cleanup/v1",
        ok=True,
        data={
            "environments": {
                "candidates": [],
                "removed": [],
                "retained": [],
                "dry_run": True,
            },
            "downloaded_files": None,
        },
    )
    _validator("cleanup-v1.schema.json").validate(gc)


def test_run_explain_json_matches_inspect_schema(tmp_path: Path, capsys) -> None:
    source = tmp_path / "Main.lean"
    source.write_text("import Mathlib\n", encoding="utf-8")
    assert lean_run_main([str(source), "--explain", "--json"]) == 0
    _validator("inspect-v1.schema.json").validate(json.loads(capsys.readouterr().out))


def test_explicit_run_explain_json_matches_inspect_schema(tmp_path: Path, capsys) -> None:
    source = tmp_path / "Main.lean"
    source.write_text("example : True := trivial\n", encoding="utf-8")
    assert lean_run_main([str(source), "--toolchain", "v4.32.2", "--explain", "--json"]) == 0
    _validator("inspect-v1.schema.json").validate(json.loads(capsys.readouterr().out))


def test_publication_access_and_failure_fixtures_match_v1_schema() -> None:
    validator = _validator("publication-v1.schema.json")
    validator.validate(
        envelope(
            "lean-runtime.publication/v1",
            ok=True,
            data={
                "registry": "oci://ghcr.io/owner/cache",
                "username": "owner",
                "credential_source": "GitHub CLI",
                "push_verified": True,
            },
        )
    )
    validator.validate(
        envelope(
            "lean-runtime.publication/v1",
            ok=True,
            data={
                "library": "oci://ghcr.io/owner/cache",
                "exact_environment_id": "lock_abc",
                "environment_id": "env_abc",
                "computer_copy_id": "sha256:" + "a" * 64,
                "publication_id": "sha256:" + "b" * 64,
                "uploaded_files": 1,
                "total_blob_bytes": 10,
                "uploaded_bytes": 4,
                "reused_bytes": 6,
                "reuse_percent": 60.0,
                "computer_record": {},
                "consumer_command": "lean-runtime env acquire environment.lock.json",
            },
        )
    )
    failure = {
        "phase": "access_preflight",
        "registry": "oci://ghcr.io/owner/cache",
        "status_code": 403,
        "retryable": False,
        "published": False,
        "partial": False,
        "credential_source": "GitHub CLI",
        "username": "owner",
        "hint": "refresh scopes",
        "message": "registry denied push access",
    }
    validator.validate(
        envelope(
            "lean-runtime.publication/v1",
            ok=False,
            data=failure,
            errors=[error("publication_failed", failure["message"], details=failure)],
        )
    )


def test_timing_phase_vocabulary_and_duration_are_bounded() -> None:
    with pytest.raises(ValueError, match="unsupported timing phase"):
        PhaseTiming("other", 1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="nonnegative"):
        PhaseTiming("execution", -1)


def test_attestation_predicate_binds_a_build_inventory(tmp_path: Path) -> None:
    from lean_runtime.verification import (
        VerificationCheck,
        VerificationReport,
        attestation_predicate,
    )

    workspace = tmp_path / "workspace"
    build = workspace / "packages" / "sample" / ".lake" / "build"
    build.mkdir(parents=True)
    (build / "Sample.olean").write_bytes(b"olean")

    report = VerificationReport(
        subject="research-stack",
        subject_kind="environment",
        checks=(VerificationCheck("lean_probe_passed", True),),
        failures=(),
        warnings=(),
        lock_id="lock_" + "a" * 64,
        environment_id="env_" + "b" * 64,
    )
    predicate = attestation_predicate(report, workspace)

    assert predicate["schema"] == "lean-runtime.attestation/v1"
    assert predicate["build_inventory"]["entries"] > 0
    assert predicate["build_inventory"]["bytes"] == len(b"olean")
    _validator("attestation-v1.schema.json").validate(predicate)

    # The inventory tracks the built outputs rather than a fixed constant.
    (build / "Extra.olean").write_bytes(b"more")
    changed = attestation_predicate(report, workspace)
    assert changed["build_inventory"]["digest"] != predicate["build_inventory"]["digest"]
