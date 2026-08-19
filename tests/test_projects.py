from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from lean_runtime import (
    AdoptionBatchResult,
    AdoptionPlan,
    AdoptionResult,
    ProjectError,
    Runtime,
    RuntimeEvent,
    SpecificationError,
    discover_project,
)
from lean_runtime._git import git_command
from lean_runtime.errors import DownloadLimitExceeded
from lean_runtime.models import ExecutionResult
from lean_runtime.policies import ExecutionPolicy
from lean_runtime.serialization import sha256_id


class ProjectToolchains:
    def __init__(self, home: Path) -> None:
        self.home = home

    @property
    def environment(self) -> dict[str, str]:
        return os.environ.copy()

    def ensure(self, toolchain: str) -> Path:
        return Path(toolchain)

    def ensure_full(self, toolchain: str, **_kwargs: object) -> Path:
        return Path(toolchain)

    def is_available_locally(self, _toolchain: str) -> bool:
        return True

    def command(self, _toolchain: str, executable: str, *args: str) -> list[str]:
        if executable == "lake" and args[-3:] == ("env", "lean", "--version"):
            return [sys.executable, "-c", "raise SystemExit(0)"]
        if executable == "lake" and args[:2] == ("env", "lean"):
            script = (
                "import pathlib,sys,time; "
                "source=pathlib.Path(sys.argv[1]); "
                "text=source.read_text(); "
                "time.sleep(10) if 'SLOW' in text else None; "
                "raise SystemExit(1 if 'BAD' in text else 0)"
            )
            return [sys.executable, "-c", script, args[-1]]
        return [sys.executable, "-c", "raise SystemExit(0)", executable, *args]


class ArtifactProjectToolchains(ProjectToolchains):
    def __init__(self, home: Path, *, cache_exit_code: int = 0) -> None:
        super().__init__(home)
        self.cache_exit_code = cache_exit_code
        self.calls: list[tuple[str, ...]] = []

    def command(self, toolchain: str, executable: str, *args: str) -> list[str]:
        self.calls.append((executable, *args))
        if executable == "lake" and args == ("exe", "cache", "get"):
            return [sys.executable, "-c", f"raise SystemExit({self.cache_exit_code})"]
        return super().command(toolchain, executable, *args)


class InitProjectToolchains(ProjectToolchains):
    def command(self, toolchain: str, executable: str, *args: str) -> list[str]:
        if executable == "lake" and args == ("build", "--help"):
            return [sys.executable, "-c", "print('-o mappings')"]
        if executable == "lake" and args == ("cache", "add", "--help"):
            return [sys.executable, "-c", "print('mappings --service URL')"]
        if executable == "lake" and "init" in args:
            directory = next(
                value.removeprefix("--dir=") for value in args if value.startswith("--dir=")
            )
            script = (
                "import pathlib,sys; root=pathlib.Path(sys.argv[1]); name=sys.argv[3]; "
                "assert (root/'lean-toolchain').read_text().strip()==sys.argv[2]; "
                "(root/'lean-toolchain').write_text(sys.argv[2]+'\\n'); "
                "(root/'lakefile.toml').write_text(f'name = \\\"{name}\\\"\\n'); "
                "(root/'.git').mkdir(); "
                "(root/f'{name}.lean').write_text(f'import {name}.Basic\\n'); "
                "(root/name).mkdir(); "
                "(root/name/'Basic.lean').write_text('def value := 1\\n')"
            )
            return [sys.executable, "-c", script, directory, toolchain, args[-2]]
        if executable == "lake" and args[-1:] == ("update",):
            directory = next(
                value.removeprefix("--dir=") for value in args if value.startswith("--dir=")
            )
            script = (
                "import json,pathlib,sys; root=pathlib.Path(sys.argv[1]); "
                "(root/'lake-manifest.json').write_text(json.dumps({"
                "'version':'1.2.0','name':'fresh','lakeDir':'.lake',"
                "'packagesDir':'.lake/packages','packages':[]}))"
            )
            return [sys.executable, "-c", script, directory]
        return super().command(toolchain, executable, *args)


class ImportBuildingToolchains(ProjectToolchains):
    def command(self, toolchain: str, executable: str, *args: str) -> list[str]:
        if executable == "lake" and args[:2] == ("env", "lean"):
            script = (
                "import pathlib,sys; source=pathlib.Path(sys.argv[1]); "
                "artifact=pathlib.Path('.lake/build/lib/lean/Sample/Dependency.olean'); "
                "missing='import Sample.Dependency' in source.read_text() "
                "and not artifact.is_file(); "
                "print(\"object file '.lake/build/lib/lean/Sample/Dependency.olean' of module "
                'Sample.Dependency does not exist", file=sys.stderr) if missing else None; '
                "raise SystemExit(1 if missing else 0)"
            )
            return [sys.executable, "-c", script, args[-1]]
        if executable == "lake" and args[:1] == ("build",):
            script = (
                "import pathlib; artifact=pathlib.Path("
                "'.lake/build/lib/lean/Sample/Dependency.olean'); "
                "artifact.parent.mkdir(parents=True, exist_ok=True); artifact.write_bytes(b'olean')"
            )
            return [sys.executable, "-c", script]
        return super().command(toolchain, executable, *args)


def _project(root: Path, *, name: str = "sample") -> Path:
    root.mkdir(parents=True)
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.32.0\n")
    (root / "lakefile.toml").write_text(f'name = "{name}"\n')
    source = root / "Sample" / "Main.lean"
    source.parent.mkdir()
    source.write_text("example : True := by trivial\n")
    return source


def _project_with_mathlib_manifest(root: Path) -> Path:
    source = _project(root)
    (root / "lake-manifest.json").write_text(
        json.dumps(
            {
                "version": "1.2.0",
                "packagesDir": ".lake/packages",
                "packages": [
                    {
                        "type": "git",
                        "name": "mathlib",
                        "url": "https://github.com/leanprover-community/mathlib4.git",
                        "rev": "a" * 40,
                    }
                ],
            }
        )
    )
    return source


