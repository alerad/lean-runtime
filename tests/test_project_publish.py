from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lean_runtime import EnvironmentLock, ProjectError, Runtime
from lean_runtime.cli import main
from lean_runtime.projects import project_publication_workflow


def _git(path: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", "-C", str(path), *arguments], text=True).strip()


def _project(path: Path, *, multiple: bool = False) -> Path:
    path.mkdir()
    (path / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n")
    libraries = '[[lean_lib]]\nname = "Fixture"\n'
    if multiple:
        libraries += '\n[[lean_lib]]\nname = "FixtureTests"\n'
    (path / "lakefile.toml").write_text(f'name = "fixture"\n\n{libraries}')
    (path / "Fixture.lean").write_text("def exportedAnswer : Nat := 42\n")
    subprocess.run(["git", "init", "--quiet", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "remote",
            "add",
            "origin",
            "git@github.com:example/export-fixture.git",
        ],
        check=True,
    )
    return path


def test_clean_root_github_project_is_publishable_without_network(tmp_path: Path) -> None:
    project = _project(tmp_path / "project")
    plan = Runtime(home=tmp_path / "runtime", libraries=[]).inspect_project_publication(project)
    assert plan.ready
    assert plan.package == "fixture"
    assert plan.modules == ("Fixture",)
    assert plan.selected_module == "Fixture"
    assert plan.repository == "https://github.com/example/export-fixture.git"
    assert plan.revision == _git(project, "rev-parse", "HEAD")
    assert plan.reference == f"github:example/export-fixture@{plan.revision}"


def test_project_preflight_reports_dirty_and_ambiguous_projects(tmp_path: Path) -> None:
    project = _project(tmp_path / "project", multiple=True)
    (project / "Fixture.lean").write_text("def exportedAnswer : Nat := 43\n")
    plan = Runtime(home=tmp_path / "runtime", libraries=[]).inspect_project_publication(project)
    assert not plan.ready
    assert plan.dirty_files == ("Fixture.lean",)
    assert any("checkout is dirty" in blocker for blocker in plan.blockers)
    assert any("multiple importable roots" in blocker for blocker in plan.blockers)
    selected = Runtime(home=tmp_path / "runtime", libraries=[]).inspect_project_publication(
        project, module="Fixture"
    )
    assert selected.selected_module == "Fixture"
    assert not any("multiple importable roots" in blocker for blocker in selected.blockers)


def test_project_preflight_rejects_non_github_and_nested_roots(tmp_path: Path) -> None:
    project = _project(tmp_path / "project")
    subprocess.run(
        ["git", "-C", str(project), "remote", "set-url", "origin", "https://example.test/x"],
        check=True,
    )
    runtime = Runtime(home=tmp_path / "runtime", libraries=[])
    plan = runtime.inspect_project_publication(project)
    assert any("GitHub" in blocker for blocker in plan.blockers)

    nested = project / "nested"
    nested.mkdir()
    (nested / "lean-toolchain").write_text("v4.32.2\n")
    (nested / "lakefile.toml").write_text('name = "nested"\n\n[[lean_lib]]\nname = "Nested"\n')
    plan = runtime.inspect_project_publication(nested)
    assert any("repository root" in blocker for blocker in plan.blockers)


def test_prepare_project_uses_exact_commit_and_selected_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path / "project")
    runtime = Runtime(home=tmp_path / "runtime", libraries=[])
    monkeypatch.setattr("lean_runtime.projects._remote_contains_commit", lambda *_args: None)
    captured = None

    def prepare(spec, *, timeout=900, cancel=None):  # type: ignore[no-untyped-def]
        nonlocal captured
        del timeout, cancel
        captured = spec
        return EnvironmentLock(
            toolchain=spec.toolchain,
            spec_digest=spec.spec_digest,
            root_lakefile='name = "fixture"\n',
            root_module="import Fixture\n",
            manifest={"packages": []},
            packages=(),
        )

    monkeypatch.setattr(runtime, "prepare", prepare)
    runtime.prepare_project(project, module="Fixture")
    assert captured is not None
    package = captured.packages[0]
    assert package.url == "https://github.com/example/export-fixture.git"
    assert package.rev == _git(project, "rev-parse", "HEAD")
    assert package.root_module == "Fixture"


def test_project_publication_workflow_is_a_small_public_caller() -> None:
    workflow = project_publication_workflow(
        library="ghcr.io/example/fixture-environments", module="Fixture"
    )
    assert "alerad/lean-runtime/.github/workflows/publish-project.yml@v2" in workflow
    assert "library: ghcr.io/example/fixture-environments" in workflow
    assert "module: Fixture" in workflow
    assert "public: true" in workflow


@pytest.mark.parametrize("module", ["Fixture Bad", "Fixture/Bad", "9Fixture"])
def test_project_publication_workflow_rejects_invalid_module(module: str) -> None:
    with pytest.raises(ProjectError, match="Lean module name"):
        project_publication_workflow(library="ghcr.io/example/fixture", module=module)


def test_project_init_publish_writes_workflow_and_refuses_overwrite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path / "project")
    output = project / ".github" / "workflows" / "publish.yml"
    arguments = [
        "--home",
        str(tmp_path / "runtime"),
        "project",
        "init-publish",
        str(project),
        "--module",
        "Fixture",
        "--library",
        "ghcr.io/example/fixture",
        "--output",
        str(output),
    ]
    assert main(arguments) == 0
    assert output.is_file()
    assert "Created" in capsys.readouterr().out
    assert main(arguments) == 2
    assert "already exists" in capsys.readouterr().err


def test_invalid_library_is_a_concise_cli_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--library", "not-a-library", "environments"]) == 2
    error = capsys.readouterr().err
    assert "invalid environment library" in error
    assert "Traceback" not in error


def test_require_ready_lists_actionable_blockers(tmp_path: Path) -> None:
    project = _project(tmp_path / "project")
    (project / "untracked.txt").write_text("dirty")
    plan = Runtime(home=tmp_path / "runtime", libraries=[]).inspect_project_publication(project)
    with pytest.raises(ProjectError, match="commit or remove local changes"):
        plan.require_ready()
