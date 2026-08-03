from lean_runtime.diagnostics import parse_diagnostics


def test_parse_multiline_diagnostics() -> None:
    diagnostics = parse_diagnostics(
        "Main.lean:3:7: error: application type mismatch\n"
        "  supplied argument has type Nat\n"
        "Main.lean:8:1: warning: declaration uses 'sorry'\n"
    )
    assert len(diagnostics) == 2
    assert diagnostics[0].severity == "error"
    assert diagnostics[0].line == 3
    assert "supplied argument" in diagnostics[0].message
    assert diagnostics[1].severity == "warning"


def test_unrecognized_output_is_not_invented_as_a_diagnostic() -> None:
    assert parse_diagnostics("Build completed successfully") == ()
