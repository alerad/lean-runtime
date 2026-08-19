from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lean_runtime.cli import PUBLIC_COMMANDS, _apply_using, main, parser
from lean_runtime.models import ExecutionResult

REMOVED = {
    "run",
    "init",
    "prepare",
    "open",
    "download",
    "environments",
    "inspect",
    "compare",
    "copy",
    "finalize",
}


def test_v4_surface_has_no_legacy_commands(capsys: pytest.CaptureFixture[str]) -> None:
    assert REMOVED.isdisjoint(PUBLIC_COMMANDS)
    with pytest.raises(SystemExit):
        parser().parse_args(["run", "Main.lean"])
    parser().print_help()
    help_text = capsys.readouterr().out
    assert "new NAME" in help_text
    assert "adopt [PATH]" in help_text
    assert "check [PATH…]" in help_text
    assert "lean-run " not in help_text


def test_project_commands_default_to_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    cwd = Path.cwd()
    assert parser().parse_args(["adopt"]).path == cwd
    assert parser().parse_args(["build"]).project == cwd
    assert parser().parse_args(["update"]).path == cwd
    assert parser().parse_args(["project", "scan"]).path == cwd
    assert parser().parse_args(["build"]).artifact_cache is True
    assert parser().parse_args(["build", "--no-cache"]).artifact_cache is False


def test_configuration_supplies_persistent_runtime_and_trust_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        """
[runtime]
home = "/tmp/lean-runtime-configured"
libraries = ["ghcr.io/example/runtime"]
availability = "required"

[trust]
publisher_verification = "required"
trusted_publisher = "https://github.com/example/runtime"
trusted_issuer = "https://token.actions.githubusercontent.com"
verification_tool = "/usr/local/bin/cosign"
""".strip()
        + "\n"
    )
    monkeypatch.setenv("LEAN_RUNTIME_CONFIG", str(config))
    args = parser().parse_args(["env", "list"])
    assert args.home == "/tmp/lean-runtime-configured"
    assert args.libraries == ["ghcr.io/example/runtime"]
    assert args.availability == "required"
    assert args.publisher_verification == "required"
    assert args.trusted_publisher == "https://github.com/example/runtime"
    assert args.trusted_issuer == "https://token.actions.githubusercontent.com"
    assert args.verification_tool == "/usr/local/bin/cosign"


def test_nearest_project_configuration_overrides_global_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    global_config = tmp_path / "global.toml"
    global_config.write_text('[runtime]\navailability = "auto"\n')
    project = tmp_path / "project"
    nested = project / "src"
    nested.mkdir(parents=True)
    (project / "lean-runtime.toml").write_text(
        'schema = "lean-runtime.project/v1"\n[runtime]\navailability = "local"\n'
    )
    monkeypatch.setenv("LEAN_RUNTIME_CONFIG", str(global_config))
    monkeypatch.chdir(nested)
    assert parser().parse_args(["env", "list"]).availability == "local"


def test_invalid_configuration_is_an_invocation_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[trust]\npublisher_verification = "sometimes"\n')
    monkeypatch.setenv("LEAN_RUNTIME_CONFIG", str(config))
    assert main(["env", "list"]) == 2
    assert "invalid trust.publisher_verification" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("value", "field", "expected"),
    [
        ("mathlib@v4.33.0", "package_refs", ["mathlib@v4.33.0"]),
        ("v4.33.0", "toolchain", "v4.33.0"),
        ("env:research", "environment", "research"),
        ("toolchain:v4.32.2", "toolchain", "v4.32.2"),
    ],
)
def test_using_classifies_context(value: str, field: str, expected: object) -> None:
    args = parser().parse_args(["check", "Main.lean", "--using", value])
    _apply_using(args)
    assert getattr(args, field) == expected


