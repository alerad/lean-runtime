from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from lean_runtime import (
    Runtime,
    StorageLimitExceeded,
    StorageLimits,
    ToolchainError,
    ToolchainManager,
)
from lean_runtime.dependency_views import dependency_view, workspace_overrides
from lean_runtime.errors import DownloadLimitExceeded, ProjectError
from lean_runtime.policies import ExecutionPolicy
from lean_runtime.storage_limits import StorageGuard, TransferBudget, owned_bytes
from lean_runtime.toolchain_slim import SlimManifest

TC = "leanprover/lean4:v4.33.0"


def project(path: Path, entries: list[dict] | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "lean-toolchain").write_text(TC + "\n")
    (path / "lakefile.toml").write_text('name = "test"\n')
    (path / "lake-manifest.json").write_text(
        json.dumps({"version": "1.1.0", "packages": entries or []})
    )
    return path


def full(manager: ToolchainManager, base: Path | None = None) -> Path:
    root = (
        base / "toolchains" / manager._toolchain_dir_name(TC)
        if base
        else manager._elan_toolchain_dir(TC)
    )
    (root / "bin").mkdir(parents=True)
    for name in ("lean", "lake"):
        binary = manager._binary(root, name)
        binary.write_text("binary")
    return root


def slim(manager: ToolchainManager) -> Path:
    root = manager.slim_path(TC)
    (root / "bin").mkdir(parents=True)
    manager._binary(root, "lean").write_text("binary")
    manifest = SlimManifest(TC, "check", 1, 6, 0, 0, "2026-10-17")
    (root / "slim-manifest.json").write_text(json.dumps(manifest.to_dict()))
    return root


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ToolchainManager:
    monkeypatch.setattr(ToolchainManager, "_user_elan_home", lambda self: None)
    return ToolchainManager(tmp_path / "runtime", allow_download=False)


@pytest.mark.parametrize(
    "operation", ["ensure", "ensure_full", "command", "bootstrap_elan", "full_path"]
)
def test_local_policy_cannot_acquire(
    manager: ToolchainManager, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("attempted installation or network access")

    monkeypatch.setattr(manager, "_install_with_elan", forbidden)
    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    monkeypatch.setattr(manager, "remote_ensure", forbidden)
    with pytest.raises(ToolchainError):
        if operation == "command":
            manager.command(TC, "lake", "build")
        elif operation == "bootstrap_elan":
            manager.bootstrap_elan()
        else:
            getattr(manager, operation)(TC)
    assert not manager.home.exists()


def test_runtime_binds_local_policy_through_low_level(manager: ToolchainManager) -> None:
    manager.allow_download = True
    runtime = Runtime(toolchains=manager, libraries=[], availability="local")
    assert not runtime.toolchains.allow_download
    with pytest.raises(ToolchainError):
        runtime.toolchains.command(TC, "lake", "build")


def test_slim_is_not_build_ready(manager: ToolchainManager) -> None:
    root = slim(manager)
    assert manager.is_available_locally(TC)
    assert not manager.is_build_available_locally(TC)
    assert manager.command(TC, "lean") == [str(manager._binary(root, "lean"))]
    with pytest.raises(ToolchainError):
        manager.command(TC, "lake", "build")


def test_borrowed_root_with_private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "private-home"))
    monkeypatch.setenv("LEAN_RUNTIME_ELAN_HOME", str(tmp_path / "explicit-elan"))
    root = tmp_path / "host-elan"
    manager = ToolchainManager(
        tmp_path / "runtime", allow_download=False, borrowed_elan_roots=(root,)
    )
    installation = full(manager, root)
    assert manager.local_capabilities(TC)["ownership"] == "borrowed"
    assert manager.is_build_available_locally(TC)
    assert TC in manager.available_toolchains()
    assert manager.command(TC, "lake") == [str(manager._binary(installation, "lake"))]
    slim(manager)
    with pytest.raises(ToolchainError, match="user-managed"):
        manager.prune_original(TC)
    assert installation.is_dir()
    assert not manager.elan_home.exists()


