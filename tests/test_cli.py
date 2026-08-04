from __future__ import annotations

import json
from pathlib import Path

from lean_runtime.bundles import BundleInfo
from lean_runtime.cli import main
from lean_runtime.models import ExecutionResult
from lean_runtime.verification import VerificationCheck, VerificationReport


def test_raw_check_cli_json_result(monkeypatch, tmp_path: Path, capsys) -> None:
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
    assert main(["raw-check", str(source), "--toolchain", "4.32.0", "--json"]) == 0
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

    monkeypatch.setattr("lean_runtime.cli.Runtime.open", lambda *_args: FakeEnvironment())
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

    def ensure_references(_runtime, references, *, toolchain=None):
        observed.update(references=references, toolchain=toolchain)
        return FakeEnvironment()

    monkeypatch.setattr("lean_runtime.cli.Runtime.ensure_references", ensure_references)
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


def test_export_cli_reports_bundle_identity(monkeypatch, tmp_path: Path, capsys) -> None:
    output = tmp_path / "environment.oci.tar.gz"
    info = BundleInfo(
        environment_id="env_" + "a" * 64,
        lock_id="lock_" + "b" * 64,
        manifest_digest="sha256:" + "c" * 64,
        path=str(output),
    )
    monkeypatch.setattr("lean_runtime.cli.Runtime.export_environment", lambda *_args: info)
    assert main(["--quiet", "export", "demo", "--output", str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["manifest_digest"] == info.manifest_digest


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
