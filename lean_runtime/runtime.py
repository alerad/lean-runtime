"""High-level API for reproducible Lean environments and raw project execution."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timezone
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised by the Python 3.10 CI job
    import tomli as tomllib

from ._paths import remove_tree
from .backends import Backend, LocalBackend
from .bundles import EnvironmentBundles, PortableCopyInfo
from .capsules import source_import_roots
from .comparison import ComparisonEntry, EnvironmentComparison, compare_locks
from .decisions import Decision
from .diagnostics import error_diagnostic, map_diagnostic_paths, parse_diagnostics
from .environments import Environment, EnvironmentManager, ExecutionCapture
from .errors import (
    DownloadLimitExceeded,
    DownloadUnavailable,
    EnvironmentError,
    ProjectError,
    SpecificationError,
    ToolchainError,
)
from .events import EventCallback, EventEmitter
from .header_cache import LeanHeaderCache
from .health import DoctorReport, diagnose, repair
from .identifier_resolver import IdentifierResolver
from .lake_cache import LakeArtifactCache
from .lockfiles import EnvironmentLock
from .matrix import MatrixContext, MatrixResult, run_matrix
from .models import (
    ExecutionProvenance,
    ExecutionResult,
    PackageProvenance,
    PhaseTiming,
    ProjectProvenance,
)
from .oci import (
    DEFAULT_ENVIRONMENT_LIBRARIES,
    OCIEnvironmentCache,
    OCIEnvironmentPublisher,
    OCIRepository,
    PublicationAccess,
    PublicationInfo,
)
from .policies import ExecutionPolicy, parse_byte_size
from .profiling import ProfileReport, run_profile
from .programs import ProgramInfo, ProgramLibrary, ProgramManager, ReadyProgram
from .project_execution import ProjectExecutor
from .project_sharing import (
    AdoptionBatchResult,
    AdoptionPlan,
    AdoptionResult,
    DetachmentPlan,
    ProjectAdopter,
    ProjectInitPlan,
    ProjectScanResult,
    ProjectUpdatePlan,
    discover_shareable_projects,
    plan_adoption,
    plan_detachment,
    project_sharing_enabled,
)
from .projects import (
    ProjectContext,
    ProjectEnvironment,
    ProjectPublicationPlan,
    discover_project,
    inspect_project_publication,
    project_check_workflow,
)
from .publisher_verification import CosignVerifier
from .references import PACKAGE_ALIASES, PackageReference, discover_package, normalize_references
from .resolver import EnvironmentResolver
from .serialization import sha256_id, sha256_text
from .shared_projects import SharedProjectManager, SharedProjectWorkspace
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
from .toolchain_oci import OCIToolchainLibrary, OCIToolchainPublisher, ToolchainPublication
from .toolchains import ToolchainManager, normalize_toolchain
from .verification import (
    VerificationCheck,
    VerificationReport,
    attestation_predicate,
    load_lock_subject,
    verify_environment,
)

EnvironmentReference = Environment | EnvironmentSpec | EnvironmentLock | str

_DEFAULT_AGENTS_GUIDE = """# AGENTS.md

## Project workflow

This is a standard Lean 4 and Lake project whose exact dependencies are shared
through Lean Runtime. Read `lean-toolchain`, the Lake configuration, and
`lake-manifest.json` before changing the project.

- Use `lean-runtime build` for the normal full build.
- Use `lean-runtime check PATH` for a focused source check.
- Use `lean-runtime check` to check every declared local library without
  building executables.
- Ordinary `lake build` and editor tooling work, but `lean-runtime build` also
  serializes writes when another project uses the same shared dependencies.
- Do not edit `.lake/packages` or files reached through its package links; they
  are generated shared dependencies. Keep project changes outside `.lake`.
- Treat `lake-manifest.json` as authoritative. Do not run `lake update` or change
  dependency revisions unless the task explicitly requires it.
- Use `lean-runtime update` to preview an intentional move to the latest
  cataloged Mathlib; apply it only after reviewing the plan.
- Before finishing, check the changed Lean files and run the smallest relevant
  build; use `lean-runtime build` when practical.
