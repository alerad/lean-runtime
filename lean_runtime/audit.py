"""Verification and independent rebuild audits for managed environments."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .backends import Backend
from .bundles import _packages_directory, _verify_package, _verify_workspace_lock
from .environments import Environment, EnvironmentManager
from .errors import EnvironmentError
from .events import EventEmitter
from .lake import ROOT_MODULE
from .policies import ExecutionPolicy
from .store import EnvironmentStore, platform_compatibility
from .toolchains import ToolchainManager

AUDIT_SCHEMA = "lean-runtime-audit/1"


@dataclass(frozen=True, slots=True)
class ArtifactInventory:
    digest: str
    entries: int
    bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {"digest": self.digest, "entries": self.entries, "bytes": self.bytes}


@dataclass(frozen=True, slots=True)
class AuditReport:
    environment_id: str
    lock_id: str
    source_verified: bool
    probe_passed: bool
    artifacts: ArtifactInventory
    rebuilt_artifacts: ArtifactInventory | None = None
    artifact_match: bool | None = None

    @property
    def ok(self) -> bool:
        return self.source_verified and self.probe_passed

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": AUDIT_SCHEMA,
            "environment_id": self.environment_id,
            "lock_id": self.lock_id,
            "source_verified": self.source_verified,
            "probe_passed": self.probe_passed,
            "artifacts": self.artifacts.to_dict(),
            "rebuilt_artifacts": (
                self.rebuilt_artifacts.to_dict() if self.rebuilt_artifacts is not None else None
            ),
            "artifact_match": self.artifact_match,
            "platform_compatibility": platform_compatibility(),
        }


def artifact_inventory(workspace: Path) -> ArtifactInventory:
    """Hash only Lake build outputs using stable workspace-relative paths."""
    roots = [path for path in workspace.rglob(".lake/build") if path.is_dir()]
    digest = hashlib.sha256()
    entries = 0
    total = 0
    for root in sorted(roots, key=lambda path: path.relative_to(workspace).as_posix()):
        for path in sorted(
            root.rglob("*"), key=lambda item: item.relative_to(workspace).as_posix()
        ):
            relative = path.relative_to(workspace).as_posix()
            stat = path.lstat()
            if path.is_symlink():
                digest.update(b"link\0" + relative.encode() + b"\0" + os.readlink(path).encode())
                entries += 1
            elif path.is_file():
                digest.update(
                    b"file\0"
                    + relative.encode()
                    + b"\0"
                    + str(stat.st_mode & 0o111).encode()
                    + b"\0"
                )
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                entries += 1
                total += stat.st_size
    return ArtifactInventory("sha256:" + digest.hexdigest(), entries, total)


def _verify_sources(environment: Environment) -> None:
    workspace = environment.root / "workspace"
    _verify_workspace_lock(workspace, environment.lock)
    packages = workspace.joinpath(*_packages_directory(environment.lock).parts)
    for package in environment.lock.packages:
        _verify_package(packages / package.name, package)


def _probe(environment: Environment, toolchains: ToolchainManager, backend: Backend) -> None:
    command = toolchains.command(
        environment.lock.toolchain, "lake", "env", "lean", f"{ROOT_MODULE}.lean"
    )
    result = backend.execute(
        command,
        cwd=environment.root / "workspace",
        environment=toolchains.environment,
        policy=ExecutionPolicy(timeout_seconds=300, max_output_bytes=2_000_000),
    )
    if result.exit_code:
        raise EnvironmentError(
            "environment audit probe failed: " + (result.stdout + result.stderr)[-2000:]
        )


def audit_environment(
    environment: Environment,
    toolchains: ToolchainManager,
    backend: Backend,
    events: EventEmitter,
    *,
    rebuild: bool = False,
) -> AuditReport:
    events.emit("audit.started", "Auditing environment", environment_id=environment.id)
    _verify_sources(environment)
    _probe(environment, toolchains, backend)
    original = artifact_inventory(environment.root / "workspace")
    rebuilt: ArtifactInventory | None = None
    if rebuild:
        events.emit("audit.rebuild_started", "Rebuilding exact lock from source")
        with tempfile.TemporaryDirectory(prefix="lean-runtime-audit-") as temporary:
            store = EnvironmentStore(Path(temporary))
            manager = EnvironmentManager(store, toolchains, backend, events)
            rebuilt_environment = manager.ensure(environment.lock)
            _verify_sources(rebuilt_environment)
            _probe(rebuilt_environment, toolchains, backend)
            rebuilt = artifact_inventory(rebuilt_environment.root / "workspace")
    report = AuditReport(
        environment.id,
        environment.lock.lock_id,
        True,
        True,
        original,
        rebuilt,
        original.digest == rebuilt.digest if rebuilt is not None else None,
    )
    events.emit(
        "audit.completed",
        "Environment audit completed",
        environment_id=environment.id,
        artifact_match=report.artifact_match,
    )
    return report
