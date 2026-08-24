from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from lean_runtime.cli import PUBLIC_COMMANDS, _apply_using, _render_storage, main, parser
from lean_runtime.models import ExecutionResult
from lean_runtime.store import StoreStatus

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
        ("lean:v4.33.0", "toolchain", "v4.33.0"),
        ("toolchain:lean:v4.33.0", "toolchain", "v4.33.0"),
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


def test_check_help_exposes_write_lock_and_keeps_legacy_alias(
    capsys: pytest.CaptureFixture[str],
) -> None:
    check_parser = parser().parse_args
    assert check_parser(["check", "Main.lean", "--write-lock", "exact.json"]).lock_out == Path(
        "exact.json"
    )
    assert check_parser(["check", "Main.lean", "--lock-out", "exact.json"]).lock_out == Path(
        "exact.json"
    )
    with pytest.raises(SystemExit):
        check_parser(["check", "--help"])
    help_text = capsys.readouterr().out
    assert "--write-lock PATH" in help_text
    assert "--lock-out" not in help_text


def test_write_lock_routes_to_standalone_discovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "Main.lean"
    source.write_text("example : True := trivial\n")
    output = tmp_path / "environment.lock.json"
    observed: list[Path] = []

    def run_front_door(arguments, *, command_name: str) -> int:
        assert command_name == "lean-runtime check"
        observed.append(arguments.lock_out)
        return 0

    monkeypatch.setattr("lean_runtime.cli._run_front_door", run_front_door)

    assert main(["check", str(source), "--write-lock", str(output)]) == 0
    assert observed == [output]


def test_write_lock_rejects_a_toolchain_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "Main.lean"
    source.write_text("example : True := trivial\n")

    assert (
        main(
            [
                "check",
                str(source),
                "--using",
                "v4.33.0",
                "--write-lock",
                str(tmp_path / "environment.lock.json"),
            ]
        )
        == 2
    )
    assert "cannot be combined" in capsys.readouterr().err


def test_repeat_preserves_explicit_toolchain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "Main.lean"
    source.write_text("example : True := trivial\n")
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
    observed: list[str | None] = []

    def check_file(_runtime: object, _path: Path, **kwargs: object) -> ExecutionResult:
        toolchain = kwargs.get("toolchain")
        assert toolchain is None or isinstance(toolchain, str)
        observed.append(toolchain)
        return result

    monkeypatch.setattr("lean_runtime.cli.Runtime.check_file", check_file)
    assert (
        main(
            [
                "--home",
                str(tmp_path / "home"),
                "check",
                str(source),
                "--using",
                "v4.33.0",
                "--repeat",
                "2",
                "--json",
            ]
        )
        == 0
    )
    assert observed == ["v4.33.0", "v4.33.0", "v4.33.0"]


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


def test_new_hint_uses_the_module_directory_lake_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "demoproj"
    plan = SimpleNamespace(
        action="create",
        root=target,
        project_name="demoproj",
        mathlib_version="4.33.0",
        toolchain="leanprover/lean4:v4.33.0",
        toolchain_installed=True,
        seed_root=None,
        download_bytes=0,
        blockers=(),
        ready=True,
    )
    result = SimpleNamespace(root=target, packages=9)

    def init_project(*_args: object, **_kwargs: object) -> SimpleNamespace:
        # Lake capitalizes the library module directory (`demoproj` → `Demoproj`).
        (target / "Demoproj").mkdir(parents=True)
        (target / "Demoproj" / "Basic.lean").write_text('def hello := "world"\n')
        return result

    monkeypatch.setattr("lean_runtime.cli.Runtime.plan_project_init", lambda *_a, **_k: plan)
    monkeypatch.setattr("lean_runtime.cli.Runtime.init_project", init_project)
    assert main(["--home", str(tmp_path / "home"), "new", str(target), "--yes"]) == 0
    output = capsys.readouterr().out
    assert "lean-runtime check Demoproj/Basic.lean" in output
    assert "demoproj/Basic.lean" not in output


def test_stdin_check_infers_the_enclosing_pinned_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = tmp_path / "proof"
    project.mkdir()
    (project / "lakefile.toml").write_text('name = "proof"\n')
    (project / "lean-toolchain").write_text("leanprover/lean4:v4.33.0\n")
    result = ExecutionResult(
        ok=True,
        exit_code=0,
        toolchain="leanprover/lean4:v4.33.0",
        command=("lean", "Main.lean"),
        cwd=str(project),
        stdout="",
        stderr="",
        elapsed_seconds=0.01,
    )
    observed: list[object] = []

    def check(_runtime: object, _source: str, **kwargs: object) -> ExecutionResult:
        observed.append(kwargs.get("project"))
        return result

    monkeypatch.setattr("lean_runtime.cli.Runtime.check", check)
    monkeypatch.setattr("sys.stdin", io.StringIO("example : True := trivial\n"))
    monkeypatch.chdir(project)
    assert main(["--home", str(tmp_path / "home"), "check", "-"]) == 0
    assert observed == [project.resolve()]


