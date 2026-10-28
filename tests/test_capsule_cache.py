from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from lean_runtime import Runtime
from lean_runtime.capsules import CAPSULE_MANIFEST, build_manifest
from lean_runtime.environments import (
    _forget_verified_closures,
    _load_capsule,
    _reports_missing_artifact,
)
from lean_runtime.errors import EnvironmentError

from test_runtime import FakeToolchains


def _sparse_environment(tmp_path: Path):
    runtime = Runtime(
        toolchains=FakeToolchains(tmp_path),  # type: ignore[arg-type]
        libraries=[],
        availability="local",
    )
    environment = runtime.open_toolchain("v4.32.2")
    workspace = environment.workspace
    build = workspace / ".lake" / "packages" / "sample" / ".lake" / "build" / "lib" / "lean"
    for suffix in (".olean", ".ir", ".olean.server", ".olean.private"):
        path = build / f"A{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(suffix.encode())
    manifest = build_manifest(
        workspace=workspace,
        environment_id="env_test",
        lock_id=environment.lock.lock_id,
        toolchain=environment.lock.toolchain,
        build_roots={"sample": build},
        imports={"A": ()},
    )
    capsule = workspace / CAPSULE_MANIFEST
    capsule.parent.mkdir(parents=True, exist_ok=True)
    capsule.write_text(json.dumps(manifest.to_dict()))
    return environment, build / "A.olean"


def test_manifest_is_parsed_once_per_file_identity(tmp_path: Path) -> None:
    environment, _ = _sparse_environment(tmp_path)
    capsule = environment.workspace / CAPSULE_MANIFEST
    first = _load_capsule(capsule)[1]
    assert _load_capsule(capsule)[1] is first
    # A rewritten manifest (new identity) is parsed again, never served stale.
    stat = capsule.stat()
    capsule.write_text(capsule.read_text() + "\n")
    os.utime(capsule, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert _load_capsule(capsule)[1] is not first


def test_sparse_verification_is_cached_and_forgettable(tmp_path: Path) -> None:
    environment, olean = _sparse_environment(tmp_path)
    manager = environment.manager
    check = frozenset({"check"})
    manager.ensure_sparse_modules(environment.lock, ("A",), check, allow_acquisition=False)
    olean.unlink()
    # Cached: no per-artifact stat storm on the hot path.
    manager.ensure_sparse_modules(environment.lock, ("A",), check, allow_acquisition=False)
    assert not manager.sparse_closure_present(environment.lock, ("A",), check)
    assert _forget_verified_closures(environment.workspace)
    assert not _forget_verified_closures(environment.workspace)
    with pytest.raises(EnvironmentError, match="missing artifacts"):
        manager.ensure_sparse_modules(environment.lock, ("A",), check, allow_acquisition=False)


def _failed(message: str) -> SimpleNamespace:
    return SimpleNamespace(ok=False, stderr=message, stdout="", diagnostics=())


def test_missing_artifact_after_cached_verification_reacquires_and_retries(tmp_path: Path) -> None:
    environment, olean = _sparse_environment(tmp_path)
    manager = environment.manager
    restored: list[tuple[str, ...]] = []

    def acquire(_lock, roots, _capabilities, _cancel):
        restored.append(tuple(roots))
        olean.write_bytes(b".olean")

    manager.sparse_acquirer = acquire
    runs: list[int] = []

    def execute(**_kwargs):
        runs.append(1)
        if len(runs) == 1:
            return _failed(f"object file {olean} of module A does not exist")
        return SimpleNamespace(ok=True, stderr="", stdout="", diagnostics=())

    environment._execute_in_instance = execute  # type: ignore[method-assign]
    manager.ensure_sparse_modules(environment.lock, ("A",), frozenset({"check"}))
    olean.unlink()
    result = environment.check("import A\n", _declaration_hints=False)
    assert result.ok and len(runs) == 2 and restored == [("A",)]


def test_genuine_missing_module_does_not_retry(tmp_path: Path) -> None:
    environment, _ = _sparse_environment(tmp_path)
    runs: list[int] = []

    def execute(**_kwargs):
        runs.append(1)
        return _failed("unknown module prefix 'Typo'")

    environment._execute_in_instance = execute  # type: ignore[method-assign]
    environment.check("import A\n", _declaration_hints=False)
    environment.check("import A\nimport Typo\n", _declaration_hints=False)
    assert len(runs) == 2


def test_missing_artifact_messages_are_recognized() -> None:
    assert _reports_missing_artifact(_failed("object file /x/A.olean of module A does not exist"))
    assert not _reports_missing_artifact(_failed("type mismatch"))
