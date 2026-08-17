from __future__ import annotations

import io
import json
from importlib.metadata import version as distribution_version
from pathlib import Path
from types import SimpleNamespace

import pytest

from lean_runtime.bundles import PortableCopyInfo
from lean_runtime.cli import _print_operation_failure, _progress, main, parser
from lean_runtime.errors import MaterializationError, PublicationError
from lean_runtime.events import RuntimeEvent
from lean_runtime.models import ExecutionResult
from lean_runtime.oci import PublicationAccess
from lean_runtime.verification import VerificationCheck, VerificationReport


def test_removed_v1_commands_are_absent() -> None:
    commands = set(parser()._subparsers._group_actions[0].choices)
    removed = {
        "resolve",
        "ensure",
        "pull",
        "build-and-push",
        "publish-index",
        "export",
        "import",
        "env-list",
        "cache-status",
        "diff",
        "gc",
        "raw-check",
        "project-build",
        "program-export",
        "program-import",
        "program-pull",
        "program-push",
        "program-publish-index",
    }
    assert commands.isdisjoint(removed)


def test_project_sharing_commands_have_safe_defaults() -> None:
    assert parser().parse_args(["init", "demo"]).mathlib_version == "latest"
    init = parser().parse_args(["init", "demo", "--mathlib-version", "4.33.0"])
    assert init.mathlib_version == "4.33.0"
    # --mathlib is no longer a declared alias; argparse still resolves it as an
    # unambiguous abbreviation of --mathlib-version.
    assert parser().parse_args(["init", "demo", "--mathlib", "4.33.0"]).mathlib_version == "4.33.0"
    assert init.agents
    assert parser().parse_args(["init", "demo", "--core"]).core
    assert parser().parse_args(["init", ".", "--name", "DemoProject"]).name == "DemoProject"
    assert not parser().parse_args(["init", "demo", "--no-agents"]).agents
    attach = parser().parse_args(["project", "attach", "projects", "--recursive"])
    assert attach.recursive and not attach.execute
    detach = parser().parse_args(["project", "detach", "demo"])
    assert not detach.execute
    build = parser().parse_args(["build", "demo"])
    assert build.shared is None
    assert parser().parse_args(["build"]).project == Path(".")
    assert parser().parse_args(["update"]).path == Path(".")
    policy = parser().parse_args(
        ["init", "demo", "--offline", "--max-download", "500MiB", "--plan"]
    )
    assert policy.offline and policy.plan and policy.max_download == "500MiB"
    assert parser().parse_args(["project", "scan"]).path == Path(".")
    with pytest.raises(SystemExit):
        parser().parse_args(["scan"])
    assert parser().parse_args(["check"]).inputs == []
    publish = parser().parse_args(
        ["publish", "environment", "lock.json", "--publish-to", "ghcr.io/example/envs"]
    )
    assert publish.publish_kind == "environment"
    with pytest.raises(SystemExit):
        parser().parse_args(["build", "demo", "--shared", "--local"])


