from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from lean_runtime.bundles import PortableCopyInfo
from lean_runtime.cli import _print_operation_failure, main, parser
from lean_runtime.errors import MaterializationError
from lean_runtime.models import ExecutionResult
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


def test_build_and_publish_accepts_environment_build_timeout() -> None:
    arguments = parser().parse_args(
        [
            "build-and-publish",
            "environment.lock.json",
            "--publish-to",
            "oci://cache",
            "--timeout",
            "3600",
        ]
    )

    assert arguments.timeout == 3600


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
    assert main(["check-file", str(source), "--toolchain", "4.32.0", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["schema"] == "lean-runtime.execution/v1"
    assert payload["data"]["toolchain"] == "leanprover/lean4:v4.32.0"


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
    assert main(["--quiet", "save-copy", "demo", "--output", str(output)]) == 0
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
                "program-create",
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
                "finalize-publication",
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