def test_missing_capabilities_are_read_only(manager: ToolchainManager) -> None:
    assert not manager.local_capabilities(TC)["check_ready"]
    assert not manager.local_capabilities(TC)["build_ready"]
    assert not manager.home.exists()


@pytest.mark.parametrize("budget", ["download", "toolchain"])
def test_unknown_full_acquisition_refused_before_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, budget: str
) -> None:
    manager = ToolchainManager(tmp_path / "runtime")
    monkeypatch.setattr(manager, "_user_elan_home", lambda: None)
    if budget == "download":
        manager.max_download_bytes = 100
    else:
        manager.storage_limits = StorageLimits(toolchain_bytes=100)
    with pytest.raises(DownloadLimitExceeded, match="unknown"):
        manager.ensure_full(TC)
    assert not manager.home.exists()


def test_readiness_is_read_only_and_distinguishes_capabilities(
    manager: ToolchainManager, tmp_path: Path
) -> None:
    slim(manager)
    path = project(tmp_path / "project")
    before = sorted(str(item.relative_to(tmp_path)) for item in tmp_path.rglob("*"))
    runtime = Runtime(
        toolchains=manager, libraries=[], availability="local", initialize_store=False
    )
    report = runtime.readiness([path])
    assert report["verdict"] is None
    assert report["projects"][0]["blockers"][0]["code"] == "missing_build_toolchain"
    assert not report["inputs_ready"]
    assert sorted(str(item.relative_to(tmp_path)) for item in tmp_path.rglob("*")) == before


def test_readiness_path_conflicts_and_missing_pins(
    manager: ToolchainManager, tmp_path: Path
) -> None:
    full(manager)
    dependency = project(tmp_path / "dep")
    (dependency / "lean-toolchain").write_text("leanprover/lean4:v4.20.0")
    root = project(
        tmp_path / "root",
        [
            {"type": "path", "name": "dep", "dir": "../dep"},
            {"type": "path", "name": "missing", "dir": "../absent"},
            {
                "type": "git",
                "name": "mathlib",
                "rev": "a" * 40,
                "url": "https://example.invalid/mathlib",
            },
        ],
    )
    runtime = Runtime(toolchains=manager, libraries=[], availability="local")
    result = runtime.readiness([root])["projects"][0]
    assert {item["code"] for item in result["blockers"]} == {
        "path_toolchain_conflict",
        "missing_path_dependency",
        "missing_exact_dependency",
    }


def test_readiness_pool_conflicts_are_not_merged(manager: ToolchainManager, tmp_path: Path) -> None:
    full(manager)
    for name, rev in (("one", "a" * 40), ("two", "b" * 40)):
        project(
            tmp_path / name,
            [{"type": "git", "name": "dep", "rev": rev, "url": "https://example.invalid/dep"}],
        )
    runtime = Runtime(toolchains=manager, libraries=[], availability="local")
    result = runtime.readiness([tmp_path], recursive=True)
    assert len(result["projects"]) == 2
    assert len(result["pool_conflicts"]) == 1
    assert len(result["pool_conflicts"][0]["variants"]) == 2


def test_local_shared_fetch_is_refused(manager: ToolchainManager) -> None:
    runtime = Runtime(toolchains=manager, libraries=[], availability="local")
    with pytest.raises(ProjectError, match="unavailable locally"):
        runtime.shared_projects._source_checkout(
            url="https://example.invalid/dep", revision="a" * 40, seed=None
        )


def test_budgeted_git_fetch_unknown(manager: ToolchainManager) -> None:
    runtime = Runtime(toolchains=manager, libraries=[], max_download_bytes=1)
    with pytest.raises(DownloadLimitExceeded, match="unknown"):
        runtime.shared_projects._source_checkout(
            url="https://example.invalid/dep", revision="a" * 40, seed=None
        )


