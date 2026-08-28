from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

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
from lean_runtime._paths import is_link, remove_tree
from lean_runtime.errors import DownloadLimitExceeded
from lean_runtime.models import ExecutionResult
from lean_runtime.package_ids import is_package_id
from lean_runtime.policies import ExecutionPolicy
from lean_runtime.serialization import sha256_id


class ProjectToolchains:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.executable_digests = {
            "lean": "sha256:" + "1" * 64,
            "lake": "sha256:" + "2" * 64,
        }

    @property
    def environment(self) -> dict[str, str]:
        return os.environ.copy()

    def environment_for(self, _toolchain: str) -> dict[str, str]:
        return self.environment

    def ensure(self, toolchain: str) -> Path:
        return Path(toolchain)

    def ensure_full(self, toolchain: str, **_kwargs: object) -> Path:
        return Path(toolchain)

    def executable_digest(self, _toolchain: str, executable: str) -> str:
        return self.executable_digests[executable]

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


def test_project_build_skips_dependency_hydration_when_build_is_ready(tmp_path: Path) -> None:
    source = _project_with_mathlib_manifest(tmp_path / "project")
    lean_build = (
        tmp_path
        / "project"
        / ".lake"
        / "packages"
        / "mathlib"
        / ".lake"
        / "build"
        / "lib"
        / "lean"
    )
    lean_build.mkdir(parents=True)
    toolchains = ArtifactProjectToolchains(tmp_path / "runtime")
    runtime = Runtime(
        home=tmp_path / "runtime",
        toolchains=toolchains,
        libraries=[],  # type: ignore[arg-type]
    )

    result = runtime.build(source, shared=False)

    assert result.ok
    assert ("lake", "exe", "cache", "get") not in toolchains.calls
    assert ("lake", "build") in toolchains.calls


def test_normal_project_use_does_not_enroll_a_global_seed(tmp_path: Path) -> None:
    source = _project(tmp_path / "project")
    (tmp_path / "project" / "lake-manifest.json").write_text(
        json.dumps({"version": "1.2.0", "packages": []})
    )
    runtime = Runtime(
        home=tmp_path / "runtime",
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )

    runtime.project(source)
    runtime.build(source, shared=False, artifact_cache=False)

    assert runtime.shared_projects.remembered_roots() == ()
    assert runtime.scan_projects(source, recursive=False).projects == (source.parents[1],)
    assert runtime.shared_projects.remembered_roots() == (source.parents[1],)


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
    assert all(is_package_id(package_id) for package_id in repaired_again.package_ids)


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
    assert is_link(package)
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
    assert package.is_dir() and not is_link(package)
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


def test_reattach_repoints_packages_when_the_toolchain_binary_changes(tmp_path: Path) -> None:
    source, _revision = _shared_project(tmp_path / "project", tmp_path / "dependency")
    toolchains = ProjectToolchains(tmp_path / "runtime")
    runtime = Runtime(toolchains=toolchains, libraries=[])  # type: ignore[arg-type]

    first = runtime.attach_projects(source).results[0]
    package_link = tmp_path / "project" / ".lake" / "packages" / "dep"
    first_target = package_link.resolve()
    old_artifact = first_target / ".lake" / "build" / "lib" / "lean" / "Dep.olean"
    old_artifact.parent.mkdir(parents=True)
    old_artifact.write_bytes(b"old toolchain")

    toolchains.executable_digests["lean"] = "sha256:" + "3" * 64
    second = runtime.attach_projects(source).results[0]
    second_target = package_link.resolve()

    assert first.action == "attached"
    assert second.action == "attached"
    assert second.workspace_id != first.workspace_id
    assert second_target != first_target
    assert not (second_target / ".lake" / "build").exists()
    marker = json.loads((second_target / ".lean-runtime-package.json").read_text())
    assert marker["artifact_key"]["lean_executable_digest"] == "sha256:" + "3" * 64


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
    # Planning does not install or hash a toolchain, so artifact reuse remains
    # conservative until attach computes the local build identity.
    assert plan.shared_bytes_reused == 0
    assert plan.new_shared_bytes == plan.current_dependency_bytes
    assert plan.checkout_bytes_removed == plan.current_dependency_bytes
    assert plan.estimated_machine_reclaimable_bytes == 0

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
    assert is_link(attached)
    assert (attached / "nested" / "package" / "lakefile.toml").is_file()
    assert not (attached / "lakefile.toml").is_file()

    runtime.detach_project(source)
    assert attached.is_dir() and not is_link(attached)
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

    assert original.is_dir() and not is_link(original)
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