def _shared_project(root: Path, dependency: Path) -> tuple[Path, str]:
    source = _project(root)
    subprocess.run(git_command("init", "--quiet", str(dependency)), check=True)
    (dependency / "lakefile.toml").write_text('name = "dep"\n')
    (dependency / "lake-manifest.json").write_text(json.dumps({"version": "1.2.0", "packages": []}))
    (dependency / "Dep.lean").write_text("def sharedValue := 1\n")
    subprocess.run(git_command("-C", str(dependency), "add", "."), check=True)
    subprocess.run(
        git_command(
            "-C",
            str(dependency),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ),
        check=True,
    )
    revision = subprocess.run(
        git_command("-C", str(dependency), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    local = root / ".lake" / "packages" / "dep"
    local.parent.mkdir(parents=True)
    subprocess.run(git_command("clone", "--quiet", str(dependency), str(local)), check=True)
    subprocess.run(
        git_command(
            "-C", str(local), "remote", "set-url", "origin", "https://example.invalid/dep.git"
        ),
        check=True,
    )
    (root / "lake-manifest.json").write_text(
        json.dumps(
            {
                "version": "1.2.0",
                "packagesDir": ".lake/packages",
                "packages": [
                    {
                        "url": "https://example.invalid/dep.git",
                        "type": "git",
                        "scope": "",
                        "rev": revision,
                        "name": "dep",
                        "inherited": False,
                        "configFile": "lakefile.toml",
                    }
                ],
            }
        )
    )
    return source, revision


def _remembered_package_pair(
    root: Path,
    *,
    producer_toolchain: str = "leanprover/lean4:v4.32.0",
    consumer_toolchain: str = "leanprover/lean4:v4.32.0",
    producer_base_revision: str | None = None,
    consumer_base_revision: str | None = None,
    input_revision: str = "v1.0.0",
) -> tuple[Path, Path, str]:
    producer = root / "producer"
    producer.mkdir(parents=True)
    (producer / ".gitignore").write_text("/.lake/\n")
    (producer / "lean-toolchain").write_text(producer_toolchain + "\n")
    (producer / "lakefile.toml").write_text('name = "dep"\n')
    producer_packages: list[dict[str, object]] = []
    if producer_base_revision is not None:
        producer_packages.append(
            {
                "name": "base",
                "type": "git",
                "url": "https://example.invalid/base.git",
                "rev": producer_base_revision,
                "inputRev": "base-display-tag",
                "configFile": "lakefile.toml",
            }
        )
    (producer / "lake-manifest.json").write_text(
        json.dumps(
            {
                "version": "1.2.0",
                "name": "dep",
                "packagesDir": ".lake/packages",
                "packages": producer_packages,
            }
        )
    )
    (producer / "Dep.lean").write_text("def rememberedValue := 1\n")
    subprocess.run(git_command("init", "--quiet", str(producer)), check=True)
    subprocess.run(
        git_command(
            "-C",
            str(producer),
            "remote",
            "add",
            "origin",
            "git@github.com:example/dep.git",
        ),
        check=True,
    )
    subprocess.run(git_command("-C", str(producer), "add", "."), check=True)
    subprocess.run(
        git_command(
            "-C",
            str(producer),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ),
        check=True,
    )
    revision = subprocess.run(
        git_command("-C", str(producer), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    artifact = producer / ".lake" / "build" / "lib" / "lean" / "Dep.olean"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"producer artifact")

    consumer_source = _project(root / "consumer")
    (root / "consumer" / "lean-toolchain").write_text(consumer_toolchain + "\n")
    consumer_packages: list[dict[str, object]] = [
        {
            "name": "dep",
            "type": "git",
            "url": "https://github.com/example/dep",
            "rev": revision,
            "inputRev": input_revision,
            "configFile": "lakefile.toml",
        }
    ]
    if consumer_base_revision is not None:
        consumer_packages.append(
            {
                "name": "base",
                "type": "git",
                "url": "https://example.invalid/base",
                "rev": consumer_base_revision,
                "inputRev": "another-base-tag",
                "configFile": "lakefile.toml",
            }
        )
    (root / "consumer" / "lake-manifest.json").write_text(
        json.dumps(
            {
                "version": "1.2.0",
                "packagesDir": ".lake/packages",
                "packages": consumer_packages,
            }
        )
    )
    return producer, consumer_source, revision


def test_discover_project_walks_up_from_a_lean_file(tmp_path: Path) -> None:
    source = _project(tmp_path / "project")
    context = discover_project(source)
    assert context.root == tmp_path / "project"
    assert context.toolchain == "leanprover/lean4:v4.32.0"
    assert context.lakefile.name == "lakefile.toml"
    assert context.manifest is None


def test_discover_project_selects_the_nearest_nested_project(tmp_path: Path) -> None:
    _project(tmp_path / "outer", name="outer")
    nested = _project(tmp_path / "outer" / "vendor" / "inner", name="inner")
    assert discover_project(nested).root == nested.parents[1]


def test_discover_project_requires_lakefile_and_toolchain(tmp_path: Path) -> None:
    source = tmp_path / "Main.lean"
    source.write_text("example : True := by trivial\n")
    with pytest.raises(ProjectError, match="no pinned Lake project"):
        discover_project(source)


def test_runtime_check_file_can_require_an_explicit_context(tmp_path: Path) -> None:
    source = tmp_path / "Main.lean"
    source.write_text("example : True := by trivial\n")
    runtime = Runtime(home=tmp_path / "runtime", libraries=[])

    with pytest.raises(SpecificationError, match="no explicit context"):
        runtime.check_file(source, discover=False)


def test_check_builds_a_missing_local_import_then_retries(tmp_path: Path) -> None:
    importer = _project(tmp_path / "project")
    dependency = importer.parent / "Dependency.lean"
    dependency.write_text("def localValue : Nat := 1\n")
    importer.write_text("import Sample.Dependency\nexample : Nat := localValue\n")
    events = []
    runtime = Runtime(
        home=tmp_path / "runtime",
        toolchains=ImportBuildingToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
        on_event=events.append,
    )

    result = runtime.check_file(importer)

    assert result.ok
    assert (tmp_path / "project/.lake/build/lib/lean/Sample/Dependency.olean").is_file()
    assert any(event.kind == "project.check_dependency_build_started" for event in events)
    assert result.timings[0].phase == "build"


def test_project_wide_check_builds_only_lake_declared_library_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _project(tmp_path / "project")
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        runtime.project_executor, "_local_libraries", lambda _context: ("Alpha", "Beta")
    )

    result = runtime.check_project(source)

    assert result.ok
    assert result.command[-3:] == ("build", "@/Alpha:leanArts", "@/Beta:leanArts")
    assert "lake" in result.command


def test_project_build_restores_known_dependency_artifacts_by_default(tmp_path: Path) -> None:
    source = _project_with_mathlib_manifest(tmp_path / "project")
    toolchains = ArtifactProjectToolchains(tmp_path / "runtime")
    events: list[RuntimeEvent] = []
    runtime = Runtime(
        home=tmp_path / "runtime",
        toolchains=toolchains,
        libraries=[],  # type: ignore[arg-type]
        on_event=events.append,
    )

    result = runtime.build(source, shared=False)

    assert result.ok
    assert ("lake", "exe", "cache", "get") in toolchains.calls
    assert ("lake", "build") in toolchains.calls
    assert result.timings[0].phase == "artifact_hydration"
    assert any(event.kind == "artifact.hydration_started" for event in events)
    assert any(event.kind == "artifact.hydration_finished" for event in events)


def test_project_build_cache_failure_falls_back_to_source_build(tmp_path: Path) -> None:
    source = _project_with_mathlib_manifest(tmp_path / "project")
    toolchains = ArtifactProjectToolchains(tmp_path / "runtime", cache_exit_code=1)
    events: list[RuntimeEvent] = []
    runtime = Runtime(
        home=tmp_path / "runtime",
        toolchains=toolchains,
        libraries=[],  # type: ignore[arg-type]
        on_event=events.append,
    )

    result = runtime.build(source, shared=False)

    assert result.ok
    assert ("lake", "build") in toolchains.calls
    failed = [event for event in events if event.kind == "artifact.hydration_failed"]
    assert len(failed) == 1
    assert failed[0].data["exit_code"] == 1


def test_project_build_can_disable_dependency_artifact_restoration(tmp_path: Path) -> None:
    source = _project_with_mathlib_manifest(tmp_path / "project")
    toolchains = ArtifactProjectToolchains(tmp_path / "runtime")
    runtime = Runtime(
        home=tmp_path / "runtime",
        toolchains=toolchains,
        libraries=[],  # type: ignore[arg-type]
    )

    result = runtime.build(source, shared=False, artifact_cache=False)

    assert result.ok
    assert ("lake", "exe", "cache", "get") not in toolchains.calls
    assert ("lake", "build") in toolchains.calls


def test_project_build_offline_policy_skips_dependency_artifact_restoration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _project_with_mathlib_manifest(tmp_path / "project")
    toolchains = ArtifactProjectToolchains(tmp_path / "runtime")
    runtime = Runtime(
        home=tmp_path / "runtime",
        toolchains=toolchains,
        libraries=[],  # type: ignore[arg-type]
    )

    def execute(command, **kwargs):
        return ExecutionResult(
            ok=True,
            exit_code=0,
            toolchain=kwargs["toolchain"],
            command=tuple(command),
            cwd=str(kwargs["cwd"]),
            stdout="",
            stderr="",
            elapsed_seconds=0,
        )

    monkeypatch.setattr(runtime, "_raw_result", execute)

    result = runtime.project(source).build(
        policy=ExecutionPolicy(timeout_seconds=30, network="disabled"),
        shared=False,
    )

    assert result.ok
    assert ("lake", "exe", "cache", "get") not in toolchains.calls


def test_shared_project_cache_uses_the_exact_package_override(tmp_path: Path) -> None:
    source, _ = _shared_project(tmp_path / "project", tmp_path / "dependency")
    checkout = tmp_path / "project/.lake/packages/dep"
    mathlib_checkout = checkout.with_name("mathlib")
    checkout.rename(mathlib_checkout)
    subprocess.run(
        git_command(
            "-C",
            str(mathlib_checkout),
            "remote",
            "set-url",
            "origin",
            "https://github.com/leanprover-community/mathlib4.git",
        ),
        check=True,
    )
    manifest = json.loads((tmp_path / "project/lake-manifest.json").read_text())
    manifest["packages"][0]["name"] = "mathlib"
    manifest["packages"][0]["url"] = "https://github.com/leanprover-community/mathlib4.git"
    (tmp_path / "project/lake-manifest.json").write_text(json.dumps(manifest))
    toolchains = ArtifactProjectToolchains(tmp_path / "runtime")
    runtime = Runtime(
        home=tmp_path / "runtime",
        toolchains=toolchains,
        libraries=[],  # type: ignore[arg-type]
    )

    result = runtime.build(source, shared=True)

    assert result.ok
    cache_call = next(call for call in toolchains.calls if call[-3:] == ("exe", "cache", "get"))
    assert cache_call[0] == "lake"
    assert cache_call[1].startswith("--packages=")


def test_project_check_does_not_restore_dependency_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _project_with_mathlib_manifest(tmp_path / "project")
    toolchains = ArtifactProjectToolchains(tmp_path / "runtime")
    runtime = Runtime(
        home=tmp_path / "runtime",
        toolchains=toolchains,
        libraries=[],  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime.project_executor, "_local_libraries", lambda _context: ("Sample",))

    result = runtime.check_project(source)

    assert result.ok
    assert ("lake", "exe", "cache", "get") not in toolchains.calls


def test_shared_project_build_uses_exact_external_package_override(tmp_path: Path) -> None:
    source, revision = _shared_project(tmp_path / "project", tmp_path / "dependency")
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )

    workspace = runtime.prepare_shared_project(source)
    override = json.loads(workspace.overrides_file.read_text())
    shared_dependency = Path(override["packages"][0]["dir"])
    assert shared_dependency != source.parents[1] / ".lake" / "packages" / "dep"
    assert shared_dependency.is_dir()
    assert (
        subprocess.run(
            git_command("-C", str(shared_dependency), "rev-parse", "HEAD"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == revision
    )

    result = runtime.build(source, shared=True)
    assert result.ok
    assert result.command[-2] == f"--packages={workspace.overrides_file}"
    assert result.command[-1] == "build"
    assert runtime.prepare_shared_project(source).reused

    marker = shared_dependency / ".lean-runtime-package.json"
    marker.unlink()
    repaired = runtime.prepare_shared_project(source)
    assert not repaired.reused
    assert marker.is_file()

    marker.write_text("{}")
    repaired_corruption = runtime.prepare_shared_project(source)
    assert not repaired_corruption.reused
    assert marker.is_file()

    record_path = repaired_corruption.root / "workspace.json"
    record = json.loads(record_path.read_text())
    record["package_ids"] = ["../../outside"]
    record_path.write_text(json.dumps(record))
    repaired_again = runtime.prepare_shared_project(source)
    assert not repaired_again.reused
    assert all(
        package_id.startswith("project_package_") for package_id in repaired_again.package_ids
    )


def test_shared_project_requires_a_lock_manifest(tmp_path: Path) -> None:
    source = _project(tmp_path / "project")
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )
    with pytest.raises(ProjectError, match="lake-manifest.json"):
        runtime.build(source, shared=True)


def test_shared_project_with_no_dependencies_reuses_its_workspace(tmp_path: Path) -> None:
    source = _project(tmp_path / "project")
    (tmp_path / "project" / "lake-manifest.json").write_text(
        json.dumps({"version": "1.2.0", "packages": []})
    )
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )

    assert not runtime.prepare_shared_project(source).reused
    assert runtime.prepare_shared_project(source).reused


def test_attach_replaces_only_packages_and_detach_materializes_them(tmp_path: Path) -> None:
    source, revision = _shared_project(tmp_path / "project", tmp_path / "dependency")
    build_artifact = tmp_path / "project" / ".lake" / "build" / "root.olean"
    build_artifact.parent.mkdir(parents=True)
    build_artifact.write_bytes(b"root build")
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )

    plan = runtime.plan_project_adoption(source)
    assert plan.ready == 1
    assert plan.current_dependency_bytes > 0
    assert plan.checkout_bytes_removed == plan.current_dependency_bytes
    assert plan.shared_bytes_reused == 0
    assert plan.new_shared_bytes == plan.current_dependency_bytes
    assert plan.estimated_machine_reclaimable_bytes == 0
    attached = runtime.attach_projects(source)
    assert attached.ok
    package = tmp_path / "project" / ".lake" / "packages" / "dep"
    assert package.is_symlink()
    assert build_artifact.read_bytes() == b"root build"
    assert (tmp_path / "project" / "lean-runtime.toml").is_file()
    assert runtime.build(source).command[-2].startswith("--packages=")
    checked = runtime.check_file(source)
    assert checked.provenance is not None
    assert [package.name for package in checked.provenance.packages] == ["dep"]
    assert checked.provenance.packages[0].revision == revision
    assert checked.provenance.packages[0].tree_hash
    assert checked.provenance.project is not None
    assert checked.provenance.project.workspace_id == attached.results[0].workspace_id
    with pytest.raises(ProjectError, match="unshare"):
        runtime.build(source, shared=False)
    detach_plan = runtime.plan_project_detachment(source)
    assert detach_plan.ready
    assert detach_plan.materialize_bytes > 0

    detached = runtime.detach_project(source)
    assert detached.action == "detached"
    assert package.is_dir() and not package.is_symlink()
    assert build_artifact.read_bytes() == b"root build"
    assert not (tmp_path / "project" / "lean-runtime.toml").exists()
    assert (
        subprocess.run(
            git_command("-C", str(package), "rev-parse", "HEAD"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == revision
    )


def test_second_graph_reuses_compatible_managed_package_without_source_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, _revision = _shared_project(tmp_path / "first", tmp_path / "dependency")
    second_root = tmp_path / "second"
    shutil.copytree(tmp_path / "first", second_root)
    local_path = second_root / "vendor" / "local"
    local_path.mkdir(parents=True)
    (local_path / "lakefile.toml").write_text('name = "local"\n')
    second_manifest = json.loads((second_root / "lake-manifest.json").read_text())
    second_manifest["packages"].append(
        {
            "type": "path",
            "name": "local",
            "dir": "./vendor/local",
            "inherited": False,
            "configFile": "lakefile.toml",
        }
    )
    (second_root / "lake-manifest.json").write_text(json.dumps(second_manifest))
    events = []
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
        on_event=events.append,
    )

    assert runtime.attach_projects(first).ok
    first_target = (tmp_path / "first" / ".lake" / "packages" / "dep").resolve()
    plan = runtime.plan_project_adoption(second_root)
    assert plan.shared_bytes_reused == plan.current_dependency_bytes
    assert plan.new_shared_bytes == 0
    assert plan.checkout_bytes_removed == plan.current_dependency_bytes
    assert plan.estimated_machine_reclaimable_bytes == plan.current_dependency_bytes

    def source_resolution_is_not_needed(**_kwargs):
        raise AssertionError("compatible managed package should bypass source resolution")

    monkeypatch.setattr(
        runtime.shared_projects,
        "_source_checkout",
        source_resolution_is_not_needed,
    )
    attached = runtime.attach_projects(second_root)

    assert attached.ok
    assert (second_root / ".lake" / "packages" / "dep").resolve() == first_target
    assert any(event.kind == "project.shared.package_reused" for event in events)


def test_attach_preserves_repository_roots_for_subdir_packages(tmp_path: Path) -> None:
    dependency = tmp_path / "dependency"
    subprocess.run(git_command("init", "--quiet", str(dependency)), check=True)
    nested = dependency / "nested" / "package"
    nested.mkdir(parents=True)
    (nested / "lakefile.toml").write_text('name = "dep"\n')
    (nested / "lake-manifest.json").write_text(json.dumps({"version": "1.2.0", "packages": []}))
    (nested / "Dep.lean").write_text("def sharedValue := 1\n")
    subprocess.run(git_command("-C", str(dependency), "add", "."), check=True)
    subprocess.run(
        git_command(
            "-C",
            str(dependency),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ),
        check=True,
    )
    revision = subprocess.run(
        git_command("-C", str(dependency), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    source = _project(tmp_path / "project")
    local = tmp_path / "project" / ".lake" / "packages" / "dep"
    local.parent.mkdir(parents=True)
    subprocess.run(git_command("clone", "--quiet", str(dependency), str(local)), check=True)
    (tmp_path / "project" / "lake-manifest.json").write_text(
        json.dumps(
            {
                "version": "1.2.0",
                "packagesDir": ".lake/packages",
                "packages": [
                    {
                        "url": str(dependency),
                        "type": "git",
                        "scope": "",
                        "rev": revision,
                        "name": "dep",
                        "subDir": "nested/package",
                        "inherited": False,
                        "configFile": "lakefile.toml",
                    }
                ],
            }
        )
    )
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )

    assert runtime.attach_projects(source).ok
    attached = tmp_path / "project" / ".lake" / "packages" / "dep"
    assert attached.is_symlink()
    assert (attached / "nested" / "package" / "lakefile.toml").is_file()
    assert not (attached / "lakefile.toml").is_file()

    runtime.detach_project(source)
    assert attached.is_dir() and not attached.is_symlink()
    assert (attached / "nested" / "package" / "lakefile.toml").is_file()


def test_attach_rolls_back_the_package_swap_when_plain_lake_rejects_it(tmp_path: Path) -> None:
    source, _revision = _shared_project(tmp_path / "project", tmp_path / "dependency")
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )
    context = discover_project(source)
    original = tmp_path / "project" / ".lake" / "packages" / "dep"

    def reject_attached_graph(overrides: Path | None) -> None:
        if overrides is None:
            raise ProjectError("plain Lake rejected the graph")

    with pytest.raises(ProjectError, match="plain Lake rejected"):
        runtime.project_adopter.attach(context, probe=reject_attached_graph)

    assert original.is_dir() and not original.is_symlink()
    assert not (tmp_path / "project" / "lean-runtime.toml").exists()
    assert not (tmp_path / "project" / ".lake" / "lean-runtime-attachment.json").exists()


def test_recursive_adoption_plan_reports_broken_projects_without_mutation(tmp_path: Path) -> None:
    source, _revision = _shared_project(tmp_path / "good", tmp_path / "dependency")
    broken = _project(tmp_path / "broken")
    (tmp_path / "broken" / "lake-manifest.json").write_text(
        json.dumps(
            {
                "version": "1.2.0",
                "packages": [{"type": "path", "name": "missing", "dir": "../does-not-exist"}],
            }
        )
    )
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )

    plan = runtime.plan_project_adoption(tmp_path, recursive=True)
    assert {project.root for project in plan.projects} == {source.parents[1], broken.parents[1]}
    assert plan.ready == 1
    assert plan.blocked == 1
    assert any(
        "does not exist" in blocker for project in plan.projects for blocker in project.blockers
    )
    assert not (tmp_path / "good" / "lean-runtime.toml").exists()