def test_view_borrows_outputs_but_owns_config(tmp_path: Path) -> None:
    source = project(tmp_path / "dep")
    (source / "Dep.lean").write_text("def value := 1")
    (source / ".lake" / "build").mkdir(parents=True)
    (source / ".lake" / "build" / "Dep.olean").write_bytes(b"compiled")
    view = dependency_view(source, tmp_path / "views")
    assert view.resolve() != source.resolve()
    assert (view / "Dep.lean").samefile(source / "Dep.lean")
    assert (view / ".lake" / "build" / "Dep.olean").samefile(
        source / ".lake" / "build" / "Dep.olean"
    )
    (view / ".lake" / "config").mkdir()
    (view / ".lake" / "config" / "lakefile.olean.lock").touch()
    assert not (source / ".lake" / "config").exists()
    assert dependency_view(source, tmp_path / "views") == view
    (source / "New.lean").write_text("def new := 2")
    assert dependency_view(source, tmp_path / "views") != view


def test_view_overrides_leave_original_unchanged(tmp_path: Path) -> None:
    source = project(tmp_path / "dep")
    manifest = tmp_path / "overrides.json"
    original = {
        "version": "1.1.0",
        "packages": [{"name": "dep", "type": "path", "dir": str(source)}],
    }
    manifest.write_text(json.dumps(original))
    output = workspace_overrides(manifest, tmp_path / "views")
    assert json.loads(manifest.read_text()) == original
    assert Path(json.loads(output.read_text())["packages"][0]["dir"]).is_dir()


def test_storage_deduplicates_hardlinks_and_ignores_borrowed_targets(tmp_path: Path) -> None:
    owned = tmp_path / "owned"
    owned.mkdir()
    (owned / "one").write_bytes(b"123")
    os.link(owned / "one", owned / "two")
    external = tmp_path / "external"
    external.mkdir()
    (external / "big").write_bytes(b"x" * 1000)
    from lean_runtime._paths import link_directory

    link_directory(external, owned / "link")
    assert owned_bytes([owned, owned / "one"]) == 3


def test_storage_guard_failure_is_not_caller_cancellation(tmp_path: Path) -> None:
    caller = threading.Event()
    with (
        pytest.raises(StorageLimitExceeded) as error,
        StorageGuard("build", [tmp_path], 2, caller) as guard,
    ):
        (tmp_path / "file").write_bytes(b"123")
    assert error.value.reason_code == "storage_limit_exceeded"
    assert guard.cancel.is_set()
    assert not caller.is_set()


def test_storage_guard_stops_process(manager: ToolchainManager, tmp_path: Path) -> None:
    runtime = Runtime(
        toolchains=manager, libraries=[], storage_limits=StorageLimits(build_bytes=10)
    )
    started = time.monotonic()
    with pytest.raises(StorageLimitExceeded):
        runtime._raw_result(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import time; "
                "Path('output').write_bytes(b'x'*100); time.sleep(30)",
            ],
            cwd=tmp_path,
            toolchain=TC,
            source_digest="",
            policy=ExecutionPolicy(timeout_seconds=10),
        )
    assert time.monotonic() - started < 5


def test_transfer_budget_counts_retries_and_cached_bytes_cost_zero() -> None:
    budget = TransferBudget(100)
    budget.require(60, consume=True)
    budget.require(40)
    with pytest.raises(DownloadLimitExceeded):
        budget.require(41)
    assert budget.used == 60


@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_invalid_storage_limit(value: int) -> None:
    with pytest.raises(ValueError):
        StorageLimits(build_bytes=value)


