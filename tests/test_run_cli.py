import json
from pathlib import Path

from lean_runtime import ProjectError
from lean_runtime.models import Diagnostic, ExecutionResult
from lean_runtime.run_cli import main


def _result(
    *,
    ok: bool = True,
    stderr: str = "",
    diagnostics: tuple[Diagnostic, ...] = (),
) -> ExecutionResult:
    return ExecutionResult(
        ok=ok,
        exit_code=0 if ok else 1,
        toolchain="leanprover/lean4:v4.32.0",
        command=("lean", "Main.lean"),
        cwd="/tmp",
        stdout="",
        stderr=stderr,
        elapsed_seconds=0.01,
        diagnostics=diagnostics,
    )


class Environment:
    def check(self, *_args, **_kwargs) -> ExecutionResult:
        return _result()


class Lock:
    def __init__(self) -> None:
        self.written: Path | None = None

    def write(self, path: Path) -> None:
        self.written = path


class FakeRuntime:
    instance: "FakeRuntime"

    def __init__(self, **_kwargs) -> None:
        self.calls: list[tuple[str, object]] = []
        self.lock = Lock()
        FakeRuntime.instance = self

    def prepare_references(self, requires, *, toolchain=None) -> Lock:
        self.calls.append(("resolve", (requires, toolchain)))
        return self.lock

    def open_exact(self, lock) -> Environment:
        self.calls.append(("ensure", lock))
        return Environment()

    def check_file(self, path, *, toolchain=None, policy=None) -> ExecutionResult:
        self.calls.append(("file", (path, toolchain, policy)))
        return _result()


def test_lean_run_discovers_local_project_by_default(monkeypatch, tmp_path: Path, capsys) -> None:
    source = tmp_path / "Main.lean"
    source.write_text("example : True := by trivial\n")
    monkeypatch.setattr("lean_runtime.run_cli.Runtime", FakeRuntime)
    assert main([str(source), "--json"]) == 0
    assert FakeRuntime.instance.calls[0][0] == "file"
    assert '"ok": true' in capsys.readouterr().out


