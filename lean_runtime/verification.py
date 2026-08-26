"""Structured verification of locks and published environments."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bundles import _packages_directory, _verify_package, _verify_workspace_lock
from .capsules import CAPSULE_MANIFEST, CapsuleManifest
from .environments import Environment, EnvironmentManager
from .errors import EnvironmentError
from .events import current
from .lake import ROOT_MODULE
from .lockfiles import EnvironmentLock
from .policies import ExecutionPolicy
from .progress import CountedProgress
from .store import EnvironmentStore, environment_identity, platform_compatibility

VERIFY_SCHEMA = "lean-runtime.verify/v1"
ATTESTATION_SCHEMA = "lean-runtime.attestation/v1"
ATTESTATION_PREDICATE_TYPE = "https://lean-runtime.dev/attestation/environment/v1"


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
    paths = [
        path
        for root in sorted(roots, key=lambda path: path.relative_to(workspace).as_posix())
        for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(workspace).as_posix())
    ]
    progress = CountedProgress(
        current().emit,
        "verification.inventory",
        f"Hashing build artifacts in {workspace.parent.name}",
        len(paths),
        phase="verification",
    )
    for path in paths:
        relative = path.relative_to(workspace).as_posix()
        progress.advance(relative)
        stat = path.lstat()
        if path.is_symlink():
            digest.update(b"link\0" + relative.encode() + b"\0" + os.readlink(path).encode())
            entries += 1
        elif path.is_file():
            digest.update(
                b"file\0" + relative.encode() + b"\0" + str(stat.st_mode & 0o111).encode() + b"\0"
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


def _verify_capsule_state(environment: Environment) -> int:
    workspace = environment.root / "workspace"
    manifest = CapsuleManifest.load(workspace / CAPSULE_MANIFEST)
    if (
        manifest.environment_id != environment.id
        or manifest.lock_id != environment.lock.lock_id
        or manifest.toolchain != environment.lock.toolchain
    ):
        raise EnvironmentError("sparse capsule identity mismatch")
    origin = environment._record.get("origin")
    if not isinstance(origin, dict):
        raise EnvironmentError("sparse capsule has no acquisition provenance")
    modules = origin.get("modules")
    capabilities = origin.get("capabilities")
    if not isinstance(modules, list) or not isinstance(capabilities, list):
        raise EnvironmentError("sparse capsule provenance is incomplete")
    selected = frozenset(str(item) for item in capabilities)
    verified = 0
    for module in manifest.closure(str(item) for item in modules):
        for artifact in module.artifacts:
            if artifact.capability not in selected:
                continue
            path = workspace.joinpath(*Path(artifact.path).parts)
            if not path.is_file() or path.stat().st_size != artifact.size:
                raise EnvironmentError(f"sparse capsule artifact is missing: {artifact.path}")
            observed = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    observed.update(chunk)
            digest = observed.hexdigest()
            if "sha256:" + digest != artifact.digest:
                raise EnvironmentError(f"sparse capsule artifact changed: {artifact.path}")
            verified += 1
    return verified


def _probe(environment: Environment, *, offline: bool = False) -> None:
    """Probe the immutable compiled environment without asking Lake to resolve it."""

    if environment.sparse:
        origin = environment._record.get("origin")
        retained = origin.get("modules") if isinstance(origin, dict) else None
        modules = tuple(str(item) for item in retained) if isinstance(retained, list) else ()
        source = f"import {modules[0]}\n" if modules else "example : True := by trivial\n"
    else:
        source = (
            f"import {ROOT_MODULE}\n"
            if environment.lock.packages
            else "example : True := by trivial\n"
        )
    result = environment.check(
        source,
        # The generated environment module itself is named ROOT_MODULE.  Giving
        # the probe that filename would make Lean see `import X` inside X and
        # correctly reject it as an import cycle.
        filename="LeanRuntimeVerification.lean",
        policy=ExecutionPolicy(timeout_seconds=300, max_output_bytes=2_000_000),
        _allow_sparse_acquisition=not offline,
    )
    if not result.ok:
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
        _probe(rebuilt)
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


def attestation_predicate(report: VerificationReport, workspace: Path) -> dict[str, Any]:
    """Bind a verification result to a stable inventory of the built outputs.

    The inventory is computed directly from the environment's Lake build
    outputs, so an ordinary ``--attest`` records it without the independent
    rebuild that ``verify --rebuild`` performs.
    """
    inventory = _artifact_inventory(workspace)
    return {
        "schema": ATTESTATION_SCHEMA,
        "lock_id": report.lock_id,
        "environment_id": report.environment_id,
        "verification": report.to_dict(),
        "build_inventory": {
            "digest": inventory.digest,
            "entries": inventory.entries,
            "bytes": inventory.bytes,
        },
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
    sparse = (environment.workspace / CAPSULE_MANIFEST).is_file()
    try:
        if sparse:
            verified = _verify_capsule_state(environment)
            source = VerificationCheck(
                "capsule_artifacts_verified", True, details={"artifacts": verified}
            )
        else:
            _verify_sources(environment)
            source = VerificationCheck(
                "package_trees_verified",
                True,
                details={"packages": len(environment.lock.packages)},
            )
    except EnvironmentError as exc:
        source = VerificationCheck("source_tree_mismatch", False, details={"message": str(exc)})
        failures.append(source)
    checks.append(source)
    try:
        if offline and not runtime.toolchains.is_available_locally(environment.lock.toolchain):
            raise EnvironmentError(
                "offline verification requires the locked toolchain to be installed"
            )
        _probe(environment, offline=offline)
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
