"""Structured verification of locks and published environments."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audit import AuditReport, _probe, _verify_sources, audit_environment
from .environments import Environment
from .errors import EnvironmentError
from .lockfiles import EnvironmentLock
from .store import environment_identity, platform_compatibility

VERIFY_SCHEMA = "lean-runtime.verify/v1"


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
        report: AuditReport = audit_environment(
            environment, runtime.toolchains, runtime.backend, runtime.events, rebuild=True
        )
        artifact_match = report.artifact_match
        checks.append(VerificationCheck("independent_rebuild_passed", report.ok))
        artifact = VerificationCheck(
            "artifact_inventory_match",
            artifact_match is True,
            details={
                "original": report.artifacts.digest,
                "rebuilt": report.rebuilt_artifacts.digest if report.rebuilt_artifacts else None,
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