def test_scan_registers_an_exact_local_graph_for_future_reuse(tmp_path: Path) -> None:
    source, _revision = _shared_project(tmp_path / "project", tmp_path / "dependency")
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )

    scanned = runtime.scan_projects(tmp_path / "project")
    context = discover_project(source)
    manifest = json.loads((context.root / "lake-manifest.json").read_text())
    seeds, donor = runtime.shared_projects.registered_graph_seeds(
        context.toolchain, manifest["packages"]
    )

    assert scanned.projects == (context.root,)
    assert donor == context.root
    assert seeds["dep"] == context.root / ".lake" / "packages" / "dep"


def test_remembered_project_donates_exact_package_source_and_artifacts(tmp_path: Path) -> None:
    producer, consumer_source, revision = _remembered_package_pair(tmp_path)
    (producer / "lean-runtime.toml").write_text(
        'schema = "lean-runtime-project/1"\ndependencies = "shared"\n'
    )
    events: list[RuntimeEvent] = []
    runtime = Runtime(
        home=tmp_path / "runtime",
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
        on_event=events.append,
    )
    runtime.shared_projects.remember_project(discover_project(producer))

    workspace = runtime.prepare_shared_project(consumer_source)
    package = Path(json.loads(workspace.overrides_file.read_text())["packages"][0]["dir"])

    assert package != producer
    assert not (package / "lean-runtime.toml").exists()
    assert (
        subprocess.run(
            git_command("-C", str(package), "rev-parse", "HEAD"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == revision
    )
    managed_artifact = package / ".lake" / "build" / "lib" / "lean" / "Dep.olean"
    assert managed_artifact.read_bytes() == b"producer artifact"
    (producer / ".lake" / "build" / "lib" / "lean" / "Dep.olean").write_bytes(b"mutated producer")
    assert managed_artifact.read_bytes() == b"producer artifact"
    selected = next(
        event for event in events if event.kind == "project.shared.project_seed_selected"
    )
    assert selected.data["revision"] == revision
    assert selected.data["artifacts"] is True


def test_remembered_package_identity_ignores_input_revision_display_tag(
    tmp_path: Path,
) -> None:
    producer, first_source, _revision = _remembered_package_pair(
        tmp_path / "first", input_revision="v-display-one"
    )
    runtime = Runtime(
        home=tmp_path / "runtime",
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )
    runtime.shared_projects.remember_project(discover_project(producer))
    first = runtime.prepare_shared_project(first_source)
    first_package = Path(json.loads(first.overrides_file.read_text())["packages"][0]["dir"])

    second_source = _project(tmp_path / "second")
    second_manifest_path = tmp_path / "first" / "consumer" / "lake-manifest.json"
    second_manifest = json.loads(second_manifest_path.read_text())
    second_manifest["packages"][0]["inputRev"] = "a-renamed-tag"
    (tmp_path / "second" / "lake-manifest.json").write_text(json.dumps(second_manifest))
    second = runtime.prepare_shared_project(second_source)
    second_package = Path(json.loads(second.overrides_file.read_text())["packages"][0]["dir"])

    assert first.workspace_id != second.workspace_id
    assert first_package == second_package


def test_remembered_package_refuses_same_tag_with_different_resolved_revision(
    tmp_path: Path,
) -> None:
    producer, consumer_source, _revision = _remembered_package_pair(tmp_path)
    runtime = Runtime(
        home=tmp_path / "runtime",
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )
    runtime.shared_projects.remember_project(discover_project(producer))
    context = discover_project(consumer_source)
    manifest = json.loads(context.current_manifest().read_text())
    manifest["packages"][0]["rev"] = "f" * 40

    seeds = runtime.shared_projects.registered_package_seeds(context, manifest["packages"])

    assert "dep" not in seeds


def test_remembered_package_reuses_only_source_when_dependency_cone_differs(
    tmp_path: Path,
) -> None:
    producer, consumer_source, _revision = _remembered_package_pair(
        tmp_path,
        producer_base_revision="a" * 40,
        consumer_base_revision="b" * 40,
    )
    runtime = Runtime(
        home=tmp_path / "runtime",
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )
    runtime.shared_projects.remember_project(discover_project(producer))
    context = discover_project(consumer_source)
    manifest = json.loads(context.current_manifest().read_text())

    seed = runtime.shared_projects.registered_package_seeds(context, manifest["packages"])["dep"]

    assert seed.artifact is None
    assert seed.artifact_miss == "resolved dependency differs: base"


def test_remembered_package_reuses_only_source_when_toolchain_differs(tmp_path: Path) -> None:
    producer, consumer_source, _revision = _remembered_package_pair(
        tmp_path,
        producer_toolchain="leanprover/lean4:v4.31.0",
        consumer_toolchain="leanprover/lean4:v4.32.0",
    )
    runtime = Runtime(
        home=tmp_path / "runtime",
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )
    runtime.shared_projects.remember_project(discover_project(producer))
    context = discover_project(consumer_source)
    manifest = json.loads(context.current_manifest().read_text())

    seed = runtime.shared_projects.registered_package_seeds(context, manifest["packages"])["dep"]

    assert seed.artifact is None
    assert "toolchain differs" in str(seed.artifact_miss)


def test_dirty_remembered_project_is_not_a_package_seed(tmp_path: Path) -> None:
    producer, consumer_source, _revision = _remembered_package_pair(tmp_path)
    (producer / "lean-runtime.toml").write_text(
        'schema = "lean-runtime-project/1"\ndependencies = "shared"\n'
    )
    (producer / "untracked.txt").write_text("do not import me\n")
    runtime = Runtime(
        home=tmp_path / "runtime",
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )
    runtime.shared_projects.remember_project(discover_project(producer))
    context = discover_project(consumer_source)
    manifest = json.loads(context.current_manifest().read_text())

    assert not runtime.shared_projects.registered_package_seeds(context, manifest["packages"])


def test_adoption_does_not_mistake_the_parent_repository_for_a_package(tmp_path: Path) -> None:
    source = _project(tmp_path / "project")
    subprocess.run(git_command("init", "--quiet", str(tmp_path / "project")), check=True)
    placeholder = tmp_path / "project" / ".lake" / "packages" / "dep"
    placeholder.mkdir(parents=True)
    (tmp_path / "project" / "lake-manifest.json").write_text(
        json.dumps(
            {
                "version": "1.2.0",
                "packages": [
                    {
                        "type": "git",
                        "name": "dep",
                        "url": "https://example.invalid/dep.git",
                        "rev": "a" * 40,
                    }
                ],
            }
        )
    )
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )

    plan = runtime.plan_project_adoption(source)
    assert plan.ready == 1
    assert not plan.projects[0].blockers
    assert "empty local placeholder" in plan.projects[0].warnings[0]


def test_init_core_creates_a_standard_project_atomically(tmp_path: Path) -> None:
    events: list[RuntimeEvent] = []
    runtime = Runtime(
        toolchains=InitProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
        on_event=events.append,
    )

    result = runtime.init_project(tmp_path / "fresh", mathlib=None)
    assert result.action == "attached"
    assert (tmp_path / "fresh" / "lakefile.toml").is_file()
    assert "enableArtifactCache = true" in (tmp_path / "fresh" / "lakefile.toml").read_text()
    assert (tmp_path / "fresh" / "lake-manifest.json").is_file()
    assert (tmp_path / "fresh" / "lean-runtime.toml").is_file()
    agents = (tmp_path / "fresh" / "AGENTS.md").read_text()
    assert "lean-runtime build" in agents
    assert "lean-runtime check PATH" in agents
    assert "Do not edit `.lake/packages`" in agents
    assert (tmp_path / "fresh" / ".lake" / "packages").is_dir()
    assert runtime.build(tmp_path / "fresh").command[-2].startswith("--packages=")
    repeated = runtime.init_project(tmp_path / "fresh", mathlib=None)
    assert repeated.action == "already-attached"
    assert repeated.workspace_id == result.workspace_id
    shared_events = [event for event in events if event.kind == "project.shared.workspace_started"]
    assert shared_events
    assert "fresh" in shared_events[0].message
    assert "lean-runtime-init" not in shared_events[0].message


def test_init_can_skip_or_preserve_an_agents_guide(tmp_path: Path) -> None:
    runtime = Runtime(
        toolchains=InitProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )

    runtime.init_project(tmp_path / "without-guide", mathlib=None, agents=False)
    assert not (tmp_path / "without-guide" / "AGENTS.md").exists()

    custom = tmp_path / "with-custom-guide" / "AGENTS.md"
    custom.parent.mkdir()
    custom.write_text("# Custom instructions\n")
    runtime.init_project(custom.parent, mathlib=None)
    assert custom.read_text() == "# Custom instructions\n"


def test_init_can_generate_matching_lean_runtime_ci(tmp_path: Path) -> None:
    runtime = Runtime(
        toolchains=InitProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )

    runtime.init_project(tmp_path / "with-ci", mathlib=None, ci=True)

    workflow = (tmp_path / "with-ci" / ".github" / "workflows" / "lean-runtime.yml").read_text()
    assert "lean-runtime check" in workflow
    assert "hashFiles('lean-toolchain', 'lake-manifest.json')" in workflow
    assert "LEAN_RUNTIME_HOME" in workflow


def test_init_at_an_empty_git_root_preserves_repository_identity_and_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "project"
    target.mkdir()
    subprocess.run(git_command("init", "--quiet", str(target)), check=True)
    tracked = target / "old.txt"
    tracked.write_text("old\n")
    subprocess.run(git_command("-C", str(target), "add", "old.txt"), check=True)
    subprocess.run(
        git_command(
            "-C",
            str(target),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--quiet",
            "-m",
            "baseline",
        ),
        check=True,
    )
    revision = subprocess.run(
        git_command("-C", str(target), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tracked.unlink()
    runtime = Runtime(
        toolchains=InitProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )
    monkeypatch.chdir(target)

    plan = runtime.plan_project_init(target, mathlib=None)
    result = runtime.init_project(target, mathlib=None)

    assert plan.action == "create"
    assert result.action == "attached"
    assert Path.cwd() == target
    observed_revision = subprocess.run(
        git_command("-C", str(target), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert observed_revision == revision
    status = subprocess.run(
        git_command("-C", str(target), "status", "--porcelain"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert " D old.txt" in status
    assert "?? lakefile.toml" in status


def test_init_preserves_compatible_repository_scaffolding(tmp_path: Path) -> None:
    target = tmp_path / "project"
    (target / ".github" / "workflows").mkdir(parents=True)
    (target / ".github" / "workflows" / "ci.yml").write_text("name: existing\n")
    (target / ".gitignore").write_text("private-notes/\n")
    (target / "README.md").write_text("# Existing project\n")
    (target / "LICENSE").write_text("Existing license\n")
    runtime = Runtime(
        toolchains=InitProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )

    runtime.init_project(target, mathlib=None)

    assert (target / ".github" / "workflows" / "ci.yml").read_text() == "name: existing\n"
    assert (target / "README.md").read_text() == "# Existing project\n"
    assert (target / "LICENSE").read_text() == "Existing license\n"
    assert "private-notes/" in (target / ".gitignore").read_text()


def test_init_plan_rejects_nonempty_nonproject_before_acquisition(tmp_path: Path) -> None:
    target = tmp_path / "project"
    target.mkdir()
    (target / "notes.txt").write_text("keep me\n")
    runtime = Runtime(
        toolchains=InitProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )

    with pytest.raises(ProjectError, match=r"notes\.txt"):
        runtime.plan_project_init(target)

    assert (target / "notes.txt").read_text() == "keep me\n"


def test_init_defaults_to_latest_cataloged_mathlib_without_mutation(tmp_path: Path) -> None:
    runtime = Runtime(
        toolchains=InitProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )

    plan = runtime.plan_project_init(tmp_path / "fresh")

    assert plan.action == "create"
    assert plan.mathlib_version == "4.33.0"
    assert plan.toolchain == "leanprover/lean4:v4.33.0"
    assert "mathlib" in plan.packages
    assert not (tmp_path / "fresh").exists()


def test_init_accepts_an_explicit_project_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    toolchains = InitProjectToolchains(tmp_path / "runtime")
    runtime = Runtime(toolchains=toolchains, libraries=[])  # type: ignore[arg-type]
    commands: list[tuple[str, ...]] = []
    original_command = toolchains.command

    def record_command(toolchain: str, executable: str, *args: str) -> list[str]:
        commands.append((executable, *args))
        return original_command(toolchain, executable, *args)

    monkeypatch.setattr(toolchains, "command", record_command)
    plan = runtime.plan_project_init(
        tmp_path / "integralframework", name="IntegralFramework", mathlib=None
    )
    runtime.init_project(tmp_path / "integralframework", name="IntegralFramework", mathlib=None)
    repeated = runtime.init_project(
        tmp_path / "integralframework", name="IntegralFramework", mathlib=None
    )

    assert plan.project_name == "IntegralFramework"
    assert repeated.action == "already-attached"
    assert any(command[-2:] == ("IntegralFramework", "lib") for command in commands)
    with pytest.raises(ProjectError, match="Lean identifier"):
        runtime.plan_project_init(tmp_path / "another", name="not-a-module", mathlib=None)
    with pytest.raises(ProjectError, match="not 'AnotherName'"):
        runtime.plan_project_init(tmp_path / "integralframework", name="AnotherName")


def test_init_policy_fails_closed_for_an_unpriced_full_toolchain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    offline = Runtime(
        toolchains=InitProjectToolchains(tmp_path / "offline-runtime"),
        libraries=[],  # type: ignore[arg-type]
        availability="local",
    )
    monkeypatch.setattr(offline, "_toolchain_installed", lambda _toolchain: False)
    plan = offline.plan_project_init(tmp_path / "offline-project", mathlib=None)
    assert plan.download_bytes is None
    assert not plan.download_bytes_complete
    assert not plan.toolchain_installed
    with pytest.raises(ProjectError, match="full .* toolchain"):
        offline.init_project(tmp_path / "offline-project", mathlib=None)

    bounded = Runtime(
        toolchains=InitProjectToolchains(tmp_path / "bounded-runtime"),
        libraries=[],  # type: ignore[arg-type]
        max_download_bytes=500,
    )
    monkeypatch.setattr(bounded, "_toolchain_installed", lambda _toolchain: False)
    with pytest.raises(DownloadLimitExceeded, match="cannot be preflighted"):
        bounded.init_project(tmp_path / "bounded-project", mathlib=None)


def test_offline_init_plan_does_not_query_remote_libraries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(
        toolchains=InitProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
        availability="local",
    )
    monkeypatch.setattr(
        runtime,
        "plan_exact",
        lambda *_args, **_kwargs: pytest.fail("offline planning queried a remote library"),
    )

    plan = runtime.plan_project_init(tmp_path / "project")

    assert not plan.ready
    assert plan.download_bytes is None
    assert "no exact local Mathlib" in plan.blockers[0]


def test_init_failure_does_not_publish_a_partial_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(
        toolchains=InitProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )
    target = tmp_path / "fresh"
    original = target / "AGENTS.md"
    original.parent.mkdir()
    original.write_text("# Keep me\n")

    def fail_attach(path, **_kwargs):
        root = Path(path)
        plan = AdoptionPlan((), False, 0, 0)
        return AdoptionBatchResult(plan, (), ((root, "simulated attachment failure"),))

    monkeypatch.setattr(runtime, "attach_projects", fail_attach)

    with pytest.raises(ProjectError, match="simulated attachment failure"):
        runtime.init_project(target, mathlib=None)

    assert original.read_text() == "# Keep me\n"
    assert not (target / "lakefile.toml").exists()
    assert not any(target.parent.glob(".fresh.lean-runtime-init-*"))


def test_latest_mathlib_init_uses_tag_input_and_exact_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(
        toolchains=InitProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        runtime,
        "_project_seed_paths",
        lambda *_args, **_kwargs: ({"seed": tmp_path}, tmp_path),
    )

    def accept_attach(path, **_kwargs):
        root = Path(path)
        plan = AdoptionPlan((), False, 0, 0)
        result = AdoptionResult(root, "attached", 9, 0, "workspace")
        return AdoptionBatchResult(plan, (result,))

    monkeypatch.setattr(runtime, "attach_projects", accept_attach)

    runtime.init_project(tmp_path / "fresh")

    lakefile = (tmp_path / "fresh" / "lakefile.toml").read_text()
    root_module = (tmp_path / "fresh" / "fresh.lean").read_text()
    manifest = json.loads((tmp_path / "fresh" / "lake-manifest.json").read_text())
    mathlib = next(item for item in manifest["packages"] if item["name"] == "mathlib")
    assert 'rev = "v4.33.0"' in lakefile
    assert root_module == "import fresh.Basic\n"
    assert mathlib["rev"] == "db584cd6d46c92f209a44c0f1c829460d327499d"
    assert mathlib["inputRev"] == "v4.33.0"


def test_update_plans_catalog_versions_and_applies_exact_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(
        toolchains=InitProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        runtime,
        "_project_seed_paths",
        lambda *_args, **_kwargs: ({"seed": tmp_path}, tmp_path / "donor"),
    )

    def accept_attach(path, **_kwargs):
        root = Path(path)
        adoption = AdoptionPlan((), False, 0, 0)
        result = AdoptionResult(root, "attached", 9, 0, "workspace")
        return AdoptionBatchResult(adoption, (result,))

    monkeypatch.setattr(runtime, "attach_projects", accept_attach)
    project = tmp_path / "project"
    runtime.init_project(project, mathlib="4.32.2")

    plan = runtime.plan_project_update(project)
    assert plan.current_version == "4.32.2"
    assert plan.target_version == "4.33.0"
    assert plan.changed
    assert plan.seed_root == tmp_path / "donor"
    assert plan.download_bytes == 0

    runtime.update_project(project)
    lakefile = (project / "lakefile.toml").read_text()
    manifest = json.loads((project / "lake-manifest.json").read_text())
    mathlib = next(item for item in manifest["packages"] if item["name"] == "mathlib")
    assert 'rev = "v4.33.0"' in lakefile
    assert mathlib["rev"] == "db584cd6d46c92f209a44c0f1c829460d327499d"
    assert (project / "lean-toolchain").read_text().strip() == "leanprover/lean4:v4.33.0"


def test_update_restores_project_metadata_after_failed_adoption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(
        toolchains=InitProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        runtime,
        "_project_seed_paths",
        lambda *_args, **_kwargs: ({"seed": tmp_path}, tmp_path),
    )

    def accept_attach(path, **_kwargs):
        root = Path(path)
        result = AdoptionResult(root, "attached", 9, 0, "workspace")
        return AdoptionBatchResult(AdoptionPlan((), False, 0, 0), (result,))

    monkeypatch.setattr(runtime, "attach_projects", accept_attach)
    project = tmp_path / "project"
    runtime.init_project(project, mathlib="4.32.2")
    before = {
        name: (project / name).read_bytes()
        for name in ("lakefile.toml", "lake-manifest.json", "lean-toolchain")
    }

    def reject_attach(path, **_kwargs):
        return AdoptionBatchResult(
            AdoptionPlan((), False, 0, 0), (), ((Path(path), "simulated failure"),)
        )

    monkeypatch.setattr(runtime, "attach_projects", reject_attach)
    with pytest.raises(ProjectError, match="simulated failure"):
        runtime.update_project(project)

    for name, contents in before.items():
        assert (project / name).read_bytes() == contents


def test_shared_project_reuses_local_git_objects_for_another_revision(tmp_path: Path) -> None:
    source, locked_revision = _shared_project(tmp_path / "project", tmp_path / "dependency")
    dependency = tmp_path / "dependency"
    (dependency / "Dep.lean").write_text("def sharedValue := 2\n")
    subprocess.run(git_command("-C", str(dependency), "add", "."), check=True)
    subprocess.run(
        git_command(
            "-C",
            str(dependency),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--quiet",
            "-m",
            "newer",
        ),
        check=True,
    )
    local = tmp_path / "project" / ".lake" / "packages" / "dep"
    subprocess.run(git_command("-C", str(local), "fetch", "--quiet", str(dependency)), check=True)
    subprocess.run(
        git_command("-C", str(local), "checkout", "--quiet", "--detach", "FETCH_HEAD"),
        check=True,
    )
    assert (
        subprocess.run(
            git_command("-C", str(local), "rev-parse", "HEAD"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        != locked_revision
    )
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )

    workspace = runtime.prepare_shared_project(source)
    shared = Path(json.loads(workspace.overrides_file.read_text())["packages"][0]["dir"])
    assert (
        subprocess.run(
            git_command("-C", str(shared), "rev-parse", "HEAD"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == locked_revision
    )


def test_shared_packages_reuse_only_their_effective_dependency_closure(tmp_path: Path) -> None:
    first_source, _revision = _shared_project(tmp_path / "first", tmp_path / "dependency")
    second_source = _project(tmp_path / "second")
    second_local = tmp_path / "second" / ".lake" / "packages" / "dep"
    second_local.parent.mkdir(parents=True)
    subprocess.run(
        git_command("clone", "--quiet", str(tmp_path / "dependency"), str(second_local)),
        check=True,
    )
    manifest = json.loads((tmp_path / "first" / "lake-manifest.json").read_text())
    manifest["packages"][0]["url"] = "https://example.invalid/dep"
    manifest["packages"][0]["scope"] = "cosmetic-scope"
    local_dependency = tmp_path / "local-dependency"
    local_dependency.mkdir()
    (local_dependency / "Local.lean").write_text("def localValue := 2\n")
    manifest["packages"].append(
        {
            "type": "path",
            "name": "localDependency",
            "dir": "../local-dependency",
            "inherited": False,
        }
    )
    (tmp_path / "second" / "lake-manifest.json").write_text(json.dumps(manifest))
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )

    first = runtime.prepare_shared_project(first_source)
    second = runtime.prepare_shared_project(second_source)
    first_override = json.loads(first.overrides_file.read_text())["packages"][0]["dir"]
    second_override = json.loads(second.overrides_file.read_text())["packages"][0]["dir"]
    assert first.workspace_id != second.workspace_id
    assert first_override == second_override


def test_shared_package_adopts_a_compatible_legacy_managed_path(tmp_path: Path) -> None:
    first_source, _revision = _shared_project(tmp_path / "first", tmp_path / "dependency")
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )
    first = runtime.prepare_shared_project(first_source)
    first_override = Path(json.loads(first.overrides_file.read_text())["packages"][0]["dir"])
    marker = json.loads((first_override / ".lean-runtime-package.json").read_text())
    marker["schema"] = "lean-runtime-shared-project/1"
    marker["package"]["url"] = str(marker["package"]["url"]).removesuffix(".git")
    marker["package"]["scope"] = "legacy-cosmetic-scope"
    legacy_id = sha256_id("project_package", marker)
    legacy = runtime.home / "project-packages" / legacy_id
    shutil.copytree(first_override, legacy)
    (legacy / ".lean-runtime-package.json").write_text(json.dumps(marker))

    second_source = _project(tmp_path / "second")
    second_local = tmp_path / "second" / ".lake" / "packages" / "dep"
    second_local.parent.mkdir(parents=True)
    subprocess.run(
        git_command("clone", "--quiet", str(tmp_path / "dependency"), str(second_local)),
        check=True,
    )
    second_manifest = json.loads((tmp_path / "first" / "lake-manifest.json").read_text())
    second_manifest["packages"][0]["url"] = "https://example.invalid/dep"
    second_manifest["packages"][0]["scope"] = "another-cosmetic-scope"
    (tmp_path / "second" / "lake-manifest.json").write_text(json.dumps(second_manifest))

    second = runtime.shared_projects.prepare(
        discover_project(second_source), seed_package_paths={"dep": legacy}
    )
    second_override = Path(json.loads(second.overrides_file.read_text())["packages"][0]["dir"])
    assert second_override == legacy
    assert second.package_ids == (legacy_id,)


def test_sparse_environment_artifacts_are_grafted_onto_exact_project_sources(
    tmp_path: Path,
) -> None:
    first_source, _revision = _shared_project(tmp_path / "first", tmp_path / "dependency")
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )
    runtime.prepare_shared_project(first_source)
    shutil.rmtree(runtime.home / "project-packages")
    shutil.rmtree(runtime.home / "project-workspaces")

    sparse = tmp_path / "sparse" / "dep" / ".lake" / "build" / "lib" / "lean"
    sparse.mkdir(parents=True)
    (sparse / "Dep.olean").write_bytes(b"verified compiled artifact")
    second_source = _project(tmp_path / "second")
    manifest = json.loads((tmp_path / "first" / "lake-manifest.json").read_text())
    (tmp_path / "second" / "lake-manifest.json").write_text(json.dumps(manifest))

    workspace = runtime.shared_projects.prepare(
        discover_project(second_source), seed_packages=tmp_path / "sparse"
    )
    package = Path(json.loads(workspace.overrides_file.read_text())["packages"][0]["dir"])
    assert (package / ".git").is_dir()
    assert (package / ".lake" / "build" / "lib" / "lean" / "Dep.olean").read_bytes() == (
        b"verified compiled artifact"
    )


def test_project_environment_checks_actual_relative_file_and_records_provenance(
    tmp_path: Path,
) -> None:
    source = _project(tmp_path / "project")
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )
    project = runtime.project(source)
    result = project.check_file(source)
    assert result.ok
    assert result.cwd == str(project.root)
    assert result.command[-1] == "Sample/Main.lean"
    assert result.environment_id is None
    assert result.provenance is not None
    assert result.provenance.project is not None
    assert result.provenance.project.root == str(project.root)
    assert result.provenance.project.workspace_digest.startswith("sha256:")
    assert result.provenance.project.lakefile_digest.startswith("sha256:")


def test_runtime_check_file_discovers_project_and_source_checks_are_disposable(
    tmp_path: Path,
) -> None:
    source = _project(tmp_path / "project")
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )
    assert runtime.check_file(source).ok
    project = runtime.project(source)
    assert project.check("example : True := by trivial", filename="Scratch.lean").ok
    jobs = project.root / ".lake" / "lean-runtime"
    assert not list(jobs.glob("check-*"))


def test_project_request_identity_changes_with_local_workspace(tmp_path: Path) -> None:
    source = _project(tmp_path / "project")
    dependency = source.parent / "Dependency.lean"
    dependency.write_text("def value := 1\n")
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )
    first = runtime.check_file(source)
    dependency.write_text("def value := 2\n")
    second = runtime.check_file(source)
    assert first.provenance is not None and second.provenance is not None
    assert first.provenance.request_digest != second.provenance.request_digest


def test_project_environment_rejects_files_outside_its_root(tmp_path: Path) -> None:
    source = _project(tmp_path / "project")
    outside = tmp_path / "Outside.lean"
    outside.write_text("example : True := by trivial\n")
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )
    with pytest.raises(ProjectError, match="outside the project root"):
        runtime.project(source).check_file(outside)


def test_project_check_propagates_cancellation_to_the_active_process(tmp_path: Path) -> None:
    source = _project(tmp_path / "project")
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )
    cancel = threading.Event()
    timer = threading.Timer(0.1, cancel.set)
    timer.start()
    try:
        result = runtime.project(source).check("-- SLOW", cancel=cancel)
    finally:
        timer.cancel()
    assert result.cancelled
    assert result.exit_code == 130
    assert any("cancelled" in item.message for item in result.diagnostics)


def test_project_async_cancellation_waits_for_process_cleanup(tmp_path: Path) -> None:
    source = _project(tmp_path / "project")
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )

    async def cancel_check() -> None:
        task = asyncio.create_task(runtime.project(source).check_async("-- SLOW"))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_check())