def test_registered_graph_donates_source_across_toolchains_without_artifacts(
    tmp_path: Path,
) -> None:
    producer_source, _revision = _shared_project(tmp_path / "producer", tmp_path / "dependency")
    producer = producer_source.parents[1]
    donor_package = producer / ".lake" / "packages" / "dep"
    donor_artifact = donor_package / ".lake" / "build" / "lib" / "lean" / "Dep.olean"
    donor_artifact.parent.mkdir(parents=True)
    donor_artifact.write_bytes(b"lean 4.32 artifact")
    (donor_package / ".git" / "info" / "exclude").write_text("/.lake/\n")
    consumer = tmp_path / "consumer"
    shutil.copytree(producer, consumer, ignore=shutil.ignore_patterns(".lake"))
    (consumer / "lean-toolchain").write_text("leanprover/lean4:v4.33.1\n")
    events: list[RuntimeEvent] = []
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
        on_event=events.append,
    )
    runtime.scan_projects(producer)

    workspace = runtime.prepare_shared_project(consumer)
    package = Path(json.loads(workspace.overrides_file.read_text())["packages"][0]["dir"])
    marker = json.loads((package / ".lean-runtime-package.json").read_text())

    assert (package / "Dep.lean").read_text() == (donor_package / "Dep.lean").read_text()
    assert not (package / ".lake" / "build").exists()
    assert marker["artifact_key"]["toolchain"] == "leanprover/lean4:v4.33.1"
    selected = [event for event in events if event.kind == "project.shared.project_seed_selected"]
    assert selected
    assert selected[0].data["donor"] == str(producer)
    assert selected[0].data["artifacts"] is False


def test_remembered_project_without_build_identity_donates_source_only(tmp_path: Path) -> None:
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
    assert not managed_artifact.exists()
    selected = next(
        event for event in events if event.kind == "project.shared.project_seed_selected"
    )
    assert selected.data["revision"] == revision
    assert selected.data["artifacts"] is False
    assert selected.data["artifact_miss"] == "donor has no matching toolchain artifact identity"


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


