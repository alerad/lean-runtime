"""High-level API for reproducible Lean environments and raw project execution."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .backends import Backend, LocalBackend
from .bundles import EnvironmentBundles, PortableCopyInfo
from .comparison import ComparisonEntry, EnvironmentComparison, compare_locks
from .decisions import Decision
from .diagnostics import error_diagnostic, map_diagnostic_paths, parse_diagnostics
from .environments import Environment, EnvironmentManager, ExecutionCapture
from .errors import (
    DownloadUnavailable,
    EnvironmentError,
    ProjectError,
    SpecificationError,
    ToolchainError,
)
from .events import EventCallback, EventEmitter
from .health import DoctorReport, diagnose
from .lockfiles import EnvironmentLock
from .matrix import MatrixContext, MatrixResult, run_matrix
from .models import ExecutionProvenance, ExecutionResult, PhaseTiming, ProjectProvenance
from .oci import (
    DEFAULT_ENVIRONMENT_LIBRARIES,
    OCIEnvironmentCache,
    OCIEnvironmentPublisher,
    OCIRepository,
    PublicationInfo,
)
from .policies import ExecutionPolicy
from .profiling import ProfileReport, run_profile
from .programs import ProgramInfo, ProgramLibrary, ProgramManager, ReadyProgram
from .projects import (
    ProjectContext,
    ProjectEnvironment,
    ProjectPublicationPlan,
    discover_project,
    inspect_project_publication,
)
from .publisher_verification import CosignVerifier
from .references import PACKAGE_ALIASES, PackageReference, discover_package, normalize_references
from .resolver import EnvironmentResolver
from .serialization import sha256_id, sha256_text
from .specs import EnvironmentSpec, GitPackage
from .store import (
    CleanupReport,
    DownloadCleanupReport,
    EnvironmentStore,
    StoreStatus,
    environment_identity,
    platform_compatibility,
    platform_record,
)
from .toolchains import ToolchainManager, normalize_toolchain
from .verification import (
    VerificationCheck,
    VerificationReport,
    load_lock_subject,
    verify_environment,
)

EnvironmentReference = Environment | EnvironmentSpec | EnvironmentLock | str


def _bundled_lock_for_references(
    packages: Sequence[str | PackageReference], toolchain: str | None
) -> EnvironmentLock | None:
    """Match a single exact alias tag to its bundled, downloadable catalog lock."""
    references = normalize_references(packages)
    if len(references) != 1:
        return None
    reference = references[0]
    if reference.revision_kind != "tag":
        return None
    alias = next(
        (
            name
            for name, (owner, repository, _command) in PACKAGE_ALIASES.items()
            if reference.url == f"https://github.com/{owner}/{repository}.git"
        ),
        None,
    )
    if alias is None:
        return None
    entry_id = f"{alias}-{reference.revision}"
    # Imported lazily: discovery's probe layer also imports Runtime.
    from .discovery.defaults import default_catalog

    entry = next((item for item in default_catalog().entries if item.id == entry_id), None)
    if entry is None:
        return None
    if toolchain is not None and normalize_toolchain(toolchain) != entry.toolchain:
        return None
    return entry.lock


def _download_reason(error: Exception) -> str:
    message = str(error).lower()
    if "platform" in message or "compatible" in message:
        return "platform_compatibility_mismatch"
    if "signature" in message or "verification_tool" in message:
        return "signature_policy_rejected"
    if "lock" in message and "mismatch" in message:
        return "lock_id_mismatch"
    if "environment" in message and "mismatch" in message:
        return "environment_id_mismatch"
    if "digest" in message or "corrupt" in message:
        return "remote_candidate_corrupt"
    if "not found" in message or "404" in message:
        return "remote_candidate_missing"
    return "remote_candidate_unavailable"


def project_toolchain(project: str | os.PathLike[str]) -> str:
    """Read and normalize the toolchain pinned by a Lean project."""
    root = Path(project).expanduser().resolve()
    path = root / "lean-toolchain"
    if not path.is_file():
        raise ProjectError(f"project has no lean-toolchain file: {root}")
    return normalize_toolchain(path.read_text(encoding="utf-8"))


class Runtime:
    """Compile environments and execute trusted Lean inputs within them."""

    def __init__(
        self,
        *,
        home: str | os.PathLike[str] | None = None,
        toolchains: ToolchainManager | None = None,
        backend: Backend | None = None,
        on_event: EventCallback | None = None,
        availability: str | None = None,
        libraries: Sequence[str] | None = None,
        publisher_verification: str = "ignore",
        trusted_publisher: str | None = None,
        trusted_issuer: str | None = None,
        verification_tool: str | os.PathLike[str] = "cosign",
    ) -> None:
        availability = availability or os.environ.get("LEAN_RUNTIME_AVAILABILITY", "auto")
        if availability not in {"auto", "required", "local"}:
            raise ValueError("availability must be 'auto', 'required', or 'local'")
        if publisher_verification not in {"ignore", "required"}:
            raise ValueError("publisher_verification must be 'ignore' or 'required'")
        if publisher_verification == "required" and (not trusted_publisher or not trusted_issuer):
            raise ValueError(
                "required publisher verification needs trusted_publisher and trusted_issuer"
            )
        self.events = EventEmitter(on_event)
        self.toolchains = toolchains or ToolchainManager(home, events=self.events)
        self.home = self.toolchains.home
        self.backend = backend or LocalBackend()
        self.store = EnvironmentStore(self.home)
        self.resolver = EnvironmentResolver(self.toolchains, self.store, self.backend, self.events)
        self.environments = EnvironmentManager(
            self.store, self.toolchains, self.backend, self.events
        )
        self.bundles = EnvironmentBundles(self.store, self.toolchains, self.backend, self.events)
        self.programs = ProgramManager(self.store, self.backend, self.events)
        configured_libraries = libraries
        if configured_libraries is None:
            configured = os.environ.get("LEAN_RUNTIME_LIBRARIES")
            configured_libraries = (
                tuple(item.strip() for item in configured.split(",") if item.strip())
                if configured is not None
                else DEFAULT_ENVIRONMENT_LIBRARIES
            )
        self.availability = availability
        self.verification_executable = verification_tool
        self.signature_verifier = (
            CosignVerifier(trusted_publisher, trusted_issuer, executable=verification_tool)
            if publisher_verification == "required"
            else None
        )
        self.libraries = tuple(
            OCIEnvironmentCache(
                OCIRepository.parse(value),
                self.store,
                self.bundles,
                self.events,
                self.signature_verifier,
            )
            for value in configured_libraries
        )

    def prepare(
        self,
        spec: EnvironmentSpec,
        *,
        timeout: float = 900,
        cancel: threading.Event | None = None,
    ) -> EnvironmentLock:
        return self.resolver.resolve(spec, timeout=timeout, cancel=cancel)

    def open_exact(
        self,
        lock: EnvironmentLock,
        *,
        name: str | None = None,
        build_profile: str = "release",
        build_timeout: float = 1800,
        accelerate: bool = False,
        cancel: threading.Event | None = None,
    ) -> Environment:
        environment_id = environment_identity(lock, build_profile)
        destination = self.store.environment_path(environment_id)
        if not destination.is_dir() and self.availability != "local":
            imported = False
            rejections: list[str] = []
            for library in self.libraries:
                try:
                    library.pull(lock, name=name)
                    imported = True
                    break
                except DownloadUnavailable as exc:
                    reason_code = _download_reason(exc)
                    rejections.append(f"{library.repository.display}: {reason_code} ({exc})")
                    self.events.emit(
                        "availability.fallback",
                        "Downloadable environment is unavailable",
                        library=library.repository.display,
                        reason=str(exc),
                        reason_code=reason_code,
                    )
            if not imported and self.availability == "required":
                if not self.libraries:
                    raise EnvironmentError(
                        "a downloadable environment is required but no environment libraries "
                        "are configured"
                    )
                nearest = f" Nearest candidates: {'; '.join(rejections)}" if rejections else ""
                raise EnvironmentError(
                    "no compatible downloadable environment was found; availability=required "
                    "does not permit a source build." + nearest
                )
        return self.environments.ensure(
            lock,
            name=name,
            build_profile=build_profile,
            build_timeout=build_timeout,
            accelerate=accelerate,
            cancel=cancel,
        )

    def environment(self, identifier: str) -> Environment:
        """Open a published environment without resolution or network access."""
        return self.environments.open(identifier)

    def create_program(
        self,
        payload: str | os.PathLike[str],
        *,
        command: Sequence[str],
        source_revision: str,
        source_environment_id: str | None = None,
        exact_environment_id: str | None = None,
        toolchain: str = "unknown",
        capability_id: str | None = None,
        provenance: Mapping[str, str] | None = None,
    ) -> ReadyProgram:
        """Create a verified ready-to-run program from a payload directory."""
        return self.programs.create(
            Path(payload),
            command=command,
            source_revision=source_revision,
            source_environment_id=source_environment_id,
            exact_environment_id=exact_environment_id,
            toolchain=toolchain,
            capability_id=capability_id,
            provenance=provenance,
        )

    def program(self, program_id: str) -> ReadyProgram:
        """Open a ready-to-run program already stored on this computer."""
        return self.programs.open(program_id)

    def save_program_copy(self, program_id: str, output: str | os.PathLike[str]) -> ProgramInfo:
        """Save a portable copy of a ready-to-run program."""
        return self.programs.export(program_id, Path(output))

    def open_program_copy(self, copy: str | os.PathLike[str]) -> ReadyProgram:
        """Verify and open a portable program copy."""
        return self.programs.import_bundle(Path(copy))

    def _program_library(self, library: str) -> ProgramLibrary:
        return ProgramLibrary(
            OCIRepository.parse(library),
            self.store,
            self.programs,
            self.events,
            self.signature_verifier,
        )

    def download_program(
        self,
        library: str,
        reference: str,
        *,
        expected_source_revision: str | None = None,
    ) -> ReadyProgram:
        """Download the compatible ready-to-run program from a library."""
        return self._program_library(library).pull(
            reference, expected_source_revision=expected_source_revision
        )

    def publish_program(
        self,
        program_id: str,
        library: str,
        *,
        tags: Sequence[str] = (),
        sign: bool = False,
    ) -> ProgramInfo:
        """Publish this computer's ready-to-run program to a library."""
        selected = self._program_library(library)
        result = selected.publish(program_id, tags=tags)
        if sign and result.copy_id is not None:
            CosignVerifier(None, None, executable=self.verification_executable).sign(
                selected.repository, result.copy_id
            )
        return result

    def finalize_program_publication(
        self,
        library: str,
        source_revision: str,
        computer_records: Sequence[dict[str, Any]],
        *,
        tags: Sequence[str] = (),
        sign: bool = False,
    ) -> str:
        """Combine computer-specific program records into one publication."""
        selected = self._program_library(library)
        publication_id = selected.publish_index(source_revision, computer_records, tags=tags)
        if sign:
            CosignVerifier(None, None, executable=self.verification_executable).sign(
                selected.repository, publication_id
            )
        return publication_id

    def save_portable_copy(
        self, identifier: str, output: str | os.PathLike[str]
    ) -> PortableCopyInfo:
        """Save a published environment as a verified portable copy."""
        environment = self.environment(identifier)
        return self.bundles.export(environment.id, Path(output))

    def open_portable_copy(
        self,
        copy: str | os.PathLike[str],
        *,
        name: str | None = None,
        probe: bool = True,
    ) -> Environment:
        """Verify and atomically open a portable environment copy."""
        info = self.bundles.import_bundle(Path(copy), name=name, probe=probe)
        return self.environment(info.environment_id)

    def publish_environment(
        self,
        identifier: str,
        library: str,
        *,
        tags: Sequence[str] = (),
        finalize: bool = True,
        sign: bool = False,
        attest: bool = False,
    ) -> PublicationInfo:
        """Publish a built environment to an environment library."""
        environment = self.environment(identifier)
        publisher = OCIEnvironmentPublisher(
            OCIRepository.parse(library), self.store, self.bundles, self.events
        )
        result = publisher.publish(environment.id, tags=tuple(tags), finalize=finalize)
        if sign:
            if result.publication_id is None:
                raise ValueError("platform-only publishing cannot sign a lock index")
            CosignVerifier(executable=self.verification_executable).sign(
                publisher.repository, result.publication_id
            )
        if attest:
            self.events.emit(
                "library.attestation_started",
                "Verifying and attesting the published environment",
                environment_id=environment.id,
            )
            report = self.verify(environment.id)
            report.raise_for_error()
            CosignVerifier(executable=self.verification_executable).attest(
                publisher.repository,
                result.publication_id or result.computer_copy_id,
                report.to_dict(),
            )
            self.events.emit(
                "library.attestation_published",
                "Published the signed environment attestation",
                digest=result.publication_id or result.computer_copy_id,
            )
        return result

    def verify(
        self,
        subject: str | os.PathLike[str],
        *,
        offline: bool = False,
        rebuild: bool = False,
    ) -> VerificationReport:
        """Verify a lock or published environment using the runtime's canonical checks."""
        if offline and rebuild:
            raise SpecificationError("offline and rebuild verification are mutually exclusive")
        path = Path(subject).expanduser()
        if path.is_file():
            if rebuild:
                raise SpecificationError("rebuild verification requires a published environment")
            return load_lock_subject(path.resolve())
        environment_id = self.store.resolve_identifier(str(subject))
        metadata_path = self.store.environment_path(environment_id) / "metadata.json"
        if metadata_path.is_file():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            lock = self.store.load_lock(str(metadata.get("lock_id")))
            observed_platform = metadata.get("platform_compatibility")
            expected_platform = platform_compatibility()
            expected_id = environment_identity(lock, str(metadata.get("build_profile")))
            failure: VerificationCheck | None = None
            if observed_platform is not None and observed_platform != expected_platform:
                failure = VerificationCheck(
                    "platform_compatibility_mismatch",
                    False,
                    details={"expected": expected_platform, "observed": observed_platform},
                )
            elif metadata.get("environment_id") != environment_id or expected_id != environment_id:
                failure = VerificationCheck(
                    "environment_id_mismatch",
                    False,
                    details={"expected": expected_id, "observed": environment_id},
                )
            if failure is not None:
                checks = (
                    VerificationCheck("alias_resolved", True, subject=environment_id),
                    failure,
                    VerificationCheck("package_trees_verified", True, skipped=True),
                    VerificationCheck("lean_probe", True, skipped=True),
                )
                return VerificationReport(
                    str(subject),
                    "environment",
                    checks,
                    (failure,),
                    (),
                    lock.lock_id,
                    environment_id,
                )
        environment = self.environment(str(subject))
        return verify_environment(self, environment, rebuild=rebuild, offline=offline)

    def compare(
        self,
        left: str | os.PathLike[str],
        right: str | os.PathLike[str],
    ) -> EnvironmentComparison:
        """Compare the semantic identity inputs of two locks or environments."""

        def prepare(value: str | os.PathLike[str]) -> tuple[EnvironmentLock, Environment | None]:
            path = Path(value).expanduser()
            if path.is_file():
                return EnvironmentLock.load(path), None
            environment = self.environment(str(value))
            return environment.lock, environment

        left_lock, left_environment = prepare(left)
        right_lock, right_environment = prepare(right)
        result = compare_locks(left_lock, right_lock)
        changes = list(result.changes)
        left_info = dict(result.left)
        right_info = dict(result.right)
        if left_environment is not None:
            left_info.update(kind="environment", environment_id=left_environment.id)
        if right_environment is not None:
            right_info.update(kind="environment", environment_id=right_environment.id)
        if left_environment is not None and right_environment is not None:
            for field in ("build_profile", "platform"):
                before = left_environment.inspect().to_dict()[field]
                after = right_environment.inspect().to_dict()[field]
                if before != after:
                    changes.append(ComparisonEntry(field, "changed", True, before, after))
        return EnvironmentComparison(left_info, right_info, tuple(changes))

    def subject_environment(self, subject: str | os.PathLike[str]) -> Environment:
        """Resolve a lock path or environment name to an open environment.

        A path that exists on disk always wins over an environment name.
        """
        path = Path(subject).expanduser()
        if path.is_file():
            return self.open_exact(EnvironmentLock.load(path.resolve()))
        return self.environment(str(subject))

    def profile(
        self,
        environment: str,
        file: str | os.PathLike[str],
        *,
        warmup: int = 1,
        repeat: int = 5,
    ) -> ProfileReport:
        """Repeatedly check one file in an exact environment named by alias or lock path."""
        path = Path(file).expanduser().resolve()
        return run_profile(
            self.subject_environment(environment),
            path.read_text(encoding="utf-8"),
            filename=path.name,
            warmup=warmup,
            repeat=repeat,
        )

    def check_matrix(
        self,
        source: str,
        *,
        contexts: Sequence[MatrixContext],
        filename: str = "Main.lean",
        base: str | os.PathLike[str] = ".",
        concurrency: int = 1,
        cancel: threading.Event | None = None,
    ) -> MatrixResult:
        """Check one input through ordinary execution paths for each named context."""
        return run_matrix(
            self,
            source,
            filename=filename,
            contexts=tuple(contexts),
            base=Path(base).expanduser().resolve(),
            concurrency=concurrency,
            cancel=cancel,
        )

    def explain(self, subject: str | os.PathLike[str]) -> tuple[Decision, ...]:
        """Explain locally observable identity, origin, and compatibility decisions."""
        path = Path(subject).expanduser()
        if path.is_file():
            lock = EnvironmentLock.load(path)
            environment_id = environment_identity(lock)
            exists = self.store.environment_path(environment_id).is_dir()
            return (
                Decision(
                    "lock_identity_verified",
                    str(path),
                    "accepted",
                    details={"lock_id": lock.lock_id},
                ),
                Decision(
                    "local_environment_hit" if exists else "local_environment_missing",
                    environment_id,
                    "accepted" if exists else "missing",
                    reason=None if exists else "source_build_or_remote_pull_required",
                ),
            )
        environment = self.environment(str(subject))
        origin = environment._record.get("origin", {"kind": "local"})
        return (
            Decision(
                "alias_resolved",
                str(subject),
                "accepted",
                details={"environment_id": environment.id},
            ),
            Decision(
                "local_environment_hit",
                environment.id,
                "accepted",
                details={"origin": origin},
            ),
            Decision(
                "platform_compatibility_match",
                environment.id,
                "accepted",
                details={"platform_compatibility": platform_compatibility()},
            ),
        )

    def finalize_publication(
        self,
        library: str,
        lock_id: str,
        computer_records: Sequence[dict[str, Any]],
        *,
        tags: Sequence[str] = (),
        sign: bool = False,
    ) -> str:
        """Finalize a deterministic publication for multiple computer types."""
        publisher = OCIEnvironmentPublisher(
            OCIRepository.parse(library), self.store, self.bundles, self.events
        )
        publication_id = publisher.publish_index(lock_id, list(computer_records), tags=tuple(tags))
        if sign:
            self.events.emit(
                "library.index_signing_started",
                "Signing the finalized environment index",
                digest=publication_id,
            )
            CosignVerifier(executable=self.verification_executable).sign(
                publisher.repository, publication_id
            )
            self.events.emit(
                "library.index_signed",
                "Signed the finalized environment index",
                digest=publication_id,
            )
        return publication_id

    def create_environment(
        self,
        name: str,
        *,
        toolchain: str,
        packages: Sequence[GitPackage],
        timeout: float = 900,
    ) -> Environment:
        spec = EnvironmentSpec(toolchain, tuple(packages))
        return self.open_exact(self.prepare(spec, timeout=timeout), name=name)

    def spec_from_references(
        self,
        packages: Sequence[str | PackageReference],
        *,
        toolchain: str | None = None,
    ) -> EnvironmentSpec:
        """Discover GitHub-style package references and return an exact specification."""
        references = normalize_references(tuple(packages))
        if not references:
            raise SpecificationError("at least one package reference is required")
        discovery_root = self.store.home / "resolution" / "references"
        discovered = []
        for reference in references:
            self.events.emit(
                "package_reference.started",
                f"Discovering {reference.display}",
                reference=reference.display,
            )
            package = discover_package(
                reference,
                directory=discovery_root,
                toolchains=self.toolchains,
            )
            discovered.append(package)
            self.events.emit(
                "package_reference.resolved",
                f"Discovered {package.package.name}",
                reference=reference.display,
                package=package.package.name,
                revision=package.package.rev,
                toolchain=package.toolchain,
                root_module=package.package.module,
            )
        declared_toolchains = {package.toolchain for package in discovered}
        if toolchain is None:
            if len(declared_toolchains) != 1:
                details = ", ".join(
                    f"{package.package.name}={package.toolchain}" for package in discovered
                )
                raise SpecificationError(
                    "package references declare different Lean toolchains; "
                    f"select one explicitly with toolchain=... ({details})"
                )
            selected = next(iter(declared_toolchains))
        else:
            selected = normalize_toolchain(toolchain)
            for package in discovered:
                if package.toolchain != selected:
                    self.events.emit(
                        "compatibility.toolchain_override",
                        f"{package.package.name} declares {package.toolchain}; using {selected}",
                        package=package.package.name,
                        declared_toolchain=package.toolchain,
                        environment_toolchain=selected,
                    )
        return EnvironmentSpec(selected, tuple(package.package for package in discovered))

    def prepare_references(
        self,
        packages: Sequence[str | PackageReference],
        *,
        toolchain: str | None = None,
        timeout: float = 900,
        cancel: threading.Event | None = None,
    ) -> EnvironmentLock:
        """Discover package references and resolve their exact Lake graph."""
        catalog_lock = _bundled_lock_for_references(packages, toolchain)
        if catalog_lock is not None:
            self.events.emit(
                "catalog.reference_matched",
                "Using bundled exact environment for package reference",
                lock_id=catalog_lock.lock_id,
            )
            return catalog_lock
        return self.prepare(
            self.spec_from_references(packages, toolchain=toolchain),
            timeout=timeout,
            cancel=cancel,
        )

    def open_references(
        self,
        packages: Sequence[str | PackageReference],
        *,
        toolchain: str | None = None,
        name: str | None = None,
        timeout: float = 900,
        cancel: threading.Event | None = None,
    ) -> Environment:
        """Build or reopen the environment described by package references."""
        return self.open_exact(
            self.prepare_references(packages, toolchain=toolchain, timeout=timeout, cancel=cancel),
            name=name,
            cancel=cancel,
        )

    def open_toolchain(
        self,
        toolchain: str,
        *,
        name: str | None = None,
        timeout: float = 900,
        cancel: threading.Event | None = None,
    ) -> Environment:
        """Build or reopen the core-only environment for one Lean toolchain."""
        spec = EnvironmentSpec(toolchain)
        return self.open_exact(
            self.prepare(spec, timeout=timeout, cancel=cancel),
            name=name,
            cancel=cancel,
        )

    def check(
        self,
        source: str,
        *,
        environment: EnvironmentReference | None = None,
        packages: Sequence[str | PackageReference] = (),
        toolchain: str | None = None,
        project: str | os.PathLike[str] | None = None,
        filename: str = "Main.lean",
        timeout: float | None = None,
        policy: ExecutionPolicy | None = None,
        cancel: threading.Event | None = None,
    ) -> ExecutionResult:
        """Check source in a content-addressed environment or a raw toolchain/project."""
        selected_policy = policy or ExecutionPolicy(timeout_seconds=timeout or 120)
        if timeout is not None and policy is not None:
            selected_policy = replace(policy, timeout_seconds=timeout)
        if environment is not None and packages:
            raise SpecificationError("check cannot combine environment= with packages=")
        if environment is not None and project is not None:
            raise SpecificationError("check cannot combine environment= with project=")
        if project is not None and packages:
            raise SpecificationError("check cannot combine project= with packages=")
        if packages:
            resolved = self.open_references(packages, toolchain=toolchain, cancel=cancel)
            return resolved.check(source, filename=filename, policy=selected_policy, cancel=cancel)
        if environment is not None:
            resolved = self._environment(environment)
            return resolved.check(source, filename=filename, policy=selected_policy, cancel=cancel)
        if project is not None:
            return self.project(project, toolchain=toolchain).check(
                source, filename=filename, policy=selected_policy, cancel=cancel
            )
        return self._raw_check(
            source,
            toolchain=toolchain,
            project=project,
            filename=filename,
            policy=selected_policy,
            cancel=cancel,
        )

    def check_file(
        self,
        path: str | os.PathLike[str],
        *,
        environment: EnvironmentReference | None = None,
        packages: Sequence[str | PackageReference] = (),
        toolchain: str | None = None,
        project: str | os.PathLike[str] | None = None,
        timeout: float | None = None,
        policy: ExecutionPolicy | None = None,
        cancel: threading.Event | None = None,
    ) -> ExecutionResult:
        source_path = Path(path).expanduser().resolve()
        selected_policy = policy or ExecutionPolicy(timeout_seconds=timeout or 120)
        if timeout is not None and policy is not None:
            selected_policy = replace(policy, timeout_seconds=timeout)
        if environment is None and not packages:
            if project is not None:
                return self.project(project, toolchain=toolchain).check_file(
                    source_path, policy=selected_policy, cancel=cancel
                )
            if toolchain is None:
                return self.project(source_path).check_file(
                    source_path, policy=selected_policy, cancel=cancel
                )
        return self.check(
            source_path.read_text(encoding="utf-8"),
            filename=source_path.name,
            environment=environment,
            packages=packages,
            toolchain=toolchain,
            project=project,
            timeout=timeout,
            policy=selected_policy,
            cancel=cancel,
        )

    def check_files(
        self,
        files: Mapping[str, str],
        *,
        entrypoint: str = "Main.lean",
        environment: EnvironmentReference,
        policy: ExecutionPolicy | None = None,
        cancel: threading.Event | None = None,
    ) -> ExecutionResult:
        """Check a multi-file request in a managed environment."""
        return self._environment(environment).check_files(
            files, entrypoint=entrypoint, policy=policy, cancel=cancel
        )

    def build(
        self,
        project: str | os.PathLike[str],
        *,
        targets: Sequence[str] = (),
        toolchain: str | None = None,
        timeout: float = 900,
    ) -> ExecutionResult:
        """Build an existing trusted Lake project outside the environment store."""
        context = discover_project(project)
        if toolchain is not None:
            context = replace(context, toolchain=normalize_toolchain(toolchain))
        return self._build_project(
            context,
            targets=targets,
            policy=ExecutionPolicy(timeout_seconds=timeout, max_output_bytes=10_000_000),
        )

    def project(
        self,
        path: str | os.PathLike[str],
        *,
        toolchain: str | None = None,
    ) -> ProjectEnvironment:
        """Open the nearest pinned Lake project containing ``path``."""
        context = discover_project(path)
        if toolchain is not None:
            context = replace(context, toolchain=normalize_toolchain(toolchain))
        return ProjectEnvironment(self, context)

    def inspect_project_publication(
        self,
        path: str | os.PathLike[str],
        *,
        module: str | None = None,
        check_remote: bool = False,
    ) -> ProjectPublicationPlan:
        """Assess a local project without building or mutating it."""
        return inspect_project_publication(self, path, module=module, check_remote=check_remote)

    def prepare_project(
        self,
        path: str | os.PathLike[str],
        *,
        module: str | None = None,
        timeout: float = 900,
        cancel: threading.Event | None = None,
    ) -> EnvironmentLock:
        """Freeze a clean, remotely available GitHub project into an exact lock."""
        plan = self.inspect_project_publication(
            path, module=module, check_remote=True
        ).require_ready()
        assert plan.repository is not None
        assert plan.revision is not None
        assert plan.selected_module is not None
        spec = EnvironmentSpec(
            plan.toolchain,
            (
                GitPackage.git(
                    plan.package,
                    plan.repository,
                    plan.revision,
                    root_module=plan.selected_module,
                ),
            ),
        )
        return self.prepare(spec, timeout=timeout, cancel=cancel)

    def export_project(
        self,
        path: str | os.PathLike[str],
        output: str | os.PathLike[str],
        *,
        module: str | None = None,
        timeout: float = 1800,
        accelerate: bool = True,
        cancel: threading.Event | None = None,
    ) -> PortableCopyInfo:
        """Build and export the current platform for one immutable project commit."""
        lock = self.prepare_project(path, module=module, timeout=timeout, cancel=cancel)
        environment = self.open_exact(
            lock,
            build_timeout=timeout,
            accelerate=accelerate,
            cancel=cancel,
        )
        return self.save_portable_copy(environment.id, output)

    def clean(
        self, *, dry_run: bool = True, minimum_age_seconds: float = 2_592_000
    ) -> CleanupReport:
        return self.store.clean(dry_run=dry_run, minimum_age_seconds=minimum_age_seconds)

    def clean_downloads(
        self, *, dry_run: bool = True, minimum_age_seconds: float = 2_592_000
    ) -> DownloadCleanupReport:
        return self.store.clean_downloads(dry_run=dry_run, minimum_age_seconds=minimum_age_seconds)

    def doctor(self) -> DoctorReport:
        return diagnose(self.toolchains, self.store)

    def store_status(self) -> StoreStatus:
        return self.store.status()

    def list_environments(self) -> tuple[dict[str, object], ...]:
        aliases = self.store.aliases()
        names_by_id: dict[str, list[str]] = {}
        for name, environment_id in aliases.items():
            names_by_id.setdefault(environment_id, []).append(name)
        records: list[dict[str, object]] = []
        for path in sorted(self.store.environments.glob("env_*")):
            metadata_path = path / "metadata.json"
            if not metadata_path.is_file():
                continue
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            records.append(
                {
                    "environment_id": path.name,
                    "lock_id": metadata.get("lock_id"),
                    "toolchain": metadata.get("toolchain"),
                    "created_at": metadata.get("created_at"),
                    "status": metadata.get("status"),
                    "origin": metadata.get("origin", {"kind": "local"}),
                    "names": sorted(names_by_id.get(path.name, [])),
                }
            )
        return tuple(records)

    def replay_capture(self, capture: ExecutionCapture | str | os.PathLike[str]) -> ExecutionResult:
        """Materialize a capture's lock if needed and replay its check request."""
        resolved = (
            ExecutionCapture.load(capture) if isinstance(capture, (str, os.PathLike)) else capture
        )
        if resolved.operation != "check":
            raise ProjectError(f"unsupported capture operation: {resolved.operation}")
        environment = self.open_exact(resolved.lock)
        return environment.check_files(
            resolved.files,
            entrypoint=resolved.entrypoint,
            policy=resolved.policy,
        )

    def _environment(self, value: EnvironmentReference) -> Environment:
        if isinstance(value, Environment):
            return value
        if isinstance(value, EnvironmentSpec):
            return self.open_exact(self.prepare(value))
        if isinstance(value, EnvironmentLock):
            return self.open_exact(value)
        return self.environment(value)

    def _raw_check(
        self,
        source: str,
        *,
        toolchain: str | None,
        project: str | os.PathLike[str] | None,
        filename: str,
        policy: ExecutionPolicy,
        cancel: threading.Event | None,
    ) -> ExecutionResult:
        project_root = Path(project).expanduser().resolve() if project else None
        selected = normalize_toolchain(toolchain) if toolchain else None
        if selected is None and project_root is not None:
            selected = project_toolchain(project_root)
        if selected is None:
            raise ToolchainError("check requires an environment, toolchain, or pinned project")
        safe_filename = Path(filename).name
        if not safe_filename.endswith(".lean"):
            safe_filename += ".lean"
        with tempfile.TemporaryDirectory(prefix="check-file-", dir=self.store.jobs) as raw:
            source_path = Path(raw) / safe_filename
            source_path.write_text(source, encoding="utf-8")
            if project_root is None:
                command = self.toolchains.command(selected, "lean", str(source_path))
                cwd = source_path.parent
            else:
                if not project_root.is_dir():
                    raise ProjectError(f"project directory does not exist: {project_root}")
                command = self.toolchains.command(selected, "lake", "env", "lean", str(source_path))
                cwd = project_root
            return self._raw_result(
                command,
                cwd=cwd,
                toolchain=selected,
                source_digest=sha256_text(source),
                policy=policy,
                path_map={str(source_path): safe_filename},
                cancel=cancel,
            )

    def _check_project_file(
        self,
        context: ProjectContext,
        source: Path,
        *,
        policy: ExecutionPolicy,
        cancel: threading.Event | None = None,
    ) -> ExecutionResult:
        relative = source.relative_to(context.root).as_posix()
        command = self.toolchains.command(context.toolchain, "lake", "env", "lean", relative)
        return self._raw_result(
            command,
            cwd=context.root,
            toolchain=context.toolchain,
            source_digest=sha256_text(source.read_text(encoding="utf-8")),
            policy=policy,
            project=context.provenance(),
            logical_command=("lake", "env", "lean", relative),
            cancel=cancel,
        )

    def _check_project_source(
        self,
        context: ProjectContext,
        source: str,
        *,
        filename: str,
        policy: ExecutionPolicy,
        cancel: threading.Event | None = None,
    ) -> ExecutionResult:
        safe_filename = Path(filename).name
        if not safe_filename.endswith(".lean"):
            safe_filename += ".lean"
        jobs = context.root / ".lake" / "lean-runtime"
        jobs.mkdir(parents=True, exist_ok=True)
        provenance = context.provenance()
        with tempfile.TemporaryDirectory(prefix="check-", dir=jobs) as temporary:
            source_path = Path(temporary) / safe_filename
            source_path.write_text(source, encoding="utf-8")
            relative = source_path.relative_to(context.root).as_posix()
            command = self.toolchains.command(context.toolchain, "lake", "env", "lean", relative)
            return self._raw_result(
                command,
                cwd=context.root,
                toolchain=context.toolchain,
                source_digest=sha256_text(source),
                policy=policy,
                project=provenance,
                logical_command=("lake", "env", "lean", safe_filename),
                path_map={relative: safe_filename, str(source_path): safe_filename},
                cancel=cancel,
            )

    def _build_project(
        self,
        context: ProjectContext,
        *,
        targets: Sequence[str],
        policy: ExecutionPolicy,
        cancel: threading.Event | None = None,
    ) -> ExecutionResult:
        command = self.toolchains.command(context.toolchain, "lake", "build", *targets)
        return self._raw_result(
            command,
            cwd=context.root,
            toolchain=context.toolchain,
            source_digest=sha256_text(""),
            policy=policy,
            project=context.provenance(),
            logical_command=("lake", "build", *targets),
            cancel=cancel,
        )

    def _raw_result(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        toolchain: str,
        source_digest: str,
        policy: ExecutionPolicy,
        project: ProjectProvenance | None = None,
        logical_command: Sequence[str] | None = None,
        path_map: Mapping[str, str] | None = None,
        cancel: threading.Event | None = None,
    ) -> ExecutionResult:
        started_at = datetime.now(timezone.utc).isoformat()
        request_command = (
            list(logical_command) if logical_command is not None else list(command[3:])
        )
        if source_digest != sha256_text("") and request_command and project is None:
            request_command[-1] = Path(request_command[-1]).name
        request_digest = sha256_id(
            "request",
            {
                "environment_id": None,
                "toolchain": toolchain,
                "command": request_command,
                "source_digest": source_digest,
                "project": project.to_dict() if project is not None else None,
                "policy": policy.to_dict(),
                "backend": self.backend.name,
            },
        )
        execution_id = sha256_id(
            "execution",
            {
                "request_digest": request_digest,
                "started_at": started_at,
                "nonce": os.urandom(16).hex(),
            },
        )
        raw = self.backend.execute(
            command,
            cwd=cwd,
            environment=self.toolchains.environment,
            policy=policy,
            cancel=cancel,
        )
        output = "\n".join(part for part in (raw.stdout, raw.stderr) if part)
        diagnostics = map_diagnostic_paths(parse_diagnostics(output), path_map)
        if raw.timed_out:
            diagnostics += (error_diagnostic("Lean execution exceeded its time limit"),)
        if raw.cancelled:
            diagnostics += (error_diagnostic("Lean execution was cancelled"),)
        provenance = ExecutionProvenance(
            environment_id=None,
            execution_id=execution_id,
            request_digest=request_digest,
            lock_id=None,
            toolchain=toolchain,
            packages=(),
            platform=platform_record(),
            backend=self.backend.name,
            requested_policy=policy.to_dict(),
            enforced_policy_fields=raw.enforced_policy_fields,
            source_digest=source_digest,
            started_at=started_at,
            project=project,
        )
        return ExecutionResult(
            ok=raw.exit_code == 0,
            exit_code=raw.exit_code,
            toolchain=toolchain,
            command=tuple(command),
            cwd=str(cwd),
            stdout=raw.stdout,
            stderr=raw.stderr,
            elapsed_seconds=raw.elapsed_seconds,
            timed_out=raw.timed_out,
            cancelled=raw.cancelled,
            output_truncated=raw.output_truncated,
            diagnostics=diagnostics,
            provenance=provenance,
            timings=(PhaseTiming("execution", round(raw.elapsed_seconds * 1000)),),
        )
