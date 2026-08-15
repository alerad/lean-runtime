from lean_runtime.diagnostics import map_diagnostic_paths, parse_diagnostics


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


def test_map_diagnostic_paths_rewrites_only_exact_staged_matches() -> None:
    staged = "/store/jobs/execution_ab/instance-cd/Main.lean"
    other = "/store/jobs/execution_ab/instance-cd/.lake/packages/std/Std.lean"
    diagnostics = parse_diagnostics(
        f"{staged}:1:19: error: unsolved goals\n{other}:4:0: warning: declaration uses 'sorry'\n"
    )
    mapped = map_diagnostic_paths(diagnostics, {staged: "Main.lean"})
    assert mapped[0].file == "Main.lean"
    assert mapped[0].line == 1
    assert mapped[1].file == other


def test_map_diagnostic_paths_is_identity_without_a_map() -> None:
    diagnostics = parse_diagnostics("Main.lean:1:1: error: boom\n")
    assert map_diagnostic_paths(diagnostics, None) is diagnostics
    assert map_diagnostic_paths(diagnostics, {}) is diagnostics


def test_parse_diagnostics_accepts_lean_diagnostic_codes() -> None:
    diagnostics = parse_diagnostics(
        "Main.lean:2:7: error(lean.unknownIdentifier): Unknown identifier `missing`\n"
    )

    assert diagnostics[0].severity == "error"
    assert diagnostics[0].message == "Unknown identifier `missing`"