def test_init_completion_recommends_the_fast_first_file_check(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    target = tmp_path / "lowercase-directory"
    plan = SimpleNamespace(
        action="create",
        root=target,
        project_name="ProofProject",
        mathlib_version="4.33.0",
        toolchain="leanprover/lean4:v4.33.0",
        toolchain_installed=True,
        seed_root=tmp_path / "shared",
        download_bytes=0,
        blockers=(),
        ready=True,
    )
    result = SimpleNamespace(root=target, packages=9)
    monkeypatch.setattr(
        "lean_runtime.cli.Runtime.plan_project_init", lambda *_args, **_kwargs: plan
    )
    monkeypatch.setattr("lean_runtime.cli.Runtime.init_project", lambda *_args, **_kwargs: result)

    assert main(["--home", str(tmp_path / "runtime"), "init", str(target)]) == 0

    assert (
        f"Next: cd {target} && lean-runtime check ProofProject/Basic.lean"
        in capsys.readouterr().out
    )


def test_fileless_check_uses_the_current_project(monkeypatch, tmp_path: Path, capsys) -> None:
    result = ExecutionResult(
        ok=True,
        exit_code=0,
        toolchain="leanprover/lean4:v4.33.0",
        command=("lake", "build", "@/Demo:leanArts"),
        cwd=str(tmp_path),
        stdout="",
        stderr="",
        elapsed_seconds=0.01,
    )
    observed: list[tuple[object, object, object]] = []

    def check_project(_runtime, project, *, toolchain=None, policy=None, cancel=None):
        observed.append((project, toolchain, policy.timeout_seconds))
        return result

    monkeypatch.setattr("lean_runtime.cli.Runtime.check_project", check_project)

    assert main(["--home", str(tmp_path / "runtime"), "check", "--timeout", "15"]) == 0
    assert observed == [(Path("."), None, 15.0)]
    assert "accepted:" in capsys.readouterr().out


def test_watch_checks_immediately_and_stops_cleanly(monkeypatch, tmp_path: Path, capsys) -> None:
    source = tmp_path / "Main.lean"
    source.write_text("example : True := by trivial\n")
    result = ExecutionResult(
        ok=True,
        exit_code=0,
        toolchain="leanprover/lean4:v4.33.0",
        command=("lake", "env", "lean", "Main.lean"),
        cwd=str(tmp_path),
        stdout="",
        stderr="",
        elapsed_seconds=0.01,
    )
    checked: list[Path] = []

    def check_file(_runtime, path, **_kwargs):
        checked.append(path)
        return result

    monkeypatch.setattr("lean_runtime.cli.Runtime.check_file", check_file)
    monkeypatch.setattr(
        "lean_runtime.cli.time.sleep", lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt())
    )

    assert main(["check", str(source), "--watch"]) == 130
    assert checked == [source]
    output = capsys.readouterr()
    assert "Watching" in output.out
    assert "accepted:" in output.out
    assert output.err == "lean-runtime: interrupted\n"


def test_stdin_check_displays_a_logical_path(monkeypatch, tmp_path: Path, capsys) -> None:
    staged = ".lake/lean-runtime/check-abc/Main.lean"
    result = ExecutionResult(
        ok=False,
        exit_code=1,
        toolchain="leanprover/lean4:v4.33.0",
        command=("lake", "env", "lean", staged),
        cwd=str(tmp_path),
        stdout="",
        stderr=f"{staged}:2:1: error: unsolved goals\n",
        elapsed_seconds=0.01,
    )
    monkeypatch.setattr("lean_runtime.cli.Runtime.check", lambda *_args, **_kwargs: result)
    monkeypatch.setattr("sys.stdin", io.StringIO("example : False := by trivial\n"))

    assert main(["--home", str(tmp_path / "runtime"), "check", "-"]) == 1

    captured = capsys.readouterr()
    assert "<stdin>:2:1: error: unsolved goals" in captured.err
    assert staged not in captured.err


def test_version_does_not_require_a_command(capsys) -> None:
    with pytest.raises(SystemExit) as stopped:
        parser().parse_args(["--version"])
    assert stopped.value.code == 0
    # Assert against the installed distribution rather than a hardcoded major,
    # so a version bump cannot break this test.
    expected = f"lean-runtime {distribution_version('lean-runtime')}"
    assert capsys.readouterr().out.strip() == expected


def test_standalone_check_error_suggests_an_exact_toolchain(tmp_path: Path, capsys) -> None:
    source = tmp_path / "Main.lean"
    source.write_text("example : True := by trivial\n")

    assert main(["--home", str(tmp_path / "runtime"), "check", str(source)]) == 2

    error = capsys.readouterr().err
    assert "--toolchain v4.33.0" in error
    assert "Traceback" not in error


def test_progress_prints_sparse_frame_and_byte_counters(capsys) -> None:
    _progress(
        RuntimeEvent(
            kind="library.layer_progress",
            message="Downloading sparse capsule frames",
            current_bytes=612 * 2**20,
            total_bytes=2**30,
            data={"frame_current": 132, "frame_total": 410},
        )
    )
    assert capsys.readouterr().err == (
        "lean-runtime: library.layer_progress: Downloading sparse capsule frames · "
        "frames 132/410, 612 MiB/1 GiB\n"
    )