def test_lean_run_resolves_frontmatter_and_writes_lock(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "Main.lean"
    source.write_text(
        '-- /// lean-runtime\n-- requires = ["mathlib@v4.32.2"]\n-- ///\nimport Mathlib\n'
    )
    output = tmp_path / "environment.lock.json"
    monkeypatch.setattr("lean_runtime.run_cli.Runtime", FakeRuntime)
    assert main([str(source), "--lock-out", str(output), "--quiet"]) == 0
    assert FakeRuntime.instance.calls[0] == (
        "resolve",
        (("mathlib@v4.32.2",), None),
    )
    assert FakeRuntime.instance.lock.written == output


def test_lean_run_resolves_embedded_lock_relative_to_source(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "Main.lean"
    source.write_text(
        '-- /// lean-runtime\n-- lock = "environment.lock.json"\n-- ///\n'
        "example : True := by trivial\n"
    )
    expected = object()
    observed: list[Path] = []
    monkeypatch.setattr("lean_runtime.run_cli.Runtime", FakeRuntime)
    monkeypatch.setattr(
        "lean_runtime.run_cli.EnvironmentLock.load",
        lambda path: observed.append(path) or expected,
    )
    assert main([str(source), "--quiet"]) == 0
    assert observed == [tmp_path / "environment.lock.json"]
    assert FakeRuntime.instance.calls[0] == ("ensure", expected)


def test_lean_run_rejects_conflicting_cli_and_frontmatter(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    source = tmp_path / "Main.lean"
    source.write_text(
        '-- /// lean-runtime\n-- requires = ["mathlib@v4.32.2"]\n-- ///\nimport Mathlib\n'
    )
    monkeypatch.setattr("lean_runtime.run_cli.Runtime", FakeRuntime)
    assert main([str(source), "--with", "alerad/leancert@v1"]) == 2
    assert "cannot combine --with" in capsys.readouterr().err


def test_lean_run_explains_explicit_dependencies_without_execution(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    source = tmp_path / "Main.lean"
    source.write_text("import Mathlib\n")

    class MustNotStart:
        def __init__(self, **_kwargs) -> None:
            raise AssertionError("explanation must not construct a runtime")

    monkeypatch.setattr("lean_runtime.run_cli.Runtime", MustNotStart)
    assert main([str(source), "--with", "mathlib@v4.32.2", "--explain"]) == 0
    output = capsys.readouterr().out
    assert "Context: standalone dependencies" in output
    assert "mathlib@v4.32.2" in output


def test_lean_run_discovers_standalone_file_and_writes_lock(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    source = tmp_path / "Main.lean"
    source.write_text("import Mathlib\nexample : 2 + 2 = 4 := by norm_num\n")
    output = tmp_path / "environment.lock.json"

    class NoProjectRuntime(FakeRuntime):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.availability = "auto"
            self.libraries = ()

        def check_file(self, *_args, **_kwargs) -> ExecutionResult:
            raise ProjectError("no project")

    class Found:
        status = "found"
        execution_result = _result()
        lock = Lock()

    class FakeDiscovery:
        def __init__(self, **_kwargs) -> None:
            pass

        def discover_and_check(self, _source: str) -> Found:
            return Found()

    monkeypatch.setattr("lean_runtime.run_cli.Runtime", NoProjectRuntime)
    monkeypatch.setattr("lean_runtime.run_cli.Discovery", FakeDiscovery)
    assert main([str(source), "--lock-out", str(output), "--quiet"]) == 0
    assert Found.lock.written == output
    assert "accepted" in capsys.readouterr().out


def test_lean_run_preserves_discovered_compiler_rejection(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    source = tmp_path / "Main.lean"
    source.write_text("import Mathlib\nexample : False := by trivial\n")
    rejection = _result(
        ok=False,
        stderr="Main.lean:2:22: error: tactic 'trivial' failed",
        diagnostics=(
            Diagnostic(
                "error",
                "tactic 'trivial' failed",
                file="Main.lean",
                line=2,
                column=22,
            ),
        ),
    )

    class NoProjectRuntime(FakeRuntime):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.availability = "auto"
            self.libraries = ()

        def check_file(self, *_args, **_kwargs) -> ExecutionResult:
            raise ProjectError("no project")

    class Attempt:
        execution_result = rejection

    class Rejected:
        status = "not_found"
        execution_result = None
        rejection_attempt = Attempt()

    class FakeDiscovery:
        def __init__(self, **_kwargs) -> None:
            pass

        def discover_and_check(self, _source: str) -> Rejected:
            return Rejected()

    monkeypatch.setattr("lean_runtime.run_cli.Runtime", NoProjectRuntime)
    monkeypatch.setattr("lean_runtime.run_cli.Discovery", FakeDiscovery)

    assert main([str(source), "--quiet"]) == 1
    captured = capsys.readouterr()
    assert "tactic 'trivial' failed" in captured.err
    assert "rejected" in captured.out

    assert main([str(source), "--json", "--quiet"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["data"]["stderr"].endswith("tactic 'trivial' failed")
    assert payload["data"]["diagnostics"][0]["message"] == "tactic 'trivial' failed"
    assert payload["data"]["timings"][0]["phase"] == "discovery"


def test_lean_run_can_disable_automatic_discovery(monkeypatch, tmp_path: Path, capsys) -> None:
    source = tmp_path / "Main.lean"
    source.write_text("import Mathlib\n")

    class NoProjectRuntime(FakeRuntime):
        def check_file(self, *_args, **_kwargs) -> ExecutionResult:
            raise ProjectError("no project")

    monkeypatch.setattr("lean_runtime.run_cli.Runtime", NoProjectRuntime)
    assert main([str(source), "--no-discover", "--quiet"]) == 2
    assert "no execution context" in capsys.readouterr().err


def test_lean_run_explains_bundled_catalog_candidates(tmp_path: Path, capsys) -> None:
    source = tmp_path / "Main.lean"
    source.write_text("import Mathlib\n")
    assert main([str(source), "--explain"]) == 0
    output = capsys.readouterr().out
    assert "Context: automatic discovery" in output
    assert "mathlib-v4.32.2" in output


def test_lean_run_rewrites_staged_paths_to_the_user_path(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    source = tmp_path / "Bad.lean"
    source.write_text("example : 2 + 2 = 5 := rfl\n")
    staged = "/store/jobs/execution_ab/instance-cd"

    class RejectingRuntime(FakeRuntime):
        def check_file(self, path, *, toolchain=None, policy=None) -> ExecutionResult:
            del path, toolchain, policy
            return ExecutionResult(
                ok=False,
                exit_code=1,
                toolchain="leanprover/lean4:v4.32.0",
                command=("lean", f"{staged}/Bad.lean"),
                cwd=staged,
                stdout=f"{staged}/Bad.lean:1:23: error: Type mismatch\n",
                stderr="",
                elapsed_seconds=0.01,
            )

    monkeypatch.setattr("lean_runtime.run_cli.Runtime", RejectingRuntime)
    assert main([str(source), "--quiet", "--toolchain", "v4.32.0"]) == 1
    captured = capsys.readouterr()
    assert f"{source}:1:23: error: Type mismatch" in captured.out
    assert staged not in captured.out
    assert f"✗ {source} rejected" in captured.out
