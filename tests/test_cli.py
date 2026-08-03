from __future__ import annotations

import json
from pathlib import Path

from lean_runtime.cli import main
from lean_runtime.models import ExecutionResult


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
    assert payload["toolchain"] == "leanprover/lean4:v4.32.0"


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
