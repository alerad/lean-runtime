from pathlib import Path

from lean_runtime.models import ExecutionResult
from lean_runtime.run_cli import main


def _result() -> ExecutionResult:
    return ExecutionResult(
        ok=True,
        exit_code=0,
        toolchain="leanprover/lean4:v4.32.0",
        command=("lean", "Main.lean"),
        cwd="/tmp",
        stdout="",
        stderr="",
        elapsed_seconds=0.01,
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

    def resolve_references(self, requires, *, toolchain=None) -> Lock:
        self.calls.append(("resolve", (requires, toolchain)))
        return self.lock

    def ensure(self, lock) -> Environment:
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