def test_stdin_check_outside_a_project_keeps_the_context_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("example : True := trivial\n"))
    monkeypatch.chdir(tmp_path)
    assert main(["--home", str(tmp_path / "home"), "check", "-"]) == 2
    assert "check requires an environment" in capsys.readouterr().err


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


class _FlushRecorder:
    def __init__(self, wrapped: object) -> None:
        self._wrapped = wrapped
        self.flushes = 0

    def flush(self) -> None:
        self.flushes += 1
        self._wrapped.flush()  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> object:
        return getattr(self._wrapped, name)


def test_watch_flushes_output_for_piped_consumers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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
    monkeypatch.setattr(
        "lean_runtime.cli.Runtime.check_file", lambda _runtime, _path, **_kwargs: result
    )
    monkeypatch.setattr(
        "lean_runtime.cli.time.sleep", lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    recorder = _FlushRecorder(sys.stdout)
    monkeypatch.setattr("sys.stdout", recorder)
    assert main(["watch", str(source), "--using", f"project:{tmp_path}"]) == 130
    # The banner and the first check result must both reach a piped consumer.
    assert recorder.flushes >= 2


def test_storage_rendering_separates_large_counts_from_labels(
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = StoreStatus(
        home="/tmp/lean-runtime-store",
        environments=3,
        locks=1,
        sources=2,
        oci_blobs=4,
        cas_artifacts=149529,
        executions=7,
        aliases=1,
        bytes_used=1024,
        bytes_free=2048,
        declaration_indexes=1234567,
    )
    _render_storage(status)
    output = capsys.readouterr().out
    assert "CAS149529" not in output
    assert "indexes1234567" not in output
    assert re.search(r"Shared module CAS\s+149529\s", output)
    assert re.search(r"Declaration indexes\s+1234567\s", output)


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


def _fake_adoption(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    plan = SimpleNamespace(ready=1, blocked=0, to_dict=lambda: {"ready": 1})
    entry = SimpleNamespace(root=root, action="attached", packages=1, reclaimed_bytes=0)
    batch = SimpleNamespace(
        plan=plan, results=(entry,), failures=(), ok=True, to_dict=lambda: {"ok": True}
    )
    monkeypatch.setattr("lean_runtime.cli.Runtime.plan_project_adoption", lambda *_a, **_k: plan)
    monkeypatch.setattr("lean_runtime.cli.Runtime.attach_projects", lambda *_a, **_k: batch)


def test_adopt_writes_an_agent_guide_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    _fake_adoption(monkeypatch, root)
    assert main(["--home", str(tmp_path / "home"), "adopt", str(root), "--yes", "--json"]) == 0
    assert (root / "AGENTS.md").is_file()
    assert "lean-runtime check" in (root / "AGENTS.md").read_text()
    payload = json.loads(capsys.readouterr().out)
    assert payload["agent_guides"] == [str(root / "AGENTS.md")]


def test_adopt_never_replaces_an_existing_agent_guide(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "AGENTS.md").write_text("# custom voice\n")
    _fake_adoption(monkeypatch, root)
    assert main(["--home", str(tmp_path / "home"), "adopt", str(root), "--yes", "--json"]) == 0
    assert (root / "AGENTS.md").read_text() == "# custom voice\n"
    assert json.loads(capsys.readouterr().out)["agent_guides"] == []


def test_adopt_no_agents_skips_the_guide(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    _fake_adoption(monkeypatch, root)
    assert (
        main(
            ["--home", str(tmp_path / "home"), "adopt", str(root), "--yes", "--no-agents", "--json"]
        )
        == 0
    )
    assert not (root / "AGENTS.md").exists()
    assert json.loads(capsys.readouterr().out)["agent_guides"] == []


def test_status_reports_agent_guide_presence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "lakefile.toml").write_text('name = "fixture"\n')
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n")
    assert main(["--home", str(tmp_path / "home"), "status", str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["agents_guide"] is None
    assert main(["--home", str(tmp_path / "home"), "status", str(root)]) == 0
    assert "lean-runtime adopt" in capsys.readouterr().out
    (root / "AGENTS.md").write_text("# guide\n")
    assert main(["--home", str(tmp_path / "home"), "status", str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["agents_guide"] == str(root / "AGENTS.md")


def test_env_acquire_demands_the_full_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lock_file = tmp_path / "environment.lock.json"
    lock_file.write_text("{}")
    observed: dict[str, object] = {}
    monkeypatch.setattr("lean_runtime.cli.EnvironmentLock.load", lambda _path: "LOCK")

    def open_exact(_runtime, lock, **kwargs):  # type: ignore[no-untyped-def]
        observed.update(kwargs, lock=lock)
        return SimpleNamespace(inspect=lambda: SimpleNamespace(to_dict=lambda: {"ok": True}))

    monkeypatch.setattr("lean_runtime.cli.Runtime.open_exact", open_exact)
    assert main(["--home", str(tmp_path / "home"), "env", "acquire", str(lock_file)]) == 0
    assert observed["import_roots"] == ("LeanRuntimeEnvironment",)
    assert json.loads(capsys.readouterr().out) == {"ok": True}