def test_completion_is_generated_from_current_commands(capsys) -> None:
    assert main(["completion", "bash"]) == 0
    output = capsys.readouterr().out
    assert "complete -W" in output
    assert "check" in output and "project" in output and "publish" in output


def test_attach_plan_is_read_only(tmp_path: Path, capsys) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n")
    (project / "lakefile.toml").write_text('name = "project"\n')
    (project / "lake-manifest.json").write_text(json.dumps({"version": "1.2.0", "packages": []}))

    assert main(["--home", str(tmp_path / "runtime"), "project", "attach", str(project)]) == 0
    output = capsys.readouterr().out
    assert "1 Lake project" in output
    assert "No changes made" in output
    assert not (project / ".lake").exists()
    assert not (project / "lean-runtime.toml").exists()


def test_interrupted_command_has_no_traceback(monkeypatch, capsys) -> None:
    def interrupted(**_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("lean_runtime.cli.Runtime", interrupted)
    assert main(["storage"]) == 130
    assert capsys.readouterr().err == "lean-runtime: interrupted\n"


def test_build_and_publish_accepts_environment_build_timeout() -> None:
    arguments = parser().parse_args(
        [
            "publish",
            "environment",
            "environment.lock.json",
            "--publish-to",
            "oci://cache",
            "--timeout",
            "3600",
        ]
    )

    assert arguments.timeout == 3600


def test_publish_access_denial_exits_three_without_opening_environment(monkeypatch, capsys) -> None:
    def denied(_self, _library: str) -> PublicationAccess:
        raise PublicationError(
            "registry denied push access",
            phase="access_preflight",
            registry="ghcr.io/owner/cache",
            status_code=403,
            credential_source="GitHub CLI",
            username="owner",
            hint="run `gh auth refresh -s write:packages,read:packages`, then retry",
        )

    def unexpected_open(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("environment opened before publication access was verified")

    monkeypatch.setattr(
        "lean_runtime.cli.Runtime.begin_publication",
        lambda self, library, **_kwargs: SimpleNamespace(
            check_access=lambda: denied(self, library)
        ),
    )
    monkeypatch.setattr("lean_runtime.cli.Runtime.open_exact", unexpected_open)

    result = main(
        [
            "publish",
            "environment",
            "missing.lock.json",
            "--publish-to",
            "oci://ghcr.io/owner/cache",
        ]
    )

    assert result == 3
    error_output = capsys.readouterr().err
    assert "Nothing was published" in error_output
    assert "owner (source: GitHub CLI)" in error_output
    assert "gh auth refresh" in error_output


def test_publish_check_access_needs_no_lock(monkeypatch, capsys) -> None:
    def allowed(_self, _library: str) -> PublicationAccess:
        return PublicationAccess("ghcr.io/owner/cache", "owner", "GitHub CLI", True)

    monkeypatch.setattr(
        "lean_runtime.cli.Runtime.begin_publication",
        lambda self, library, **_kwargs: SimpleNamespace(
            check_access=lambda: allowed(self, library)
        ),
    )
    result = main(
        [
            "publish",
            "environment",
            "--publish-to",
            "oci://ghcr.io/owner/cache",
            "--check-access",
        ]
    )

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["push_verified"] is True
    assert output["credential_source"] == "GitHub CLI"


def test_publish_failure_json_is_a_versioned_envelope(monkeypatch, capsys) -> None:
    def denied(_self, _library: str) -> PublicationAccess:
        raise PublicationError(
            "registry denied push access",
            phase="access_preflight",
            registry="ghcr.io/owner/cache",
            status_code=403,
            credential_source="GitHub CLI",
            username="owner",
            hint="refresh scopes",
        )

    monkeypatch.setattr(
        "lean_runtime.cli.Runtime.begin_publication",
        lambda self, library, **_kwargs: SimpleNamespace(
            check_access=lambda: denied(self, library)
        ),
    )
    result = main(
        [
            "publish",
            "environment",
            "--publish-to",
            "oci://ghcr.io/owner/cache",
            "--check-access",
            "--json",
        ]
    )

    assert result == 3
    output = json.loads(capsys.readouterr().out)
    assert output["schema"] == "lean-runtime.publication/v1"
    assert output["ok"] is False
    assert output["data"]["published"] is False
    assert output["data"]["status_code"] == 403
    assert output["errors"][0]["code"] == "publication_failed"


def test_publish_access_json_is_a_versioned_envelope(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "lean_runtime.cli.Runtime.begin_publication",
        lambda _self, _library, **_kwargs: SimpleNamespace(
            check_access=lambda: PublicationAccess(
                "ghcr.io/owner/cache", "owner", "GitHub CLI", True
            )
        ),
    )
    result = main(
        [
            "publish",
            "environment",
            "--publish-to",
            "oci://ghcr.io/owner/cache",
            "--check-access",
            "--json",
        ]
    )

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["schema"] == "lean-runtime.publication/v1"
    assert output["ok"] is True
    assert output["data"]["push_verified"] is True


def test_verbose_materialization_failure_includes_tool_output(capsys) -> None:
    failure = MaterializationError(
        "environment build failed",
        phase="build",
        command=("lake", "build"),
        exit_code=1,
        output="specific compiler failure\n",
    )

    _print_operation_failure(failure, verbose=True)

    assert capsys.readouterr().err == (
        "lean-runtime: environment build failed\n"
        "phase: build\n"
        "command: lake build\n"
        "exit code: 1\n"
        "specific compiler failure\n"
    )


def test_check_file_cli_json_result(monkeypatch, tmp_path: Path, capsys) -> None:
    source = tmp_path / "Main.lean"
    source.write_text("example : True := by trivial")
    result = ExecutionResult(
        ok=True,
        exit_code=0,
        toolchain="leanprover/lean4:v4.32.0",
        command=("lean", "Main.lean"),
        cwd=str(tmp_path),
        stdout="",
        stderr="",
        elapsed_seconds=0.01,
    )
    monkeypatch.setattr("lean_runtime.cli.Runtime.check", lambda *args, **kwargs: result)
    assert main(["check", str(source), "--toolchain", "4.32.0", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["schema"] == "lean-runtime.execution/v1"
    assert payload["data"]["toolchain"] == "leanprover/lean4:v4.32.0"


def test_check_cli_accepts_one_local_file(monkeypatch, tmp_path: Path, capsys) -> None:
    source = tmp_path / "Main.lean"
    source.write_text("example : True := by trivial")
    result = ExecutionResult(
        ok=True,
        exit_code=0,
        toolchain="leanprover/lean4:v4.33.0",
        command=("lake", "env", "lean", "Main.lean"),
        cwd=str(tmp_path),
        stdout="",
        stderr="",
        elapsed_seconds=0.01,
    )
    observed = {}

    def check_file(_runtime, path, **options):
        observed.update(path=path, **options)
        return result

    monkeypatch.setattr("lean_runtime.cli.Runtime.check_file", check_file)

    assert main(["check", str(source), "--json"]) == 0
    assert observed["path"] == source
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_managed_check_cli_accepts_supporting_files(monkeypatch, tmp_path: Path, capsys) -> None:
    main_source = tmp_path / "Main.lean"
    support_source = tmp_path / "Defs.lean"
    main_source.write_text("import Defs")
    support_source.write_text("def answer := 42")
    observed = {}
    result = ExecutionResult(
        ok=True,
        exit_code=0,
        toolchain="leanprover/lean4:v4.32.0",
        command=("lean", "Main.lean"),
        cwd=str(tmp_path),
        stdout="",
        stderr="",
        elapsed_seconds=0.01,
    )

    class FakeEnvironment:
        def check_files(self, files, *, entrypoint, policy):
            observed.update(files=files, entrypoint=entrypoint, policy=policy)
            return result

    monkeypatch.setattr("lean_runtime.cli.Runtime.environment", lambda *_args: FakeEnvironment())
    assert (
        main(
            [
                "check",
                "--environment",
                "demo",
                str(main_source),
                "--include",
                str(support_source),
                "--json",
            ]
        )
        == 0
    )
    assert observed["entrypoint"] == "Main.lean"
    assert observed["files"] == {"Main.lean": "import Defs", "Defs.lean": "def answer := 42"}
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_check_cli_discovers_with_packages(monkeypatch, tmp_path: Path, capsys) -> None:
    source = tmp_path / "Main.lean"
    source.write_text("import LeanCert")
    observed = {}
    result = ExecutionResult(
        ok=True,
        exit_code=0,
        toolchain="leanprover/lean4:v4.32.2",
        command=("lean", "Main.lean"),
        cwd=str(tmp_path),
        stdout="",
        stderr="",
        elapsed_seconds=0.01,
    )

    class FakeEnvironment:
        def check_files(self, files, *, entrypoint, policy):
            observed.update(files=files, entrypoint=entrypoint, policy=policy)
            return result

    def open_references(_runtime, references, *, toolchain=None):
        observed.update(references=references, toolchain=toolchain)
        return FakeEnvironment()

    monkeypatch.setattr("lean_runtime.cli.Runtime.open_references", open_references)
    assert (
        main(
            [
                "check",
                str(source),
                "--with",
                "github:alerad/leancert@v4.32.2.4",
                "--json",
            ]
        )
        == 0
    )
    assert observed["references"] == ["github:alerad/leancert@v4.32.2.4"]
    assert observed["entrypoint"] == "Main.lean"
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_save_copy_cli_reports_copy_identity(monkeypatch, tmp_path: Path, capsys) -> None:
    output = tmp_path / "environment.oci.tar.gz"
    info = PortableCopyInfo(
        environment_id="env_" + "a" * 64,
        exact_environment_id="lock_" + "b" * 64,
        copy_id="sha256:" + "c" * 64,
        path=str(output),
    )
    monkeypatch.setattr("lean_runtime.cli.Runtime.save_portable_copy", lambda *_args: info)
    assert main(["--quiet", "copy", "save", "demo", "--output", str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["copy_id"] == info.copy_id


def test_program_create_cli_loads_content_addressed_provenance(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    payload = tmp_path / "payload"
    payload.mkdir()
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"example.protocol.version": "1.0.0"}))
    observed = {}

    def create_program(_runtime, selected_payload, **options):
        observed.update(payload=selected_payload, **options)
        return SimpleNamespace(
            id="program_" + "a" * 64,
            description=SimpleNamespace(to_dict=lambda: {"schema": "test"}),
            root=payload,
        )

    monkeypatch.setattr("lean_runtime.cli.Runtime.create_program", create_program)
    assert (
        main(
            [
                "--quiet",
                "program",
                "create",
                str(payload),
                "--command",
                "bin/checker",
                "--source-revision",
                "b" * 40,
                "--provenance-file",
                str(profile),
            ]
        )
        == 0
    )
    assert observed["provenance"] == {"example.protocol.version": "1.0.0"}
    assert json.loads(capsys.readouterr().out)["program_id"].startswith("program_")


def test_finalize_publication_reads_computer_records(monkeypatch, tmp_path: Path, capsys) -> None:
    result = tmp_path / "computer.json"
    record = {"digest": "sha256:" + "a" * 64, "size": 42}
    result.write_text(json.dumps({"computer_record": record}))
    observed = {}

    def finalize(_runtime, library, lock_id, computer_records, *, tags):
        observed.update(
            library=library,
            lock_id=lock_id,
            computer_records=computer_records,
            tags=tags,
        )
        return "sha256:" + "b" * 64

    monkeypatch.setattr("lean_runtime.cli.Runtime.finalize_publication", finalize)
    assert (
        main(
            [
                "--quiet",
                "finalize",
                "environment",
                "lock_" + "c" * 64,
                str(result),
                "--library",
                "ghcr.io/example/environments",
                "--tag",
                "v2",
            ]
        )
        == 0
    )
    assert observed["computer_records"] == [record]
    assert observed["tags"] == ["v2"]
    assert json.loads(capsys.readouterr().out)["publication_id"].startswith("sha256:")


def test_verify_cli_is_concise_and_json_is_versioned(monkeypatch, capsys) -> None:
    report = VerificationReport(
        "demo",
        "environment",
        (VerificationCheck("lean_probe_passed", True),),
        (),
        (),
        "lock_" + "a" * 64,
        "env_" + "b" * 64,
    )
    monkeypatch.setattr("lean_runtime.cli.Runtime.verify", lambda *_args, **_kwargs: report)
    assert main(["--quiet", "verify", "demo"]) == 0
    assert capsys.readouterr().out == "✓ demo verified\n"
    assert main(["--quiet", "verify", "demo", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == "lean-runtime.verify/v1"


def test_storage_renders_human_summary_and_json(tmp_path: Path, capsys) -> None:
    from lean_runtime.store import EnvironmentStore

    store = EnvironmentStore(tmp_path)
    environment = store.environment_path("env_" + "a" * 64)
    environment.mkdir()
    (environment / "payload.bin").write_bytes(b"x" * 4096)
    store.set_alias("research", environment.name)

    assert main(["--home", str(tmp_path), "storage"]) == 0
    output = capsys.readouterr().out
    assert "Store" in output and str(tmp_path) in output
    assert "Environments" in output and "Total" in output
    assert "4 KiB" in output
    assert "research" in output
    assert "lean-runtime clean" in output

    assert main(["--home", str(tmp_path), "storage", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["environments"] == 1
    assert document["environments_bytes"] >= 4096
    assert document["environment_usage"][0]["aliases"] == ["research"]


def test_clean_previews_then_reclaims_with_human_output(tmp_path: Path, capsys) -> None:
    import os as _os

    from lean_runtime.store import EnvironmentStore

    store = EnvironmentStore(tmp_path)
    candidate = store.environment_path("env_" + "b" * 64)
    candidate.mkdir()
    (candidate / "payload.bin").write_bytes(b"x" * 4096)
    _os.utime(candidate, (1_000_000_000, 1_000_000_000))

    assert main(["--home", str(tmp_path), "clean", "--minimum-age-hours", "0"]) == 0
    preview = capsys.readouterr().out
    assert "Would remove 1 unused environment(s)" in preview
    assert "4 KiB" in preview
    assert "--execute" in preview
    assert candidate.is_dir()

    assert main(["--home", str(tmp_path), "clean", "--minimum-age-hours", "0", "--execute"]) == 0
    applied = capsys.readouterr().out
    assert "Removed 1 environment(s)" in applied
    assert "reclaimed" in applied and "4 KiB" in applied
    assert not candidate.exists()

    assert main(["--home", str(tmp_path), "clean", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["schema"] == "lean-runtime.cleanup/v1"
    assert document["data"]["environments"]["candidates"] == []


def _check_result(ok: bool, cwd: str, name: str, stderr: str = "") -> ExecutionResult:
    return ExecutionResult(
        ok=ok,
        exit_code=0 if ok else 1,
        toolchain="leanprover/lean4:v4.33.0",
        command=("lake", "env", "lean", name),
        cwd=cwd,
        stdout="",
        stderr=stderr,
        elapsed_seconds=0.01,
    )


def test_multi_file_check_reports_each_file_and_fails_on_any_rejection(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    first = tmp_path / "A.lean"
    second = tmp_path / "B.lean"
    first.write_text("example : True := by trivial\n")
    second.write_text("example : False := by trivial\n")
    checked: list[Path] = []

    def check_file(_runtime, path, **_kwargs):
        checked.append(Path(path))
        ok = Path(path).name != "B.lean"
        return _check_result(ok, str(tmp_path), Path(path).name, "" if ok else "error: oops")

    monkeypatch.setattr("lean_runtime.cli.Runtime.check_file", check_file)

    assert main(["--home", str(tmp_path / "runtime"), "check", str(first), str(second)]) == 1
    assert checked == [first, second]
    output = capsys.readouterr()
    assert "accepted" in output.out and "rejected" in output.out
    assert "1/2 accepted" in output.out
    assert "error: oops" in output.err


def test_directory_checks_expand_lean_files_and_skip_hidden_directories(
    monkeypatch, tmp_path: Path
) -> None:
    project = tmp_path / "proj"
    (project / "Sub").mkdir(parents=True)
    (project / "A.lean").write_text("-- a\n")
    (project / "Sub" / "B.lean").write_text("-- b\n")
    (project / "notes.md").write_text("not lean\n")
    (project / ".lake" / "build").mkdir(parents=True)
    (project / ".lake" / "build" / "Staged.lean").write_text("-- staged\n")
    (project / ".git").mkdir()
    (project / ".git" / "Ignored.lean").write_text("-- ignored\n")
    checked: list[Path] = []

    def check_file(_runtime, path, **_kwargs):
        checked.append(Path(path))
        return _check_result(True, str(project), Path(path).name)

    monkeypatch.setattr("lean_runtime.cli.Runtime.check_file", check_file)

    assert main(["--home", str(tmp_path / "runtime"), "check", str(project)]) == 0
    assert checked == [project / "A.lean", project / "Sub" / "B.lean"]


def test_two_existing_files_are_never_treated_as_a_legacy_environment(
    monkeypatch, tmp_path: Path
) -> None:
    first = tmp_path / "First.lean"
    second = tmp_path / "Second.lean"
    first.write_text("-- 1\n")
    second.write_text("-- 2\n")

    def environment(_runtime, _name):
        raise AssertionError("existing files must not resolve as an environment")

    monkeypatch.setattr("lean_runtime.cli.Runtime.environment", environment)
    monkeypatch.setattr(
        "lean_runtime.cli.Runtime.check_file",
        lambda _runtime, path, **_kwargs: _check_result(True, str(tmp_path), Path(path).name),
    )

    assert main(["--home", str(tmp_path / "runtime"), "check", str(first), str(second)]) == 0


def test_legacy_environment_file_pair_is_rejected(tmp_path: Path, capsys) -> None:
    source = tmp_path / "Main.lean"
    source.write_text("example : True := by trivial\n")
    assert main(["--home", str(tmp_path / "runtime"), "check", "myenv", str(source)]) == 2
    assert "check input does not exist" in capsys.readouterr().err


def test_environment_flag_checks_every_file_in_the_environment(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    first = tmp_path / "A.lean"
    second = tmp_path / "B.lean"
    first.write_text("-- 1\n")
    second.write_text("-- 2\n")
    entrypoints: list[str] = []

    class FakeEnvironment:
        def check_files(self, files, *, entrypoint, policy):
            entrypoints.append(entrypoint)
            return _check_result(True, str(tmp_path), entrypoint)

    monkeypatch.setattr(
        "lean_runtime.cli.Runtime.environment", lambda _runtime, _name: FakeEnvironment()
    )

    assert (
        main(
            [
                "--home",
                str(tmp_path / "runtime"),
                "check",
                "--environment",
                "myenv",
                str(first),
                str(second),
                "--json",
            ]
        )
        == 0
    )
    assert len(entrypoints) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "lean-runtime.check-batch/v1"
    assert payload["ok"] is True
    assert payload["data"]["total"] == 2 and payload["data"]["accepted"] == 2


def test_missing_check_input_is_a_clear_invocation_error(tmp_path: Path, capsys) -> None:
    present = tmp_path / "Present.lean"
    present.write_text("-- here\n")
    missing = tmp_path / "Missing.lean"
    assert main(["--home", str(tmp_path / "runtime"), "check", str(present), str(missing)]) == 2
    assert "check input does not exist" in capsys.readouterr().err


def test_environment_flag_rejects_conflicting_selectors(tmp_path: Path, capsys) -> None:
    source = tmp_path / "Main.lean"
    source.write_text("-- x\n")
    assert (
        main(
            [
                "--home",
                str(tmp_path / "runtime"),
                "check",
                "--environment",
                "myenv",
                "--toolchain",
                "v4.33.0",
                str(source),
            ]
        )
        == 2
    )
    assert "--environment cannot be combined" in capsys.readouterr().err


def _completion_words(script: str, shell: str) -> set[str]:
    if shell == "bash":
        return set(script.split("'")[1].split())
    if shell == "zsh":
        return set(script.split("(", 1)[1].split(")", 1)[0].split())
    return {line.rsplit(" ", 1)[-1] for line in script.strip().splitlines()}


def test_completion_offers_only_public_commands() -> None:
    from lean_runtime.cli import _completion_script

    for shell in ("bash", "zsh", "fish"):
        words = _completion_words(_completion_script(shell), shell)
        assert {"run", "check", "init", "build", "publish"} <= words
        assert {"program", "toolchain"} <= words
        for removed in (
            "check-file",
            "profile",
            "matrix",
            "save-copy",
            "open-copy",
            "build-and-publish",
            "finalize-publication",
            "toolchain-publish",
            "toolchain-slim",
            "scan",
            "attach",
            "detach",
            "install",
        ):
            assert removed not in words, (shell, removed)


def test_root_help_promotes_run_and_hides_compat_commands(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    output = capsys.readouterr().out
    assert "discover context and check one Lean file" in output
    assert "lean-run FILE" in output
    assert "check-file" not in output
    assert "build-and-publish" not in output
    assert "save-copy" not in output


def test_run_help_documents_precedence_and_shortcut(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["run", "--help"])
    output = capsys.readouterr().out
    assert "context precedence" in output
    assert "lean-run Main.lean" in output


def test_check_help_points_standalone_users_to_run(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["check", "--help"])
    output = capsys.readouterr().out
    assert "lean-runtime run" in output


def test_removed_compatibility_spellings_are_rejected(capsys) -> None:
    for command in (
        "check-file",
        "profile",
        "matrix",
        "save-copy",
        "open-copy",
        "build-and-publish",
        "finalize-publication",
        "toolchain-publish",
        "toolchain-finalize-publication",
        "toolchain-slim",
        "program-create",
        "program-save-copy",
        "program-open-copy",
        "program-download",
        "program-publish",
        "program-finalize-publication",
        "install",
        "scan",
        "attach",
        "detach",
    ):
        with pytest.raises(SystemExit):
            main([command, "--help"])
        capsys.readouterr()


def test_canonical_groups_replace_every_removed_spelling() -> None:
    canonical = (
        ["check", "Main.lean"],
        ["copy", "save", "env", "--output", "copy.tar"],
        ["copy", "open", "copy.tar"],
        ["publish", "environment", "lock.json", "--publish-to", "oci://cache"],
        ["finalize", "environment", "lock_id", "result.json", "--library", "oci://cache"],
        ["publish", "toolchain", "v4.33.0", "--library", "oci://cache"],
        ["finalize", "toolchain", "v4.33.0", "result.json", "--library", "oci://cache"],
        ["program", "create", "payload", "--command", "bin/x", "--source-revision", "abc"],
        ["program", "save", "program_id", "--output", "program.tar"],
        ["program", "open", "program.tar"],
        ["program", "download", "oci://programs", "revision"],
        ["publish", "program", "program_id", "--library", "oci://programs"],
        ["finalize", "program", "revision", "result.json", "--library", "oci://programs"],
        ["toolchain", "install", "v4.33.0"],
        ["toolchain", "slim", "v4.33.0"],
        ["project", "scan"],
        ["project", "attach"],
        ["project", "detach"],
    )
    for argv in canonical:
        assert parser().parse_args(argv) is not None, argv


def test_lean_file_as_command_suggests_run(capsys) -> None:
    assert main(["Main.lean"]) == 2
    output = capsys.readouterr().err
    assert "is a Lean file, not a command" in output
    assert "lean-runtime run Main.lean" in output
    assert "lean-run Main.lean" in output