def test_unrelated_remembered_project_is_rejected_before_path_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unrelated = tmp_path / "unrelated"
    unrelated_source = _project(unrelated)
    path_dependency = unrelated / "local-data"
    path_dependency.mkdir()
    (path_dependency / "large.bin").write_bytes(b"x" * 4096)
    (unrelated / "lake-manifest.json").write_text(
        json.dumps(
            {
                "version": "1.2.0",
                "packages": [
                    {"name": "local-data", "type": "path", "dir": "local-data"}
                ],
            }
        )
    )
    subprocess.run(git_command("init", "--quiet", str(unrelated)), check=True)
    subprocess.run(
        git_command(
            "-C", str(unrelated), "remote", "add", "origin", "https://example.invalid/other"
        ),
        check=True,
    )
    subprocess.run(git_command("-C", str(unrelated), "add", "."), check=True)
    subprocess.run(
        git_command(
            "-C",
            str(unrelated),
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
    consumer_source = _project(tmp_path / "consumer")
    consumer_manifest = {
        "version": "1.2.0",
        "packages": [
            {
                "name": "wanted",
                "type": "git",
                "url": "https://example.invalid/wanted",
                "rev": "a" * 40,
            }
        ],
    }
    (tmp_path / "consumer" / "lake-manifest.json").write_text(json.dumps(consumer_manifest))
    runtime = Runtime(
        home=tmp_path / "runtime",
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )
    runtime.shared_projects.remember_project(discover_project(unrelated_source))

    def reject_hash(_path: Path) -> str:
        raise AssertionError("unrelated path dependency was hashed")

    monkeypatch.setattr("lean_runtime.shared_projects.source_snapshot_digest", reject_hash)

    context = discover_project(consumer_source)
    assert runtime.shared_projects.registered_package_seeds(
        context, consumer_manifest["packages"]
    ) == {}
    registry = json.loads(runtime.shared_projects.seed_registry.read_text())
    assert registry["projects"][0]["remote"] == "https://example.invalid/other"


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
    assert plan.mathlib_version == "4.33.1"
    assert plan.toolchain == "leanprover/lean4:v4.33.1"
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
    assert 'rev = "v4.33.1"' in lakefile
    assert root_module == "import fresh.Basic\n"
    assert mathlib["rev"] == "0df444a360eaa60ab8c11dca51a86af692955474"
    assert mathlib["inputRev"] == "v4.33.1"


def test_latest_mathlib_init_accelerates_a_cold_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(
        toolchains=InitProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_project_seed_paths", lambda *_args, **_kwargs: ({}, None))
    environment_workspace = tmp_path / "environment" / "workspace"
    (environment_workspace / ".lake" / "packages").mkdir(parents=True)
    observed: list[bool] = []

    def open_exact(_lock, **kwargs):
        observed.append(kwargs.get("accelerate") is True)
        return SimpleNamespace(workspace=environment_workspace)

    def accept_attach(path, **_kwargs):
        root = Path(path)
        plan = AdoptionPlan((), False, 0, 0)
        result = AdoptionResult(root, "attached", 9, 0, "workspace")
        return AdoptionBatchResult(plan, (result,))

    monkeypatch.setattr(runtime, "open_exact", open_exact)
    monkeypatch.setattr(runtime, "attach_projects", accept_attach)

    runtime.init_project(tmp_path / "fresh")

    assert observed == [True]


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
    assert plan.target_version == "4.33.1"
    assert plan.changed
    assert plan.seed_root == tmp_path / "donor"
    assert plan.download_bytes == 0

    runtime.update_project(project)
    lakefile = (project / "lakefile.toml").read_text()
    manifest = json.loads((project / "lake-manifest.json").read_text())
    mathlib = next(item for item in manifest["packages"] if item["name"] == "mathlib")
    assert 'rev = "v4.33.1"' in lakefile
    assert mathlib["rev"] == "0df444a360eaa60ab8c11dca51a86af692955474"
    assert (project / "lean-toolchain").read_text().strip() == "leanprover/lean4:v4.33.1"


def test_update_is_a_successful_noop_without_mathlib(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(
        toolchains=InitProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )

    def accept_attach(path, **_kwargs):
        root = Path(path)
        result = AdoptionResult(root, "attached", 0, 0, "workspace")
        return AdoptionBatchResult(AdoptionPlan((), False, 0, 0), (result,))

    monkeypatch.setattr(runtime, "attach_projects", accept_attach)
    project = tmp_path / "project"
    runtime.init_project(project, mathlib=None)

    plan = runtime.plan_project_update(project)
    assert plan.ready
    assert not plan.changed
    assert plan.current_version == plan.target_version == "core"
    assert plan.packages == ()
    assert plan.download_bytes == 0
    assert runtime.update_project(project) == plan


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


def test_shared_package_artifacts_are_rekeyed_when_toolchain_binary_changes(
    tmp_path: Path,
) -> None:
    first_source, _revision = _shared_project(tmp_path / "first", tmp_path / "dependency")
    second_source = _project(tmp_path / "second")
    second_local = tmp_path / "second" / ".lake" / "packages" / "dep"
    second_local.parent.mkdir(parents=True)
    subprocess.run(
        git_command("clone", "--quiet", str(tmp_path / "dependency"), str(second_local)),
        check=True,
    )
    (tmp_path / "second" / "lake-manifest.json").write_bytes(
        (tmp_path / "first" / "lake-manifest.json").read_bytes()
    )
    toolchains = ProjectToolchains(tmp_path / "runtime")
    runtime = Runtime(toolchains=toolchains, libraries=[])  # type: ignore[arg-type]

    first = runtime.prepare_shared_project(first_source)
    first_package = Path(json.loads(first.overrides_file.read_text())["packages"][0]["dir"])
    artifact = first_package / ".lake" / "build" / "lib" / "lean" / "Dep.olean"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"old compiler artifact")
    first_marker = json.loads((first_package / ".lean-runtime-package.json").read_text())
    assert first_marker["artifact_key"]["schema"] == "lean-runtime-package-artifact-key/2"

    toolchains.executable_digests["lean"] = "sha256:" + "3" * 64
    second = runtime.prepare_shared_project(second_source)
    second_package = Path(json.loads(second.overrides_file.read_text())["packages"][0]["dir"])
    second_marker = json.loads((second_package / ".lean-runtime-package.json").read_text())

    assert second.workspace_id != first.workspace_id
    assert second_package != first_package
    assert not (second_package / ".lake" / "build").exists()
    assert second_marker["artifact_key"]["lean_executable_digest"] == "sha256:" + "3" * 64
    assert "profile" not in second_marker["artifact_key"]


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
    marker["artifact_key"]["schema"] = "lean-runtime-package-artifact-key/1"
    marker["package"]["url"] = str(marker["package"]["url"]).removesuffix(".git")
    marker["package"]["scope"] = "legacy-cosmetic-scope"
    legacy_id = sha256_id("project_package", marker)
    legacy = runtime.home / "project-packages" / legacy_id
    shutil.copytree(first_override, legacy)
    legacy_artifact = legacy / ".lake" / "build" / "lib" / "lean" / "Dep.olean"
    legacy_artifact.parent.mkdir(parents=True)
    legacy_artifact.write_bytes(b"legacy artifact")
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
    assert second_override != legacy
    assert second.package_ids != (legacy_id,)
    assert not (second_override / ".lake" / "build").exists()


def test_sparse_environment_artifacts_without_build_identity_are_not_grafted(
    tmp_path: Path,
) -> None:
    first_source, _revision = _shared_project(tmp_path / "first", tmp_path / "dependency")
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
    )
    runtime.prepare_shared_project(first_source)
    remove_tree(runtime.home / "project-packages")
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
    assert not (package / ".lake" / "build" / "lib" / "lean" / "Dep.olean").exists()


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


def test_project_source_digest_ignores_unrelated_files(tmp_path: Path) -> None:
    from lean_runtime.projects import discover_project

    source = _project(tmp_path / "project")
    (tmp_path / "project" / "lakefile.toml").write_text(
        'name = "sample"\n[[lean_lib]]\nname = "Sample"\n'
    )
    context = discover_project(source)
    assert context.source_roots() == (((tmp_path / "project").resolve(), ("Sample",)),)
    original = context.provenance().workspace_digest
    assert original.startswith("sha256:")

    # Data, tooling, and nested unrelated projects beside the lakefile are not sources.
    (tmp_path / "project" / "README.md").write_text("notes\n")
    (tmp_path / "project" / "data").mkdir()
    (tmp_path / "project" / "data" / "blob.bin").write_bytes(b"\0" * 4096)
    (tmp_path / "project" / "venv" / "lib").mkdir(parents=True)
    (tmp_path / "project" / "venv" / "lib" / "site.py").write_text("x = 1\n")
    (tmp_path / "project" / "Other").mkdir()
    (tmp_path / "project" / "Other" / "Unowned.lean").write_text("example : True := trivial\n")
    assert context.provenance().workspace_digest == original

    # Owned modules, sibling modules, and configuration do change it.
    (tmp_path / "project" / "Sample" / "Extra.lean").write_text("example : True := trivial\n")
    with_module = context.provenance().workspace_digest
    assert with_module != original
    (tmp_path / "project" / "Sample.lean").write_text("import Sample.Main\n")
    with_root_module = context.provenance().workspace_digest
    assert with_root_module != with_module
    source.write_text("example : 1 = 1 := rfl\n")
    with_edit = context.provenance().workspace_digest
    assert with_edit != with_root_module
    (tmp_path / "project" / "lean-toolchain").write_text("leanprover/lean4:v4.33.0\n")
    with_toolchain = context.provenance().workspace_digest
    assert with_toolchain != with_edit
    (tmp_path / "project" / "lakefile.toml").write_text('name = "sample"\nversion = "0.2.0"\n')
    assert context.provenance().workspace_digest != with_toolchain


def test_project_source_digest_walks_whole_tree_without_declared_targets(
    tmp_path: Path,
) -> None:
    from lean_runtime.projects import discover_project

    root = tmp_path / "project"
    root.mkdir()
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.32.0\n")
    (root / "lakefile.lean").write_text("import Lake\nopen Lake DSL\npackage sample\n")
    (root / "Sample").mkdir()
    (root / "Sample" / "Main.lean").write_text("example : True := trivial\n")
    context = discover_project(root / "Sample" / "Main.lean")
    assert context.source_roots() is None
    original = context.provenance().workspace_digest

    (root / "data").mkdir()
    (root / "data" / "blob.bin").write_bytes(b"\0" * 4096)
    (root / ".lake").mkdir()
    (root / ".lake" / "Ignored.lean").write_text("example : True := trivial\n")
    assert context.provenance().workspace_digest == original

    (root / "Anywhere").mkdir()
    (root / "Anywhere" / "Module.lean").write_text("example : True := trivial\n")
    assert context.provenance().workspace_digest != original


def test_project_source_digest_reuses_clean_git_blob_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _project(tmp_path / "project")
    subprocess.run(git_command("init", "--quiet", str(tmp_path / "project")), check=True)
    subprocess.run(git_command("-C", str(tmp_path / "project"), "add", "."), check=True)
    subprocess.run(
        git_command(
            "-C",
            str(tmp_path / "project"),
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
    context = discover_project(source)
    cache = tmp_path / "runtime" / "project-fingerprints" / "project.json"
    original = context.source_digest(blob_cache_path=cache)
    original_open = Path.open

    def guarded_open(path: Path, *args: object, **kwargs: object):
        if path == source and args and args[0] == "rb":
            raise AssertionError("unchanged source content was read again")
        return original_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", guarded_open)

    assert context.source_digest(blob_cache_path=cache) == original


def test_opaque_git_project_uses_git_source_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.32.0\n")
    (root / "lakefile.lean").write_text("import Lake\nopen Lake DSL\npackage sample\n")
    source = root / "Sample.lean"
    source.write_text("example : True := trivial\n")
    subprocess.run(git_command("init", "--quiet", str(root)), check=True)
    context = discover_project(source)

    def reject_walk(*_args: object, **_kwargs: object):
        raise AssertionError("opaque Git project was recursively walked in Python")

    monkeypatch.setattr("lean_runtime.projects.os.walk", reject_walk)

    assert context.source_digest().startswith("sha256:")


def test_project_source_roots_include_globs_and_exe_roots(tmp_path: Path) -> None:
    from lean_runtime.projects import discover_project

    root = tmp_path / "project"
    root.mkdir()
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.32.0\n")
    (root / "lakefile.toml").write_text(
        'name = "sample"\n'
        "[[lean_lib]]\n"
        'name = "Sample"\n'
        'srcDir = "src"\n'
        'globs = ["Extra.*"]\n'
        "[[lean_exe]]\n"
        'name = "tool"\n'
        'root = "Tool.Main"\n'
    )
    (root / "src" / "Sample").mkdir(parents=True)
    (root / "src" / "Sample" / "Main.lean").write_text("example : True := trivial\n")
    context = discover_project(root / "src" / "Sample" / "Main.lean")
    roots = context.source_roots()
    assert roots is not None
    assert set(roots) == {
        ((root / "src").resolve(), ("Sample",)),
        ((root / "src").resolve(), ("Extra",)),
        (root.resolve(), ("Tool", "Main")),
    }
    assert context.owns_file(root / "src" / "Extra" / "Thing.lean") is True
    assert context.owns_file(root / "Tool" / "Main" / "Impl.lean") is True
    assert context.owns_file(root / "src" / "Stray.lean") is False


def test_project_check_announces_fingerprinting(tmp_path: Path) -> None:
    source = _project(tmp_path / "project")
    events: list[RuntimeEvent] = []
    runtime = Runtime(
        toolchains=ProjectToolchains(tmp_path / "runtime"),
        libraries=[],  # type: ignore[arg-type]
        on_event=events.append,
    )
    result = runtime.project(source).check_file(source)
    assert result.ok
    kinds = [event.kind for event in events]
    started = kinds.index("project.fingerprint_started")
    finished = kinds.index("project.fingerprint_finished")
    assert started < finished
    assert events[started].data["root"] == str(runtime.project(source).root)
    assert isinstance(events[finished].data["elapsed_ms"], int)