"""


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
        max_download_bytes: int | None = None,
        allow_source_build: bool = True,
        publisher_verification: str = "ignore",
        trusted_publisher: str | None = None,
        trusted_issuer: str | None = None,
        verification_tool: str | os.PathLike[str] = "cosign",
    ) -> None:
        availability = availability or os.environ.get("LEAN_RUNTIME_AVAILABILITY", "auto")
        if availability not in {"auto", "required", "local"}:
            raise ValueError("availability must be 'auto', 'required', or 'local'")
        if max_download_bytes is None:
            configured_limit = os.environ.get("LEAN_RUNTIME_MAX_DOWNLOAD")
            if configured_limit:
                max_download_bytes = parse_byte_size(configured_limit)
        if max_download_bytes is not None and max_download_bytes < 0:
            raise ValueError("max_download_bytes must be nonnegative")
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
        self.shared_projects = SharedProjectManager(self.home, self.events)
        self.lake_cache = LakeArtifactCache(self.home, self.toolchains, self.events)
        self.header_cache = LeanHeaderCache(self.home, self.toolchains, self.events)
        self.identifier_resolver = IdentifierResolver(self.home)
        self.project_adopter = ProjectAdopter(self.shared_projects)
        self.project_executor = ProjectExecutor(self)
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
        self.allow_source_build = allow_source_build
        self.max_download_bytes = max_download_bytes
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
                max_download_bytes=max_download_bytes,
            )
            for value in configured_libraries
        )
        self.toolchain_libraries = tuple(
            OCIToolchainLibrary(
                OCIRepository.parse(value),
                self.store,
                self.toolchains,
                self.events,
                self.signature_verifier,
            )
            for value in configured_libraries
        )
        self.toolchains.remote_ensure = self._acquire_check_toolchain
        self.environments.sparse_acquirer = self._acquire_sparse_modules

    def _acquire_check_toolchain(
        self,
        toolchain: str,
        cancel: threading.Event | None = None,
    ) -> bool:
        if self.availability == "local":
            raise ToolchainError(
                f"toolchain {normalize_toolchain(toolchain)!r} is not available locally; "
                "offline mode does not permit a download or Elan installation"
            )
        for library in self.toolchain_libraries:
            try:
                return library.pull(toolchain, cancel=cancel)
            except DownloadUnavailable:
                continue
        return False

    def _acquire_sparse_modules(
        self,
        lock: EnvironmentLock,
        roots: tuple[str, ...],
        capabilities: frozenset[str],
    ) -> None:
        if self.availability == "local":
            raise EnvironmentError(
                "this environment is missing the artifacts for "
                + (", ".join(roots) if roots else "the requested import closure")
                + "; offline mode does not permit extending a sparse projection. "
                "Acquire the closure once while online, or open the exact full "
                "environment."
            )
        rejections: list[str] = []
        for library in self.libraries:
            try:
                library.pull_capsule(lock, roots, capabilities=capabilities)
                return
            except DownloadUnavailable as exc:
                rejections.append(f"{library.repository.display}: {exc}")
        detail = f" ({'; '.join(rejections)})" if rejections else ""
        raise EnvironmentError(
            "sparse environment cannot acquire its requested import closure" + detail
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
        import_roots: Sequence[str] = (),
        cancel: threading.Event | None = None,
    ) -> Environment:
        environment_id = environment_identity(lock, build_profile)
        destination = self.store.environment_path(environment_id)
        imported = False
        if self.max_download_bytes is not None and self.availability != "local":
            preflight = self.plan_exact(
                lock,
                build_profile=build_profile,
                import_roots=import_roots,
            )
            planned = preflight.get("download_bytes")
            complete = preflight.get("download_bytes_complete") is True
            self.events.emit(
                "acquisition.planned",
                "Combined toolchain and environment acquisition planned",
                phase="plan",
                total_bytes=planned if isinstance(planned, int) else None,
                download_bytes=planned,
                environment_download_bytes=preflight.get("environment_download_bytes"),
                toolchain_download_bytes=preflight.get("toolchain_download_bytes"),
            )
            if not complete:
                raise DownloadLimitExceeded(
                    "acquisition cost is incomplete because no published slim runtime or "
                    "environment could be priced; refusing to continue under a download "
                    "limit; inspect components with lean-run --plan"
                )
            if isinstance(planned, int) and planned > self.max_download_bytes:
                raise DownloadLimitExceeded(
                    f"acquiring this check downloads {planned} bytes, above the configured "
                    f"limit of {self.max_download_bytes} bytes; inspect components with "
                    "lean-run --plan"
                )
        if not destination.is_dir() and self.availability != "local":
            rejections: list[str] = []
            for library in self.libraries:
                try:
                    library.pull_capsule(lock, tuple(import_roots), name=name)
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
            if not imported:
                self.events.emit(
                    "capability.required",
                    "No downloadable environment is available; building from source",
                    capability="source_build",
                    environment_id=environment_id,
                )
        if not destination.is_dir() and not imported and not self.allow_source_build:
            raise EnvironmentError(
                f"environment {environment_id} is not retained locally; offline mode "
                "does not permit downloading, toolchain installation, or source "
                "materialization"
            )
        return self.environments.ensure(
            lock,
            name=name,
            build_profile=build_profile,
            build_timeout=build_timeout,
            accelerate=accelerate,
            cancel=cancel,
        )

    def plan_exact(
        self,
        lock: EnvironmentLock,
        *,
        build_profile: str = "release",
        import_roots: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Report what opening a lock would cost, without acquiring anything."""
        environment_id = environment_identity(lock, build_profile)
        destination = self.store.environment_path(environment_id)
        ready = destination.is_dir()
        sparse = (destination / "workspace" / ".lean-runtime" / "capsule.json").is_file()
        libraries: list[dict[str, Any]] = []
        environment_download: int | None = 0 if ready and not sparse else None
        if not ready or sparse:
            for environment_library in self.libraries:
                try:
                    capsule = environment_library.plan_capsule(lock, tuple(import_roots))
                    environment_plan: dict[str, Any] = {
                        "library": environment_library.repository.display,
                        "profile": "check-capsule",
                        "modules": len(capsule.modules),
                        "total_bytes": capsule.total_bytes,
                        "cached_bytes": capsule.cached_bytes,
                        "download_bytes": capsule.download_bytes,
                    }
                except EnvironmentError as exc:
                    libraries.append(
                        {
                            "library": environment_library.repository.display,
                            "available": False,
                            "error": str(exc),
                        }
                    )
                    continue
                environment_plan["available"] = True
                libraries.append(environment_plan)
                if environment_download is None:
                    planned_bytes = environment_plan.get("download_bytes")
                    environment_download = planned_bytes if isinstance(planned_bytes, int) else None
        toolchain_plans: list[dict[str, Any]] = []
        toolchain_download: int | None = 0 if self._toolchain_installed(lock.toolchain) else None
        if toolchain_download is None:
            for toolchain_library in self.toolchain_libraries:
                try:
                    toolchain_plan = toolchain_library.plan(lock.toolchain)
                except EnvironmentError as exc:
                    toolchain_plans.append(
                        {
                            "library": toolchain_library.repository.display,
                            "available": False,
                            "error": str(exc),
                        }
                    )
                    continue
                toolchain_plans.append(
                    {
                        "library": toolchain_library.repository.display,
                        "available": True,
                        "reference": toolchain_plan.reference,
                        "lean_commit": toolchain_plan.lean_commit,
                        "total_bytes": toolchain_plan.total_bytes,
                        "cached_bytes": toolchain_plan.cached_bytes,
                        "download_bytes": toolchain_plan.download_bytes,
                    }
                )
                if toolchain_download is None:
                    toolchain_download = toolchain_plan.download_bytes
        known_components = [
            value for value in (environment_download, toolchain_download) if isinstance(value, int)
        ]
        download_bytes = sum(known_components) if known_components else None
        return {
            "lock_id": lock.lock_id,
            "toolchain": lock.toolchain,
            "environment_id": environment_id,
            "environment_ready": ready,
            "environment_download_bytes": environment_download,
            "toolchain_installed": self._toolchain_installed(lock.toolchain),
            "toolchain_download_bytes": toolchain_download,
            "toolchain_libraries": toolchain_plans,
            "max_download_bytes": self.max_download_bytes,
            "download_bytes": download_bytes,
            "download_bytes_complete": isinstance(environment_download, int)
            and isinstance(toolchain_download, int),
            "libraries": libraries,
        }

    def _toolchain_installed(self, toolchain: str) -> bool:
        """Answer without bootstrapping Elan or installing anything."""
        try:
            if self.toolchains.is_available_locally(toolchain):
                return True
            self.toolchains.elan_path(bootstrap=False)
            return self.toolchains.is_installed(toolchain)
        except (AttributeError, ToolchainError):
            # AttributeError: duck-typed toolchain managers without Elan.
            return False

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
        source_lock_id: str | None = None,
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
            source_lock_id=source_lock_id,
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
        publisher: OCIEnvironmentPublisher | None = None,
    ) -> PublicationInfo:
        """Publish a built environment to an environment library."""
        if sign and not finalize:
            raise ValueError("platform-only publishing cannot sign a lock index")
        environment = self.environment(identifier)
        repository = OCIRepository.parse(library)
        selected_publisher = publisher or OCIEnvironmentPublisher(
            repository, self.store, self.bundles, self.events
        )
        if selected_publisher.repository != repository:
            raise ValueError("publication session does not match the requested library")
        result = selected_publisher.publish(
            environment.id,
            tags=tuple(tags),
            finalize=finalize,
            profile="check-capsule",
        )
        phase = "signing"
        try:
            if sign:
                assert result.publication_id is not None
                CosignVerifier(executable=self.verification_executable).sign(
                    selected_publisher.repository, result.publication_id
                )
            phase = "attestation"
            if attest:
                self.events.emit(
                    "library.attestation_started",
                    "Verifying and attesting the published environment",
                    environment_id=environment.id,
                )
                report = self.verify(environment.id)
                report.raise_for_error()
                CosignVerifier(executable=self.verification_executable).attest(
                    selected_publisher.repository,
                    result.publication_id or result.computer_copy_id,
                    attestation_predicate(report, environment.workspace),
                )
                self.events.emit(
                    "library.attestation_published",
                    "Published the signed environment attestation",
                    digest=result.publication_id or result.computer_copy_id,
                )
        except (EnvironmentError, OSError) as exc:
            raise selected_publisher.fail(exc, phase=phase) from exc
        selected_publisher.complete(result)
        return result

    def begin_publication(
        self,
        library: str,
        *,
        auth_timeout: float = 10,
        registry_timeout: float = 30,
    ) -> OCIEnvironmentPublisher:
        """Resolve and pin publication authentication for one complete attempt."""
        return OCIEnvironmentPublisher(
            OCIRepository.parse(library),
            self.store,
            self.bundles,
            self.events,
            auth_timeout=auth_timeout,
            registry_timeout=registry_timeout,
        )

    def check_publication_access(self, library: str) -> PublicationAccess:
        """Prove registry push access without building or publishing content."""
        return self.begin_publication(library).check_access()

    def publish_toolchain(self, toolchain: str, library: str) -> ToolchainPublication:
        """Publish one verified platform check-toolchain manifest."""
        return OCIToolchainPublisher(OCIRepository.parse(library), self.toolchains).publish(
            toolchain
        )

    def finalize_toolchain_publication(
        self,
        toolchain: str,
        library: str,
        descriptors: list[dict[str, Any]],
        *,
        sign: bool = False,
    ) -> str:
        """Publish the multi-platform index only after every platform succeeds."""
        publisher = OCIToolchainPublisher(OCIRepository.parse(library), self.toolchains)
        digest = publisher.publish_index(toolchain, descriptors)
        if sign:
            CosignVerifier(executable=self.verification_executable).sign(
                publisher.repository, digest
            )
        return digest

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
            lock = self.prepare_references(packages, toolchain=toolchain, cancel=cancel)
            resolved = self.open_exact(
                lock,
                import_roots=source_import_roots(source),
                cancel=cancel,
            )
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
                try:
                    local_project = self.project(source_path)
                except ProjectError as exc:
                    raise ProjectError(
                        f"{exc}\nFor automatic standalone context discovery, use "
                        "`lean-runtime run FILE` (or `lean-run FILE`).\n"
                        "To select core Lean explicitly, pass an exact toolchain: "
                        "`lean-runtime check FILE --toolchain v4.33.0`."
                    ) from exc
                return local_project.check_file(source_path, policy=selected_policy, cancel=cancel)
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
        shared: bool | None = None,
    ) -> ExecutionResult:
        """Build an existing trusted Lake project outside the environment store."""
        context = discover_project(project)
        self.shared_projects.remember_project(context)
        if toolchain is not None:
            context = replace(context, toolchain=normalize_toolchain(toolchain))
        selected_shared = project_sharing_enabled(context.root) if shared is None else shared
        if shared is False and project_sharing_enabled(context.root):
            raise ProjectError(
                "project is attached to shared dependencies; run "
                "`lean-runtime detach . --execute` before requesting a local build"
            )
        return self._build_project(
            context,
            targets=targets,
            policy=ExecutionPolicy(timeout_seconds=timeout, max_output_bytes=10_000_000),
            shared=selected_shared,
        )

    def check_project(
        self,
        project: str | os.PathLike[str] = ".",
        *,
        toolchain: str | None = None,
        timeout: float | None = None,
        policy: ExecutionPolicy | None = None,
        cancel: threading.Event | None = None,
    ) -> ExecutionResult:
        """Check all declared local libraries through Lake's dependency ordering."""
        context = discover_project(project)
        self.shared_projects.remember_project(context)
        if toolchain is not None:
            context = replace(context, toolchain=normalize_toolchain(toolchain))
        selected_policy = policy or ExecutionPolicy(
            timeout_seconds=timeout or 900, max_output_bytes=10_000_000
        )
        if timeout is not None and policy is not None:
            selected_policy = replace(policy, timeout_seconds=timeout)
        return self.project_executor.check_project(
            context,
            policy=selected_policy,
            cancel=cancel,
        )

    def prepare_shared_project(
        self,
        project: str | os.PathLike[str],
        *,
        toolchain: str | None = None,
        cancel: threading.Event | None = None,
    ) -> SharedProjectWorkspace:
        """Prepare reusable dependencies for a pinned local Lake project."""
        context = discover_project(project)
        if toolchain is not None:
            context = replace(context, toolchain=normalize_toolchain(toolchain))
        return self.shared_projects.prepare(context, cancel=cancel)

    def plan_project_adoption(
        self, path: str | os.PathLike[str], *, recursive: bool = False
    ) -> AdoptionPlan:
        """Inspect one project or a tree without changing it."""
        return plan_adoption(Path(path), recursive=recursive, shared=self.shared_projects)

    def _probe_project_graph(
        self,
        context: ProjectContext,
        overrides: Path | None,
        *,
        timeout: float = 120,
    ) -> None:
        self.toolchains.ensure_full(context.toolchain)
        lake_args = (
            *((f"--packages={overrides}",) if overrides else ()),
            "env",
            "lean",
            "--version",
        )
        command = self.toolchains.command(context.toolchain, "lake", *lake_args)
        try:
            process = subprocess.run(
                command,
                cwd=context.root,
                env=self.toolchains.environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProjectError(f"Lake graph probe timed out for {context.root}") from exc
        if process.returncode:
            detail = process.stdout.strip()
            raise ProjectError(
                f"Lake could not load the {'shared' if overrides else 'attached'} dependency "
                f"graph for {context.root}" + (f":\n{detail}" if detail else "")
            )

    def attach_projects(
        self,
        path: str | os.PathLike[str],
        *,
        recursive: bool = False,
        seed_packages: Path | None = None,
        seed_package_paths: dict[str, Path] | None = None,
        plan: AdoptionPlan | None = None,
        display_name: str | None = None,
    ) -> AdoptionBatchResult:
        """Adopt exact shared dependencies after a read-only plan."""
        if plan is not None:
            requested_roots = discover_shareable_projects(Path(path), recursive=recursive)
            planned_roots = tuple(project.root for project in plan.projects)
            if requested_roots != planned_roots or plan.recursive != recursive:
                raise ProjectError("adoption plan does not match the requested project scope")
            selected_plan = plan
        else:
            selected_plan = self.plan_project_adoption(path, recursive=recursive)
        results: list[AdoptionResult] = []
        failures: list[tuple[Path, str]] = []
        for project in selected_plan.projects:
            if project.attached:
                workspace_id = discover_project(project.root).provenance().workspace_id
                results.append(
                    AdoptionResult(
                        project.root,
                        "already-attached",
                        len(project.packages),
                        0,
                        workspace_id,
                    )
                )
                continue
            if not project.ready:
                continue
            context = discover_project(project.root)
            self.shared_projects.remember_project(context)

            def probe(overrides: Path | None, selected_context: ProjectContext = context) -> None:
                self._probe_project_graph(selected_context, overrides)

            try:
                results.append(
                    self.project_adopter.attach(
                        context,
                        probe=probe,
                        seed_packages=(seed_packages if len(selected_plan.projects) == 1 else None),
                        seed_package_paths=(
                            seed_package_paths if len(selected_plan.projects) == 1 else None
                        ),
                        display_name=(display_name if len(selected_plan.projects) == 1 else None),
                    )
                )
            except (OSError, ProjectError) as exc:
                failures.append((project.root, str(exc)))
        return AdoptionBatchResult(selected_plan, tuple(results), tuple(failures))

    def scan_projects(
        self, path: str | os.PathLike[str], *, recursive: bool = True
    ) -> ProjectScanResult:
        """Register local Lake projects as exact dependency seeds without adopting them."""
        selected = Path(path).expanduser().resolve()
        roots = discover_shareable_projects(selected, recursive=recursive)
        for root in roots:
            self.shared_projects.remember_project(discover_project(root))
        return ProjectScanResult(selected, roots)

    @staticmethod
    def _lakefile_with_mathlib_revision(contents: str, revision: str) -> str:
        """Replace Mathlib's input revision in a Lake TOML require block."""
        lines = contents.splitlines(keepends=True)
        starts = [index for index, line in enumerate(lines) if line.strip() == "[[require]]"]
        for position, start in enumerate(starts):
            end = starts[position + 1] if position + 1 < len(starts) else len(lines)
            block = lines[start:end]
            if not any(line.strip().replace(" ", "") == 'name="mathlib"' for line in block):
                continue
            for index in range(start, end):
                if lines[index].lstrip().startswith("rev") and "=" in lines[index]:
                    prefix = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
                    lines[index] = f'{prefix}rev = "{revision}"\n'
                    return "".join(lines)
            lines.insert(end, f'rev = "{revision}"\n')
            return "".join(lines)
        raise ProjectError("lakefile.toml has no [[require]] block named mathlib")

    @staticmethod
    def _project_manifest_packages(
        packages: list[dict[str, Any]], *, mathlib_version: str
    ) -> list[dict[str, Any]]:
        """Retain exact commits while matching Lake's direct input revision."""
        copied = [dict(entry) for entry in packages]
        for entry in copied:
            if entry.get("name") == "mathlib" and entry.get("type") == "git":
                entry["inputRev"] = f"v{mathlib_version.removeprefix('v')}"
                break
        return copied

    def _ensure_project_toolchain(self, toolchain: str) -> None:
        """Ensure the full Lake-capable toolchain under the selected policy."""
        installed = self._toolchain_installed(toolchain)
        if not installed and self.availability == "local":
            raise ProjectError(
                f"offline project setup requires the full {toolchain} toolchain locally"
            )
        if not installed and self.max_download_bytes is not None:
            raise DownloadLimitExceeded(
                "the full Lake-capable Lean toolchain is not installed and Elan's download "
                "size cannot be preflighted; refusing under --max-download; install the "
                "toolchain first with `lean-runtime install VERSION`"
            )
        ensure_full = getattr(self.toolchains, "ensure_full", self.toolchains.ensure)
        ensure_full(toolchain)

    def _project_toolchain_blockers(self, toolchain: str, *, installed: bool) -> tuple[str, ...]:
        if installed:
            return ()
        if self.availability == "local":
            return (f"offline setup requires the full {toolchain} toolchain locally",)
        if self.max_download_bytes is not None:
            return (
                "the full Lake-capable Lean toolchain is missing and its Elan download "
                "cannot be preflighted under --max-download",
            )
        return ()

    def plan_project_update(
        self,
        path: str | os.PathLike[str] = ".",
        *,
        seed_from: str | os.PathLike[str] | None = None,
    ) -> ProjectUpdatePlan:
        """Plan an explicit update to the latest cataloged stable Mathlib."""
        context = discover_project(path)
        blockers: list[str] = []
        if context.lakefile.name != "lakefile.toml":
            blockers.append("automatic updates currently require lakefile.toml")
        manifest_path = context.current_manifest()
        if manifest_path is None:
            raise ProjectError("project update requires lake-manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw_packages = manifest.get("packages") if isinstance(manifest, dict) else None
        if not isinstance(raw_packages, list):
            raise ProjectError("project manifest has no package entries")
        mathlib_package = next(
            (
                entry
                for entry in raw_packages
                if isinstance(entry, dict)
                and entry.get("type") == "git"
                and entry.get("name") == "mathlib"
            ),
            None,
        )
        if mathlib_package is None:
            raise ProjectError("project does not have a locked Mathlib dependency")
        current_revision = str(mathlib_package.get("rev"))
        current_version = str(mathlib_package.get("inputRev") or current_revision).removeprefix("v")
        from .discovery.defaults import default_catalog

        for entry in default_catalog().entries:
            if not entry.id.startswith("mathlib-v"):
                continue
            packages = entry.lock.manifest.get("packages")
            if not isinstance(packages, list):
                continue
            locked_mathlib = next(
                (
                    package
                    for package in packages
                    if isinstance(package, dict) and package.get("name") == "mathlib"
                ),
                None,
            )
            if locked_mathlib is not None and locked_mathlib.get("rev") == current_revision:
                current_version = entry.id.removeprefix("mathlib-v")
                break
        latest = self._mathlib_catalog_entry("latest")
        latest_packages = latest.lock.manifest["packages"]
        assert isinstance(latest_packages, list)
        latest_mathlib = next(
            entry
            for entry in latest_packages
            if isinstance(entry, dict) and entry.get("name") == "mathlib"
        )
        target_version = latest.id.removeprefix("mathlib-v")
        toolchain_installed = self._toolchain_installed(latest.toolchain)
        changed = current_revision != str(latest_mathlib.get("rev")) or (
            context.toolchain != latest.toolchain
        )
        seed_root: Path | None = None
        download_bytes: int | None = 0
        complete = True
        if changed:
            blockers.extend(
                self._project_toolchain_blockers(latest.toolchain, installed=toolchain_installed)
            )
            seeds, seed_root = self._project_seed_paths(
                latest.toolchain,
                latest_packages,
                seed_from=Path(seed_from) if seed_from is not None else None,
            )
            if not seeds:
                if self.availability == "local":
                    download_bytes = None
                    complete = False
                    blockers.append("offline update has no exact local Mathlib dependency graph")
                else:
                    acquisition = self.plan_exact(latest.lock, import_roots=("Mathlib",))
                    raw_download = acquisition.get("download_bytes")
                    download_bytes = raw_download if isinstance(raw_download, int) else None
                    complete = acquisition.get("download_bytes_complete") is True
        if not toolchain_installed:
            download_bytes = None
            complete = False
        return ProjectUpdatePlan(
            context.root,
            current_version,
            target_version,
            current_revision,
            str(latest_mathlib.get("rev")),
            context.toolchain,
            latest.toolchain,
            tuple(str(entry["name"]) for entry in latest_packages),
            seed_root,
            download_bytes,
            complete,
            tuple(blockers),
            toolchain_installed,
        )

    def update_project(
        self,
        path: str | os.PathLike[str] = ".",
        *,
        seed_from: str | os.PathLike[str] | None = None,
    ) -> ProjectUpdatePlan:
        """Move an attached TOML project to the latest cataloged Mathlib transactionally."""
        plan = self.plan_project_update(path, seed_from=seed_from)
        if not plan.ready:
            raise ProjectError("project cannot be updated:\n- " + "\n- ".join(plan.blockers))
        if not plan.changed:
            return plan
        context = discover_project(plan.root)
        latest = self._mathlib_catalog_entry("latest")
        latest_packages = latest.lock.manifest["packages"]
        assert isinstance(latest_packages, list)
        self._ensure_project_toolchain(latest.toolchain)
        seeds, _seed_root = self._project_seed_paths(
            latest.toolchain,
            latest_packages,
            seed_from=Path(seed_from) if seed_from is not None else None,
        )
        seed_packages: Path | None = None
        seed_paths: dict[str, Path] | None = seeds or None
        if not seeds:
            if self.availability == "local":
                raise ProjectError(
                    "offline update needs an exact local latest-Mathlib graph; "
                    "run `lean-runtime scan PATH` or pass `--seed-from PROJECT`"
                )
            environment = self.open_exact(latest.lock, import_roots=("Mathlib",))
            raw_dir = latest.lock.manifest.get("packagesDir", ".lake/packages")
            seed_packages = environment.workspace / str(raw_dir)
        lakefile = context.lakefile
        manifest_path = context.current_manifest()
        assert manifest_path is not None
        toolchain_path = context.root / "lean-toolchain"
        marker = context.root / ".lake" / "lean-runtime-attachment.json"
        config = context.root / "lean-runtime.toml"
        originals = {
            lakefile: lakefile.read_bytes(),
            manifest_path: manifest_path.read_bytes(),
            toolchain_path: toolchain_path.read_bytes(),
        }
        optional_originals = {
            path: path.read_bytes() for path in (marker, config) if path.is_file()
        }
        try:
            lakefile.write_text(
                self._lakefile_with_mathlib_revision(
                    lakefile.read_text(encoding="utf-8"), f"v{plan.target_version}"
                ),
                encoding="utf-8",
            )
            current_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            current_manifest["version"] = latest.lock.manifest.get("version", "1.2.0")
            current_manifest["packagesDir"] = latest.lock.manifest.get(
                "packagesDir", ".lake/packages"
            )
            current_manifest["packages"] = self._project_manifest_packages(
                latest_packages, mathlib_version=plan.target_version
            )
            manifest_path.write_text(
                json.dumps(current_manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            toolchain_path.write_text(latest.toolchain + "\n", encoding="utf-8")
            if marker.is_file():
                marker.unlink()
            if project_sharing_enabled(context.root):
                config.unlink()
            result = self.attach_projects(
                context.root,
                seed_packages=seed_packages,
                seed_package_paths=seed_paths,
            )
            if result.failures or not result.results:
                detail = result.failures[0][1] if result.failures else "project was not attachable"
                raise ProjectError(f"updated project shared setup failed: {detail}")
            return plan
        except BaseException:
            for file_path, contents in originals.items():
                file_path.write_bytes(contents)
            for file_path in (marker, config):
                if file_path in optional_originals:
                    file_path.write_bytes(optional_originals[file_path])
                elif file_path.exists():
                    file_path.unlink()
            raise

    def detach_project(self, path: str | os.PathLike[str]) -> AdoptionResult:
        """Materialize independent dependency copies for an attached project."""
        context = discover_project(path)
        return self.project_adopter.detach(
            context,
            probe=lambda overrides: self._probe_project_graph(context, overrides),
        )

    def plan_project_detachment(self, path: str | os.PathLike[str]) -> DetachmentPlan:
        """Report the independent-copy cost without changing the project."""
        return plan_detachment(Path(path))

    @staticmethod
    def _mathlib_catalog_entry(version: str = "latest") -> Any:
        from .discovery.defaults import default_catalog

        entries = [
            entry
            for entry in default_catalog().entries
            if entry.id.startswith("mathlib-v") and "mathlib" in entry.package_names
        ]
        if not entries:
            raise ProjectError("bundled catalog contains no Mathlib project template")

        def version_key(entry: Any) -> tuple[int, ...]:
            value = entry.id.removeprefix("mathlib-v")
            return tuple(int(part) for part in value.split(".") if part.isdigit())

        if version == "latest":
            return max(entries, key=version_key)
        requested = version.removeprefix("v")
        matching = next((entry for entry in entries if entry.id == f"mathlib-v{requested}"), None)
        if matching is not None:
            return matching
        available = ", ".join(
            entry.id.removeprefix("mathlib-v") for entry in sorted(entries, key=version_key)
        )
        raise ProjectError(
            f"Mathlib {requested} is not in the bundled catalog; available: {available}"
        )

    def _explicit_seed_roots(self, seed_from: Path | None) -> tuple[Path, ...]:
        if seed_from is None:
            return ()
        selected = seed_from.expanduser().resolve()
        roots: tuple[Path, ...]
        try:
            roots = (discover_project(selected).root,)
        except ProjectError:
            roots = discover_shareable_projects(selected, recursive=True)
        if not roots:
            raise ProjectError(f"no pinned Lake projects found under seed path: {selected}")
        for root in roots:
            self.shared_projects.remember_project(discover_project(root))
        return roots

    def _project_seed_paths(
        self,
        toolchain: str,
        packages: list[dict[str, Any]],
        *,
        seed_from: Path | None,
    ) -> tuple[dict[str, Path], Path | None]:
        required = {str(entry["name"]) for entry in packages if entry.get("type") == "git"}
        managed = self.shared_projects.graph_seeds(toolchain, packages)
        if set(managed) == required:
            return managed, self.home / "project-packages"
        roots = self._explicit_seed_roots(seed_from)
        registered, root = self.shared_projects.registered_graph_seeds(
            toolchain, packages, roots=roots
        )
        if set(registered) == required:
            return registered, root
        return {}, None

    @staticmethod
    def _validate_new_project_target(target: Path) -> tuple[Path, ...]:
        """Validate metadata that can be preserved around an atomic project init."""
        if not target.exists():
            return ()
        if not target.is_dir():
            raise ProjectError(f"initialization target is not a directory: {target}")
        entries = tuple(target.iterdir())

        def compatible(entry: Path) -> bool:
            name = entry.name
            return (
                name in {".git", ".github", ".gitignore", "AGENTS.md"}
                or name == "README"
                or name.startswith("README.")
                or name == "LICENSE"
                or name.startswith("LICENSE.")
            )

        unsupported = [entry for entry in entries if not compatible(entry)]
        if unsupported:
            names = ", ".join(sorted(entry.name for entry in unsupported))
            raise ProjectError(
                f"initialization target is not empty: {target} ({names}); choose an empty "
                "directory or run `lean-runtime init .` inside an existing Lake project"
            )
        return entries

    def plan_project_init(
        self,
        path: str | os.PathLike[str] = ".",
        *,
        name: str | None = None,
        mathlib: str | None = "latest",
        toolchain: str | None = None,
        seed_from: str | os.PathLike[str] | None = None,
    ) -> ProjectInitPlan:
        """Plan project creation or adoption without changing the target."""
        target = Path(path).expanduser().resolve()
        if (target / "lakefile.toml").is_file() or (target / "lakefile.lean").is_file():
            if name is not None:
                lakefile = target / "lakefile.toml"
                if not lakefile.is_file():
                    raise ProjectError(
                        "cannot verify --name for an existing lakefile.lean project; omit it"
                    )
                try:
                    with lakefile.open("rb") as stream:
                        configured_name = tomllib.load(stream).get("name")
                except (OSError, tomllib.TOMLDecodeError) as exc:
                    raise ProjectError(f"could not verify existing project --name: {exc}") from exc
                if configured_name != name:
                    raise ProjectError(
                        f"existing project is named {configured_name!r}, not {name!r}"
                    )
            adoption = self.plan_project_adoption(target).projects[0]
            installed = (
                self._toolchain_installed(adoption.toolchain)
                if adoption.toolchain is not None
                else False
            )
            blockers = tuple(adoption.blockers) + self._project_toolchain_blockers(
                adoption.toolchain or "unknown", installed=installed
            )
            return ProjectInitPlan(
                target,
                "adopt",
                adoption.toolchain or "unknown",
                None,
                adoption.packages,
                None,
                0,
                True,
                adoption.attached,
                installed,
                None,
                blockers,
            )
        self._validate_new_project_target(target)
        project_name = name or target.name
        if name is not None and (
            not name
            or not (name[0].isalpha() or name[0] == "_")
            or any(not (character.isalnum() or character == "_") for character in name)
        ):
            raise ProjectError("project --name must be a Lean identifier")
        selected = self._mathlib_catalog_entry(mathlib or "latest")
        selected_toolchain = normalize_toolchain(toolchain or selected.toolchain)
        if mathlib is not None and selected_toolchain != selected.toolchain:
            raise ProjectError(
                f"Mathlib {selected.id.removeprefix('mathlib-v')} requires "
                f"{selected.toolchain}, not {selected_toolchain}"
            )
        if mathlib is None:
            installed = self._toolchain_installed(selected_toolchain)
            blockers = self._project_toolchain_blockers(selected_toolchain, installed=installed)
            return ProjectInitPlan(
                target,
                "create",
                selected_toolchain,
                None,
                (),
                None,
                0 if installed else None,
                installed,
                False,
                installed,
                project_name,
                blockers,
            )
        selected_packages = selected.lock.manifest["packages"]
        assert isinstance(selected_packages, list)
        seed_paths, seed_root = self._project_seed_paths(
            selected_toolchain,
            selected_packages,
            seed_from=Path(seed_from) if seed_from is not None else None,
        )
        toolchain_installed = self._toolchain_installed(selected_toolchain)
        init_blockers = list(
            self._project_toolchain_blockers(
                selected_toolchain,
                installed=toolchain_installed,
            )
        )
        if seed_paths:
            download_bytes: int | None = 0
            complete = True
        elif self.availability == "local":
            download_bytes = None
            complete = False
            init_blockers.append("offline setup has no exact local Mathlib dependency graph")
        else:
            acquisition = self.plan_exact(selected.lock, import_roots=("Mathlib",))
            raw_download = acquisition.get("download_bytes")
            download_bytes = raw_download if isinstance(raw_download, int) else None
            complete = acquisition.get("download_bytes_complete") is True
        if not toolchain_installed:
            download_bytes = None
            complete = False
        return ProjectInitPlan(
            target,
            "create",
            selected_toolchain,
            selected.id.removeprefix("mathlib-v"),
            tuple(str(entry["name"]) for entry in selected_packages),
            seed_root,
            download_bytes,
            complete,
            False,
            toolchain_installed,
            project_name,
            tuple(init_blockers),
        )

    def init_project(
        self,
        path: str | os.PathLike[str] = ".",
        *,
        name: str | None = None,
        mathlib: str | None = "latest",
        toolchain: str | None = None,
        agents: bool = True,
        ci: bool = False,
        seed_from: str | os.PathLike[str] | None = None,
    ) -> AdoptionResult:
        """Create or adopt a project; new projects become visible only when complete."""
        target = Path(path).expanduser().resolve()
        if (target / "lakefile.toml").is_file() or (target / "lakefile.lean").is_file():
            if ci:
                raise ProjectError("init --ci is only available while creating a new project")
            self._ensure_project_toolchain(discover_project(target).toolchain)
            result = self.attach_projects(target)
            if result.failures or not result.results:
                detail = result.failures[0][1] if result.failures else "project was not attachable"
                raise ProjectError(f"existing project shared setup failed: {detail}")
            agents_file = target / "AGENTS.md"
            if agents and not agents_file.exists():
                agents_file.write_text(_DEFAULT_AGENTS_GUIDE, encoding="utf-8")
            return result.results[0]

        plan = self.plan_project_init(
            target,
            name=name,
            mathlib=mathlib,
            toolchain=toolchain,
            seed_from=seed_from,
        )
        selected = self._mathlib_catalog_entry(mathlib or "latest")
        selected_packages = selected.lock.manifest["packages"]
        assert isinstance(selected_packages, list)
        self._ensure_project_toolchain(plan.toolchain)
        seed_package_paths: dict[str, Path] | None = None
        seed_packages: Path | None = None
        if mathlib is not None:
            seed_package_paths, _seed_root = self._project_seed_paths(
                plan.toolchain,
                selected_packages,
                seed_from=Path(seed_from) if seed_from is not None else None,
            )
            if not seed_package_paths:
                if self.availability == "local":
                    raise ProjectError(
                        "offline initialization needs an exact local Mathlib graph; "
                        "run `lean-runtime scan PATH` or pass `--seed-from PROJECT`"
                    )
                environment = self.open_exact(selected.lock, import_roots=("Mathlib",))
                raw_packages = selected.lock.manifest.get("packagesDir", ".lake/packages")
                seed_packages = environment.workspace / str(raw_packages)
                seed_package_paths = None
        target.parent.mkdir(parents=True, exist_ok=True)
        existing_entries = self._validate_new_project_target(target)
        staging = Path(
            tempfile.mkdtemp(prefix=f".{target.name}.lean-runtime-init-", dir=target.parent)
        )
        published = False
        original_git = next((entry for entry in existing_entries if entry.name == ".git"), None)
        published_in_place = False
        published_entries: list[Path] = []
        modified_existing: dict[Path, bytes] = {}
        try:
            custom_agents = target / "AGENTS.md"
            if custom_agents.is_file():
                (staging / "AGENTS.md").write_bytes(custom_agents.read_bytes())
            command = self.toolchains.command(
                plan.toolchain,
                "lake",
                "--offline",
                f"--dir={staging}",
                "init",
                plan.project_name or target.name,
                "lib",
            )
            process = subprocess.run(
                command,
                cwd=staging,
                env=self.toolchains.environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            if process.returncode:
                detail = process.stdout.strip()
                raise ProjectError(
                    "Lake could not initialize the project" + (f":\n{detail}" if detail else "")
                )
            for existing in existing_entries:
                if existing.name in {".git", "AGENTS.md"}:
                    continue
                destination = staging / existing.name
                if existing.name == ".gitignore" and destination.is_file():
                    generated = destination.read_text(encoding="utf-8").splitlines()
                    preserved = existing.read_text(encoding="utf-8").splitlines()
                    merged = list(dict.fromkeys([*preserved, *generated]))
                    destination.write_text("\n".join(merged) + "\n", encoding="utf-8")
                elif existing.is_dir():
                    if destination.exists():
                        remove_tree(destination)
                    shutil.copytree(existing, destination, symlinks=True)
                else:
                    shutil.copy2(existing, destination, follow_symlinks=False)
            if self.lake_cache.capabilities(plan.toolchain).supported:
                lakefile = staging / "lakefile.toml"
                lakefile.write_text(
                    "enableArtifactCache = true\n" + lakefile.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
            update_command = self.toolchains.command(
                plan.toolchain, "lake", "--offline", f"--dir={staging}", "update"
            )
            updated = subprocess.run(
                update_command,
                cwd=staging,
                env=self.toolchains.environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            manifest_path = staging / "lake-manifest.json"
            if updated.returncode or not manifest_path.is_file():
                detail = updated.stdout.strip()
                raise ProjectError(
                    "Lake could not create the root manifest identity"
                    + (f":\n{detail}" if detail else "")
                )
            loaded_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(loaded_manifest, dict):
                raise ProjectError("Lake created a malformed root manifest")
            manifest: dict[str, Any] = loaded_manifest
            if mathlib is not None:
                version = selected.id.removeprefix("mathlib-v")
                manifest["version"] = selected.lock.manifest.get("version", "1.2.0")
                manifest["packagesDir"] = selected.lock.manifest.get(
                    "packagesDir", ".lake/packages"
                )
                manifest["packages"] = self._project_manifest_packages(
                    selected_packages, mathlib_version=version
                )
                lakefile = staging / "lakefile.toml"
                with lakefile.open("a", encoding="utf-8") as stream:
                    stream.write(
                        "\n[[require]]\n"
                        'name = "mathlib"\n'
                        'git = "https://github.com/leanprover-community/mathlib4.git"\n'
                        f'rev = "v{version}"\n'
                    )
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            result = self.attach_projects(
                staging,
                seed_packages=seed_packages,
                seed_package_paths=seed_package_paths,
                display_name=plan.project_name or target.name,
            )
            if result.failures or not result.results:
                detail = result.failures[0][1] if result.failures else "project was not attachable"
                raise ProjectError(f"project shared setup failed: {detail}")
            agents_file = staging / "AGENTS.md"
            if agents and not agents_file.exists():
                agents_file.write_text(_DEFAULT_AGENTS_GUIDE, encoding="utf-8")
            if ci:
                workflow = staging / ".github" / "workflows" / "lean-runtime.yml"
                if workflow.exists():
                    raise ProjectError(f"CI workflow already exists: {workflow}")
                workflow.parent.mkdir(parents=True, exist_ok=True)
                workflow.write_text(
                    project_check_workflow(runtime_version=distribution_version("lean-runtime")),
                    encoding="utf-8",
                )
            if original_git is not None:
                generated_git = staging / ".git"
                if generated_git.is_dir() and not generated_git.is_symlink():
                    remove_tree(generated_git)
                elif generated_git.exists() or generated_git.is_symlink():
                    generated_git.unlink()
            if target.exists():
                # Keep an existing directory inode alive. Replacing the directory itself
                # leaves a shell that invoked `lean-runtime init .` inside an unlinked cwd.
                # Staging has already been fully initialized, attached, and probed, so only
                # its children need a small rollback-safe publication transaction here.
                published_in_place = True
                for entry in sorted(staging.iterdir(), key=lambda path: path.name):
                    destination = target / entry.name
                    if destination.exists() or destination.is_symlink():
                        if entry.name in {
                            ".github",
                            ".gitignore",
                            "AGENTS.md",
                            "README",
                            "README.md",
                            "LICENSE",
                        } or entry.name.startswith(("README.", "LICENSE.")):
                            if entry.name == ".gitignore" and destination.is_file():
                                modified_existing[destination] = destination.read_bytes()
                                destination.write_bytes(entry.read_bytes())
                            if entry.is_symlink() or entry.is_file():
                                entry.unlink()
                            elif entry.exists():
                                remove_tree(entry)
                            continue
                        raise ProjectError(
                            f"initialization target changed while publishing: {destination}"
                        )
                    entry.replace(destination)
                    published_entries.append(destination)
            else:
                staging.replace(target)
            published = True
            context = discover_project(target)
            # Registry discovery is an optimization, not part of publishing the project.
            with suppress(OSError, EnvironmentError):
                self.shared_projects.remember_project(context)
            return replace(result.results[0], root=target)
        except BaseException:
            if published_in_place:
                for entry in reversed(published_entries):
                    if entry.is_symlink() or entry.is_file():
                        entry.unlink()
                    elif entry.exists():
                        remove_tree(entry)
                for destination, contents in modified_existing.items():
                    destination.write_bytes(contents)
            elif published and target.exists():
                remove_tree(target)
            raise
        finally:
            if staging.exists():
                remove_tree(staging)

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
        self.shared_projects.remember_project(context)
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
        """Freeze a clean project available from its Git origin into an exact lock."""
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
        roots = tuple(
            module
            for line in lock.root_module.splitlines()
            if line.strip().startswith("import ")
            for module in line.strip().removeprefix("import ").split()
        )
        return self.bundles.export_capsule(environment.id, Path(output), roots=roots)

    def clean(
        self,
        *,
        dry_run: bool = True,
        minimum_age_seconds: float = 2_592_000,
        keep_last: int = 0,
    ) -> CleanupReport:
        return self.store.clean(
            dry_run=dry_run,
            minimum_age_seconds=minimum_age_seconds,
            keep_last=keep_last,
        )

    def clean_scratch(
        self, *, dry_run: bool = True, minimum_age_seconds: float = 3600
    ) -> CleanupReport:
        """Reclaim abandoned disposable execution and resolution workspaces."""
        return self.store.clean_scratch(dry_run=dry_run, minimum_age_seconds=minimum_age_seconds)

    def clean_downloads(
        self, *, dry_run: bool = True, minimum_age_seconds: float = 2_592_000
    ) -> DownloadCleanupReport:
        return self.store.clean_downloads(dry_run=dry_run, minimum_age_seconds=minimum_age_seconds)

    def doctor(self) -> DoctorReport:
        return diagnose(self.toolchains, self.store)

    def doctor_fix(self) -> DoctorReport:
        return repair(self.toolchains, self.store)

    def store_status(self, *, verify: bool = False) -> StoreStatus:
        return self.store.status(verify=verify)

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
            raise ToolchainError(
                "check requires an environment, explicit toolchain, or pinned project; "
                "use `lean-runtime run FILE` for standalone context discovery"
            )
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
        return self.project_executor.check_file(context, source, policy=policy, cancel=cancel)

    def _check_project_source(
        self,
        context: ProjectContext,
        source: str,
        *,
        filename: str,
        policy: ExecutionPolicy,
        cancel: threading.Event | None = None,
    ) -> ExecutionResult:
        return self.project_executor.check_source(
            context,
            source,
            filename=filename,
            policy=policy,
            cancel=cancel,
        )

    def _build_project(
        self,
        context: ProjectContext,
        *,
        targets: Sequence[str],
        policy: ExecutionPolicy,
        cancel: threading.Event | None = None,
        shared: bool | None = None,
    ) -> ExecutionResult:
        return self.project_executor.build(
            context,
            targets=targets,
            policy=policy,
            cancel=cancel,
            shared=shared,
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
        packages: Sequence[PackageProvenance] = (),
        logical_command: Sequence[str] | None = None,
        path_map: Mapping[str, str] | None = None,
        environment: Mapping[str, str] | None = None,
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
            environment=dict(environment)
            if environment is not None
            else self.toolchains.environment,
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
            packages=tuple(packages),
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
