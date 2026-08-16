"""Explicit versioned serializers for public CLI machine output."""

from __future__ import annotations

from typing import Any

from .comparison import EnvironmentComparison
from .matrix import MatrixResult
from .models import ExecutionResult
from .profiling import ProfileReport
from .verification import VerificationReport


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def envelope(
    schema: str,
    *,
    ok: bool,
    data: dict[str, Any],
    warnings: list[dict[str, Any]] | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema": schema,
        "ok": ok,
        "data": _json_value(data),
        "warnings": _json_value(warnings or []),
        "errors": _json_value(errors or []),
    }


def error(code: str, message: str, *, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"code": code, "message": message, "details": details or {}}


def serialize_execution_v1(result: ExecutionResult) -> dict[str, Any]:
    return envelope("lean-runtime.execution/v1", ok=result.ok, data=result.to_dict())


def serialize_verify_v1(report: VerificationReport) -> dict[str, Any]:
    warnings = [
        error(item.code, str((item.details or {}).get("message", item.code)), details=item.details)
        for item in report.warnings
    ]
    errors = [
        error(item.code, str((item.details or {}).get("message", item.code)), details=item.details)
        for item in report.failures
    ]
    return envelope(
        "lean-runtime.verify/v1",
        ok=report.ok,
        data=report.to_dict(),
        warnings=warnings,
        errors=errors,
    )


def serialize_comparison_v1(result: EnvironmentComparison) -> dict[str, Any]:
    return envelope("lean-runtime.comparison/v1", ok=True, data=result.to_dict())


def serialize_profile_v1(result: ProfileReport) -> dict[str, Any]:
    return envelope("lean-runtime.profile/v1", ok=result.ok, data=result.to_dict())


def serialize_matrix_v1(result: MatrixResult) -> dict[str, Any]:
    return envelope("lean-runtime.matrix/v1", ok=result.ok, data=result.to_dict())


def serialize_check_batch_v1(
    entries: list[tuple[str, ExecutionResult]], duration_seconds: float
) -> dict[str, Any]:
    accepted = sum(1 for _, result in entries if result.ok)
    ok = bool(entries) and accepted == len(entries)
    return envelope(
        "lean-runtime.check-batch/v1",
        ok=ok,
        data={
            "ok": ok,
            "duration_ms": round(duration_seconds * 1000),
            "total": len(entries),
            "accepted": accepted,
            "entries": [{"path": path, "result": result.to_dict()} for path, result in entries],
        },
    )