def test_using_classifies_existing_paths(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    lock = tmp_path / "environment.lock.json"
    lock.write_text("{}")
    project_args = parser().parse_args(["check", "Main.lean", "--using", str(project)])
    _apply_using(project_args)
    assert project_args.project == project
    lock_args = parser().parse_args(["check", "Main.lean", "--using", str(lock)])
    _apply_using(lock_args)
    assert lock_args._using_lock == lock


def test_new_rejects_an_existing_project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plan = SimpleNamespace(
        action="adopt",
        root=tmp_path,
        project_name="Fixture",
        mathlib_version="4.33.0",
        toolchain="leanprover/lean4:v4.33.0",
        toolchain_installed=True,
        seed_root=None,
        download_bytes=0,
        blockers=(),
        ready=True,
    )
    monkeypatch.setattr("lean_runtime.cli.Runtime.plan_project_init", lambda *_a, **_k: plan)
    assert main(["--home", str(tmp_path / "home"), "new", str(tmp_path), "--yes"]) == 2


def test_new_guided_creation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "Proof"
    plan = SimpleNamespace(
        action="create",
        root=target,
        project_name="Proof",
        mathlib_version="4.33.0",
        toolchain="leanprover/lean4:v4.33.0",
        toolchain_installed=True,
        seed_root=None,
        download_bytes=0,
        blockers=(),
        ready=True,
    )
    result = SimpleNamespace(root=target, packages=9)
    monkeypatch.setattr("lean_runtime.cli.Runtime.plan_project_init", lambda *_a, **_k: plan)
    monkeypatch.setattr("lean_runtime.cli.Runtime.init_project", lambda *_a, **_k: result)
    assert main(["--home", str(tmp_path / "home"), "new", str(target), "--yes"]) == 0


def test_watch_checks_immediately(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "Main.lean"
    source.write_text("example : True := by trivial\n")
    result = ExecutionResult(
        ok=True,
        exit_code=0,
        toolchain="leanprover/lean4:v4.33.0",
        command=("lean", "Main.lean"),
        cwd=str(tmp_path),
        stdout="",
        stderr="",
        elapsed_seconds=0.01,
    )
    checked: list[Path] = []
    monkeypatch.setattr(
        "lean_runtime.cli.Runtime.check_file",
        lambda _runtime, path, **_kwargs: checked.append(path) or result,
    )
    monkeypatch.setattr(
        "lean_runtime.cli.time.sleep", lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    assert main(["watch", str(source), "--using", f"project:{tmp_path}"]) == 130
    assert checked == [source.resolve()]


def test_lean_file_as_command_suggests_check(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["Main.lean"]) == 2
    assert "lean-runtime check Main.lean" in capsys.readouterr().err


def test_environment_namespace_maps_to_exact_operations() -> None:
    assert parser().parse_args(["env", "list"]).command == "environments"
    assert parser().parse_args(["env", "lock", "e.toml"]).command == "prepare"
    assert parser().parse_args(["env", "acquire", "e.json"]).command == "acquire"
    assert parser().parse_args(["env", "diff", "a", "b"]).command == "compare"
    assert parser().parse_args(["env", "export", "x", "--output", "x.tar"]).command == "copy-save"
    assert parser().parse_args(["env", "import", "x.tar"]).command == "copy-open"


def test_project_vocabulary_is_user_facing() -> None:
    assert parser().parse_args(["project", "info"]).project_command == "inspect"
    assert parser().parse_args(["project", "share"]).command == "attach"
    assert parser().parse_args(["project", "unshare"]).command == "detach"


def test_toolchain_optimize_replaces_slim() -> None:
    assert parser().parse_args(["toolchain", "optimize", "v4.33.0"]).command == ("toolchain-slim")
    with pytest.raises(SystemExit):
        parser().parse_args(["toolchain", "slim", "v4.33.0"])


def test_completion_contains_only_v4_top_level_words(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["completion", "bash"]) == 0
    output = capsys.readouterr().out
    assert "adopt" in output
    assert "status" in output
    assert " init " not in f" {output} "


def test_status_without_context_is_actionable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--home", str(tmp_path / "home"), "status", str(tmp_path)]) == 0
    assert "no pinned Lake project" in capsys.readouterr().out


def test_standalone_status_reports_plan_not_selection_and_availability(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "Standalone.lean"
    source.write_text("import Mathlib\n")
    assert main(["--home", str(tmp_path / "home"), "status", str(source), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert "selected" not in data
    assert data["planned_first"] == data["candidates"][0]
    assert data["availability"][data["planned_first"]]["remote"] == "not_probed"
