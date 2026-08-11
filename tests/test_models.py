"""Result-object ergonomics: filtered views and compact representations."""

from __future__ import annotations

from lean_runtime import Diagnostic, ExecutionResult


def _result(diagnostics: tuple[Diagnostic, ...], *, ok: bool) -> ExecutionResult:
    return ExecutionResult(
        ok=ok,
        exit_code=0 if ok else 1,
        toolchain="leanprover/lean4:v4.32.2",
        command=("lean", "Main.lean"),
        cwd="/fixture",
        stdout="",
        stderr="",
        elapsed_seconds=0.84,
        diagnostics=diagnostics,
    )


def test_errors_warnings_and_first_error_filter_diagnostics() -> None:
    error = Diagnostic("error", "unsolved goals", file="Main.lean", line=1, column=19)
    warning = Diagnostic("warning", "unused variable", file="Main.lean", line=2, column=0)
    result = _result((warning, error), ok=False)
    assert result.errors == (error,)
    assert result.warnings == (warning,)
    assert result.first_error is error


def test_first_error_is_none_when_accepted() -> None:
    result = _result((), ok=True)
    assert result.errors == ()
    assert result.first_error is None


def test_diagnostic_repr_is_compact() -> None:
    diagnostic = Diagnostic("error", "unsolved goals\ncase h", file="Main.lean", line=1, column=19)
    assert repr(diagnostic) == "Diagnostic(error, Main.lean:1:19, 'unsolved goals')"
    assert diagnostic.location == "Main.lean:1:19"
    assert Diagnostic("error", "boom").location is None


def test_execution_result_repr_is_restrained() -> None:
    error = Diagnostic("error", "unsolved goals", file="Main.lean", line=1, column=19)
    rejected = repr(_result((error,), ok=False))
    assert (
        rejected
        == "ExecutionResult(ok=False, errors=1, elapsed=0.84s, first_error='Main.lean:1:19')"
    )
    accepted = repr(_result((), ok=True))
    assert accepted == "ExecutionResult(ok=True, errors=0, elapsed=0.84s)"


def test_to_dict_is_unchanged_by_convenience_properties() -> None:
    error = Diagnostic("error", "unsolved goals", file="Main.lean", line=1, column=19)
    payload = _result((error,), ok=False).to_dict()
    assert "errors" not in payload
    assert "warnings" not in payload
    assert "first_error" not in payload
    assert payload["diagnostics"][0]["message"] == "unsolved goals"