@pytest.mark.integration
def test_real_local_shared_build_and_warm_reuse(tmp_path: Path) -> None:
    """No network: use an existing full toolchain and a tiny local Git fixture."""
    import subprocess

    probe = ToolchainManager(tmp_path / "probe", allow_download=False)
    toolchain = next(
        (name for name in probe.available_toolchains() if probe.is_build_available_locally(name)),
        None,
    )
    if toolchain is None:
        pytest.skip("no full local Lean/Lake toolchain")
    dependency = project(tmp_path / "dependency")
    (dependency / "lean-toolchain").write_text(toolchain)
    (dependency / "lakefile.toml").write_text('name = "borrowed-dep"\n[[lean_lib]]\nname = "Dep"\n')
    (dependency / "Dep.lean").write_text("def borrowedValue : Nat := 7\n")
    subprocess.run(["git", "init", "-q", str(dependency)], check=True)
    subprocess.run(["git", "-C", str(dependency), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(dependency),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    revision = subprocess.check_output(
        ["git", "-C", str(dependency), "rev-parse", "HEAD"], text=True
    ).strip()
    entries = [
        {
            "type": "git",
            "name": "«borrowed-dep»",
            "url": str(dependency),
            "rev": revision,
            "inputRev": revision,
            "inherited": False,
            "configFile": "lakefile.toml",
        }
    ]
    first = project(tmp_path / "first", entries)
    (first / "lean-toolchain").write_text(toolchain)
    (first / "lakefile.toml").write_text(
        'name = "consumer"\ndefaultTargets = ["Main"]\n'
        '[[require]]\nname = "borrowed-dep"\ngit = '
        + json.dumps(str(dependency))
        + "\nrev = "
        + json.dumps(revision)
        + '\n[[lean_lib]]\nname = "Main"\n'
    )
    (first / "Main.lean").write_text("import Dep\nexample : borrowedValue = 7 := rfl\n")
    local = first / ".lake" / "packages" / "«borrowed-dep»"
    local.parent.mkdir(parents=True)
    local.symlink_to(dependency, target_is_directory=True)
    runtime = Runtime(
        home=tmp_path / "runtime",
        libraries=[],
        availability="local",
        storage_limits=StorageLimits(build_bytes=10_000_000),
    )
    result = runtime.build(first, shared=True)
    assert result.ok, result.stdout + result.stderr
    workspace = runtime.prepare_shared_project(first)
    source = Path(json.loads(workspace.overrides_file.read_text())["packages"][0]["dir"])
    outputs = list((source / ".lake" / "build").rglob("Dep.olean"))
    assert outputs, "first build must publish output in shared package, not private view"
    stamp = outputs[0].stat().st_mtime_ns
    assert not (source / ".lake" / "config").exists()
    second = project(tmp_path / "second", entries)
    (second / "lean-toolchain").write_text(toolchain)
    (second / "lakefile.toml").write_text((first / "lakefile.toml").read_text())
    (second / "Main.lean").write_text((first / "Main.lean").read_text())
    second_result = runtime.build(second, shared=True)
    assert second_result.ok, second_result.stdout + second_result.stderr
    assert outputs[0].stat().st_mtime_ns == stamp


def test_known_clone_budget_refuses_before_copy(tmp_path: Path) -> None:
    from lean_runtime.store import clone_tree

    source = tmp_path / "source"
    source.mkdir()
    (source / "large").write_bytes(b"x" * 100)
    owned = tmp_path / "owned"
    with pytest.raises(StorageLimitExceeded), StorageGuard("dependency", [owned], 1):
        clone_tree(source, owned / "copy")
    assert not owned.exists()


def test_readiness_cli_does_not_create_runtime_home(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from lean_runtime.cli import main

    root = project(tmp_path / "project")
    home = tmp_path / "empty-runtime"
    assert main(["--home", str(home), "readiness", str(root), "--json"]) in {0, 2}
    report = json.loads(capsys.readouterr().out)
    assert report["verdict"] is None
    assert not home.exists()


def test_oci_transfer_budget_is_aggregate_and_deduplicates_cached_blobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import io

    from lean_runtime.events import EventEmitter
    from lean_runtime.oci import OCIRegistryClient, OCIRepository
    from lean_runtime.storage_limits import download_budgeted
    from lean_runtime.store import EnvironmentStore

    store = EnvironmentStore(tmp_path / "cache")
    client = OCIRegistryClient(OCIRepository.parse("oci://example.invalid/test/library"))
    requests = []
    blobs = [b"abc", b"def"]
    descriptors = [
        {"digest": "sha256:" + hashlib.sha256(blob).hexdigest(), "size": len(blob)}
        for blob in blobs
    ]

    def request(req):
        requests.append(req)
        return io.BytesIO(blobs[len(requests) - 1])

    monkeypatch.setattr(client, "_request", request)

    class Operation:
        max_download_bytes = 5

        @download_budgeted
        def run(self):
            first = client.download_blob(descriptors[0], store, EventEmitter())
            assert client.download_blob(descriptors[0], store, EventEmitter()) == first
            client.download_blob(descriptors[1], store, EventEmitter())

    with pytest.raises(DownloadLimitExceeded):
        Operation().run()
    assert len(requests) == 1


def test_local_resolution_and_reference_discovery_never_fetch(manager: ToolchainManager) -> None:
    from lean_runtime import EnvironmentSpec, GitPackage, ResolutionError, SpecificationError

    full(manager)
    runtime = Runtime(toolchains=manager, libraries=[], availability="local")
    with pytest.raises(ResolutionError, match="local-only"):
        runtime.prepare(
            EnvironmentSpec(TC, (GitPackage("dep", "https://example.invalid/dep", "a" * 40),))
        )
    with pytest.raises(SpecificationError, match="local-only"):
        runtime.spec_from_references(["github:example/dep"])


def test_cli_budget_flags_survive_context_translation() -> None:
    from lean_runtime.cli import _standalone_check_args, parser

    args = parser().parse_args(
        ["--max-download", "1MiB", "--max-build-bytes", "2MiB", "check", "Demo.lean"]
    )
    translated = _standalone_check_args(args, Path("Demo.lean"))
    assert translated.max_download == 1024**2
    assert translated.max_build_bytes == 2 * 1024**2


def test_public_build_budget_forwards_cancellation(tmp_path: Path) -> None:
    class Tools(ToolchainManager):
        def ensure_full(self, toolchain, **kwargs):
            return toolchain

        def command(self, toolchain, executable, *args):
            return [
                sys.executable,
                "-c",
                "from pathlib import Path; import time; "
                "Path('output').write_bytes(b'x'*100); time.sleep(30)",
            ]

        def environment_for(self, toolchain):
            return os.environ.copy()

    root = project(tmp_path / "project")
    runtime = Runtime(
        toolchains=Tools(tmp_path / "runtime"),
        libraries=[],
        storage_limits=StorageLimits(build_bytes=10),
    )
    caller = threading.Event()
    started = time.monotonic()
    with pytest.raises(StorageLimitExceeded):
        runtime.build(root, timeout=10, cancel=caller)
    assert time.monotonic() - started < 5
    assert not caller.is_set()


def test_environment_check_budget_stops_process(tmp_path: Path) -> None:
    from lean_runtime import EnvironmentSpec

    class Tools(ToolchainManager):
        def ensure(self, toolchain, **kwargs):
            return toolchain

        def command(self, toolchain, executable, *args):
            return [
                sys.executable,
                "-c",
                "from pathlib import Path; import time; "
                "Path('output').write_bytes(b'x'*1000); time.sleep(30)",
            ]

    runtime = Runtime(
        toolchains=Tools(tmp_path / "runtime"),
        libraries=[],
        storage_limits=StorageLimits(build_bytes=500),
    )
    lock = runtime.prepare(EnvironmentSpec(TC, ()))
    environment = runtime.open_exact(lock)
    start = time.monotonic()
    with pytest.raises(StorageLimitExceeded):
        environment.check("example : True := trivial", policy=ExecutionPolicy(timeout_seconds=10))
    assert time.monotonic() - start < 5
