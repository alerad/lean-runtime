"""Structured verification of locks and published environments."""

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
from .lake import ROOT_MODULE
from .lockfiles import EnvironmentLock
from .policies import ExecutionPolicy
from .store import EnvironmentStore, environment_identity, platform_compatibility
from .toolchains import ToolchainManager

VERIFY_SCHEMA = "lean-runtime.verify/v1"


@dataclass(frozen=True, slots=True)
class _ArtifactInventory:
    digest: str
    entries: int
    bytes: int


def _artifact_inventory(workspace: Path) -> _ArtifactInventory:
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
    return _ArtifactInventory("sha256:" + digest.hexdigest(), entries, total)


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
            "environment verification probe failed: " + (result.stdout + result.stderr)[-2000:]
        )


def _rebuild_inventory(runtime: Any, environment: Environment) -> tuple[str, str]:
    runtime.events.emit(
        "verification.rebuild_started",
        "Rebuilding exact lock from source",
        environment_id=environment.id,
    )
    original = _artifact_inventory(environment.root / "workspace")
    with tempfile.TemporaryDirectory(prefix="lean-runtime-verify-") as temporary:
        store = EnvironmentStore(Path(temporary))
        manager = EnvironmentManager(store, runtime.toolchains, runtime.backend, runtime.events)
        rebuilt = manager.ensure(environment.lock)
        _verify_sources(rebuilt)
        _probe(rebuilt, runtime.toolchains, runtime.backend)
        rebuilt_inventory = _artifact_inventory(rebuilt.root / "workspace")
    return original.digest, rebuilt_inventory.digest


@dataclass(frozen=True, slots=True)
class VerificationCheck:
    code: str
    ok: bool
    subject: str | None = None
    details: dict[str, Any] | None = None
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "ok": self.ok,
            "subject": self.subject,
            "details": self.details or {},
            "skipped": self.skipped,
        }


@dataclass(frozen=True, slots=True)
class VerificationReport:
    subject: str
    subject_kind: str
    checks: tuple[VerificationCheck, ...]
    failures: tuple[VerificationCheck, ...]
    warnings: tuple[VerificationCheck, ...]
    lock_id: str
    environment_id: str | None
    artifact_match: bool | None = None

    @property
    def ok(self) -> bool:
        return not self.failures

    def raise_for_error(self) -> VerificationReport:
        if self.failures:
            failure = self.failures[0]
            detail = (failure.details or {}).get("message", failure.code)
            raise EnvironmentError(f"verification failed: {detail}")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "subject_kind": self.subject_kind,
            "ok": self.ok,
            "checks": [item.to_dict() for item in self.checks],
            "failures": [item.to_dict() for item in self.failures],
            "warnings": [item.to_dict() for item in self.warnings],
            "lock_id": self.lock_id,
            "environment_id": self.environment_id,
            "artifact_match": self.artifact_match,
        }


def verify_lock(lock: EnvironmentLock, *, subject: str) -> VerificationReport:
    checks = (
        VerificationCheck("lock_schema_valid", True),
        VerificationCheck("lock_identity_verified", True, details={"lock_id": lock.lock_id}),
        VerificationCheck("package_names_unique", True, details={"packages": len(lock.packages)}),
        VerificationCheck("package_paths_safe", True),
        VerificationCheck("source_acquisition", True, skipped=True),
    )
    return VerificationReport(subject, "lock", checks, (), (), lock.lock_id, None)


def verify_environment(
    runtime: Any,
    environment: Environment,
    *,
    rebuild: bool,
    offline: bool = False,
) -> VerificationReport:
    checks: list[VerificationCheck] = [
        VerificationCheck("alias_resolved", True, subject=environment.id),
        VerificationCheck(
            "lock_identity_verified", True, details={"lock_id": environment.lock.lock_id}
        ),
    ]
    failures: list[VerificationCheck] = []
    warnings: list[VerificationCheck] = []
    expected = environment_identity(environment.lock, str(environment._record["build_profile"]))
    identity = VerificationCheck(
        "environment_identity_verified",
        expected == environment.id,
        details={"expected": expected, "observed": environment.id},
    )
    checks.append(identity)
    if not identity.ok:
        failures.append(identity)
    expected_platform = platform_compatibility()
    observed_platform = environment._record.get("platform_compatibility", expected_platform)
    platform = VerificationCheck(
        "platform_compatibility_verified",
        observed_platform == expected_platform,
        details={"expected": expected_platform, "observed": observed_platform},
    )
    checks.append(platform)
    if not platform.ok:
        failures.append(platform)
    try:
        _verify_sources(environment)
        source = VerificationCheck(
            "package_trees_verified", True, details={"packages": len(environment.lock.packages)}
        )
    except EnvironmentError as exc:
        source = VerificationCheck("source_tree_mismatch", False, details={"message": str(exc)})
        failures.append(source)
    checks.append(source)
    try:
        if offline and not runtime.toolchains.is_installed(environment.lock.toolchain):
            raise EnvironmentError(
                "offline verification requires the locked toolchain to be installed"
            )
        _probe(environment, runtime.toolchains, runtime.backend)
        probe = VerificationCheck("lean_probe_passed", True)
    except EnvironmentError as exc:
        probe = VerificationCheck("probe_failed", False, details={"message": str(exc)})
        failures.append(probe)
    checks.append(probe)
    if offline:
        checks.append(
            VerificationCheck(
                "offline_retained_state_verified",
                probe.ok and source.ok,
                details={"acquisition_forbidden": True},
            )
        )
    artifact_match: bool | None = None
    if rebuild and not failures:
        original_digest, rebuilt_digest = _rebuild_inventory(runtime, environment)
        artifact_match = original_digest == rebuilt_digest
        checks.append(VerificationCheck("independent_rebuild_passed", True))
        artifact = VerificationCheck(
            "artifact_inventory_match",
            artifact_match is True,
            details={
                "original": original_digest,
                "rebuilt": rebuilt_digest,
            },
        )
        checks.append(artifact)
        if not artifact.ok:
            warnings.append(artifact)
    elif rebuild:
        checks.append(VerificationCheck("independent_rebuild", True, skipped=True))
    return VerificationReport(
        environment.id,
        "environment",
        tuple(checks),
        tuple(failures),
        tuple(warnings),
        environment.lock.lock_id,
        environment.id,
        artifact_match,
    )


def load_lock_subject(path: Path) -> VerificationReport:
    return verify_lock(EnvironmentLock.load(path), subject=str(path))
