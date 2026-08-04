"""High-level API for reproducible Lean environments and raw project execution."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audit import AuditReport, audit_environment
from .backends import Backend, LocalBackend
from .bundles import BundleInfo, EnvironmentBundles
from .diagnostics import error_diagnostic, parse_diagnostics
from .environments import Environment, EnvironmentManager, ExecutionCapture
from .errors import (
    EnvironmentError,
    PrebuiltUnavailable,
    ProjectError,
    SpecificationError,
    ToolchainError,
)
from .events import EventCallback, EventEmitter
from .health import DoctorReport, diagnose
from .lockfiles import EnvironmentLock
from .models import ExecutionProvenance, ExecutionResult
from .oci import (
    DEFAULT_CACHE_REPOSITORIES,
    OCIEnvironmentCache,
    OCIEnvironmentPublisher,
    OCIRepository,
    PublishInfo,
)
from .policies import ExecutionPolicy
from .references import PackageReference, discover_package, normalize_references
from .resolver import EnvironmentResolver
from .serialization import sha256_id, sha256_text
from .signatures import CosignVerifier
from .specs import EnvironmentSpec, GitPackage
from .store import (
    BlobGarbageCollectionReport,
    EnvironmentStore,
    GarbageCollectionReport,
    StoreStatus,
    environment_identity,
    platform_record,
)
from .toolchains import ToolchainManager, normalize_toolchain

EnvironmentReference = Environment | EnvironmentSpec | EnvironmentLock | str


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
        prebuilt: str | None = None,
        caches: Sequence[str] | None = None,
        signatures: str = "ignore",
        trusted_identity: str | None = None,
        trusted_issuer: str | None = None,
        cosign: str | os.PathLike[str] = "cosign",
    ) -> None:
        prebuilt = prebuilt or os.environ.get("LEAN_RUNTIME_PREBUILT", "auto")
        if prebuilt not in {"auto", "require", "never"}:
            raise ValueError("prebuilt must be 'auto', 'require', or 'never'")
        if signatures not in {"ignore", "require"}:
            raise ValueError("signatures must be 'ignore' or 'require'")
        if signatures == "require" and (not trusted_identity or not trusted_issuer):
            raise ValueError("required signatures need trusted_identity and trusted_issuer")
        self.toolchains = toolchains or ToolchainManager(home)
        self.home = self.toolchains.home
        self.backend = backend or LocalBackend()
        self.events = EventEmitter(on_event)
        self.store = EnvironmentStore(self.home)
        self.resolver = EnvironmentResolver(self.toolchains, self.store, self.events)
        self.environments = EnvironmentManager(
            self.store, self.toolchains, self.backend, self.events
        )
        self.bundles = EnvironmentBundles(self.store, self.toolchains, self.backend, self.events)
        configured_caches = caches
        if configured_caches is None:
            configured = os.environ.get("LEAN_RUNTIME_CACHES")
            configured_caches = (
                tuple(item.strip() for item in configured.split(",") if item.strip())
                if configured is not None
                else DEFAULT_CACHE_REPOSITORIES
            )
        self.prebuilt = prebuilt
        self.cosign_executable = cosign
        self.signature_verifier = (
            CosignVerifier(trusted_identity, trusted_issuer, executable=cosign)
            if signatures == "require"
            else None
        )
        self.caches = tuple(
            OCIEnvironmentCache(
                OCIRepository.parse(value),
                self.store,
                self.bundles,
                self.events,
                self.signature_verifier,
            )
            for value in configured_caches
        )

    def resolve(self, spec: EnvironmentSpec, *, timeout: float = 900) -> EnvironmentLock:
        return self.resolver.resolve(spec, timeout=timeout)

    def ensure(
        self,
        lock: EnvironmentLock,
        *,
        name: str | None = None,
        build_profile: str = "release",
    ) -> Environment:
        environment_id = environment_identity(lock, build_profile)
        destination = self.store.environment_path(environment_id)
        if not destination.is_dir() and self.prebuilt != "never":
            imported = False
            for cache in self.caches:
                try:
                    cache.pull(lock, name=name)
                    imported = True
                    break
                except PrebuiltUnavailable as exc:
                    self.events.emit(
                        "prebuilt.fallback",
                        "Prebuilt environment is unavailable",
                        registry=cache.repository.display,
                        reason=str(exc),
                    )
            if not imported and self.prebuilt == "require":
                if not self.caches:
                    raise EnvironmentError(
                        "prebuilt environments are required but no caches configured"
                    )
                raise EnvironmentError("no configured cache provided a compatible environment")
        return self.environments.ensure(lock, name=name, build_profile=build_profile)

    def open(self, identifier: str) -> Environment:
        """Open a published environment without resolution or network access."""
        return self.environments.open(identifier)

    def export_environment(self, identifier: str, output: str | os.PathLike[str]) -> BundleInfo:
        """Export a published environment as a deterministic OCI layout archive."""
        environment = self.open(identifier)
        return self.bundles.export(environment.id, Path(output))

    def import_environment(
        self,
        bundle: str | os.PathLike[str],
        *,
        name: str | None = None,
        probe: bool = True,
    ) -> Environment:
        """Verify and atomically import a local OCI environment bundle."""
        info = self.bundles.import_bundle(Path(bundle), name=name, probe=probe)
        return self.open(info.environment_id)

    def publish_environment(
        self,
        identifier: str,
        repository: str,
        *,
        tags: Sequence[str] = (),
        finalize: bool = True,
        sign: bool = False,
        attest: bool = False,
    ) -> PublishInfo:
        """Publish a built environment and its lock-level OCI index."""
        environment = self.open(identifier)
        publisher = OCIEnvironmentPublisher(
            OCIRepository.parse(repository), self.store, self.bundles, self.events
        )
        result = publisher.publish(environment.id, tags=tuple(tags), finalize=finalize)
        if sign:
            if result.index_digest is None:
                raise ValueError("platform-only publishing cannot sign a lock index")
            CosignVerifier(executable=self.cosign_executable).sign(
                publisher.repository, result.index_digest
            )
        if attest:
            report = self.audit(environment.id)
            CosignVerifier(executable=self.cosign_executable).attest(
                publisher.repository,
                result.index_digest or result.manifest_digest,
                report.to_dict(),
            )
        return result

    def audit(self, identifier: str, *, rebuild: bool = False) -> AuditReport:
        """Verify locked sources and optionally compare an independent source rebuild."""
        return audit_environment(
            self.open(identifier), self.toolchains, self.backend, self.events, rebuild=rebuild
        )

    def publish_environment_index(
        self,
        repository: str,
        lock_id: str,
        platform_descriptors: Sequence[dict[str, Any]],
        *,
        tags: Sequence[str] = (),
    ) -> str:
        """Finalize a deterministic multi-platform lock index."""
        publisher = OCIEnvironmentPublisher(
            OCIRepository.parse(repository), self.store, self.bundles, self.events
        )
        return publisher.publish_index(lock_id, list(platform_descriptors), tags=tuple(tags))

    def create_environment(
        self,
        name: str,
        *,
        toolchain: str,
        packages: Sequence[GitPackage],
        timeout: float = 900,
    ) -> Environment:
        spec = EnvironmentSpec(toolchain, tuple(packages))
        return self.ensure(self.resolve(spec, timeout=timeout), name=name)

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

    def resolve_references(
        self,
        packages: Sequence[str | PackageReference],
        *,
        toolchain: str | None = None,
        timeout: float = 900,
    ) -> EnvironmentLock:
        """Discover package references and resolve their exact Lake graph."""
        return self.resolve(
            self.spec_from_references(packages, toolchain=toolchain), timeout=timeout
        )

    def ensure_references(
        self,
        packages: Sequence[str | PackageReference],
        *,
        toolchain: str | None = None,
        name: str | None = None,
        timeout: float = 900,
    ) -> Environment:
        """Build or reopen the environment described by package references."""
        return self.ensure(
            self.resolve_references(packages, toolchain=toolchain, timeout=timeout), name=name
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
    ) -> ExecutionResult:
        """Check source in a content-addressed environment or a raw toolchain/project."""
        selected_policy = policy or ExecutionPolicy(timeout_seconds=timeout or 120)
        if timeout is not None and policy is not None:
            selected_policy = replace(policy, timeout_seconds=timeout)
        if environment is not None and packages:
            raise SpecificationError("check cannot combine environment= with packages=")
        if project is not None and packages:
            raise SpecificationError("check cannot combine project= with packages=")
        if packages:
            resolved = self.ensure_references(packages, toolchain=toolchain)
            return resolved.check(source, filename=filename, policy=selected_policy)
        if environment is not None:
            resolved = self._environment(environment)
            return resolved.check(source, filename=filename, policy=selected_policy)
        return self._raw_check(
            source,
            toolchain=toolchain,
            project=project,
            filename=filename,
            policy=selected_policy,
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
    ) -> ExecutionResult:
        source_path = Path(path).expanduser().resolve()
        return self.check(
            source_path.read_text(encoding="utf-8"),
            filename=source_path.name,
            environment=environment,
            packages=packages,
            toolchain=toolchain,
            project=project,
            timeout=timeout,
            policy=policy,
        )

    def check_files(
        self,
        files: Mapping[str, str],
        *,
        entrypoint: str = "Main.lean",
        environment: EnvironmentReference,
        policy: ExecutionPolicy | None = None,
    ) -> ExecutionResult:
        """Check a multi-file request in a managed environment."""
        return self._environment(environment).check_files(
            files, entrypoint=entrypoint, policy=policy
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
        root = Path(project).expanduser().resolve()
        if not root.is_dir():
            raise ProjectError(f"project directory does not exist: {root}")
        selected = normalize_toolchain(toolchain) if toolchain else project_toolchain(root)
        command = self.toolchains.command(selected, "lake", "build", *targets)
        return self._raw_result(
            command,
            cwd=root,
            toolchain=selected,
            source_digest=sha256_text(""),
            policy=ExecutionPolicy(timeout_seconds=timeout, max_output_bytes=10_000_000),
        )

    def gc(
        self, *, dry_run: bool = True, minimum_age_seconds: float = 2_592_000
    ) -> GarbageCollectionReport:
        return self.store.gc(dry_run=dry_run, minimum_age_seconds=minimum_age_seconds)

    def gc_oci_blobs(
        self, *, dry_run: bool = True, minimum_age_seconds: float = 2_592_000
    ) -> BlobGarbageCollectionReport:
        return self.store.gc_oci_blobs(dry_run=dry_run, minimum_age_seconds=minimum_age_seconds)

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
        environment = self.ensure(resolved.lock)
        return environment.check_files(
            resolved.files,
            entrypoint=resolved.entrypoint,
            policy=resolved.policy,
        )

    def _environment(self, value: EnvironmentReference) -> Environment:
        if isinstance(value, Environment):
            return value
        if isinstance(value, EnvironmentSpec):
            return self.ensure(self.resolve(value))
        if isinstance(value, EnvironmentLock):
            return self.ensure(value)
        return self.open(value)

    def _raw_check(
        self,
        source: str,
        *,
        toolchain: str | None,
        project: str | os.PathLike[str] | None,
        filename: str,
        policy: ExecutionPolicy,
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
        with tempfile.TemporaryDirectory(prefix="raw-check-", dir=self.store.jobs) as raw:
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
            )

    def _raw_result(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        toolchain: str,
        source_digest: str,
        policy: ExecutionPolicy,
    ) -> ExecutionResult:
        started_at = datetime.now(timezone.utc).isoformat()
        logical_command = list(command[3:])
        if source_digest != sha256_text("") and logical_command:
            logical_command[-1] = Path(logical_command[-1]).name
        request_digest = sha256_id(
            "request",
            {
                "environment_id": None,
                "toolchain": toolchain,
                "command": logical_command,
                "source_digest": source_digest,
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
        )
        output = "\n".join(part for part in (raw.stdout, raw.stderr) if part)
        diagnostics = parse_diagnostics(output)
        if raw.timed_out:
            diagnostics += (error_diagnostic("Lean execution exceeded its time limit"),)
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
        )
