"""High-level API for reproducible Lean environments and raw project execution."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .backends import Backend, LocalBackend
from .diagnostics import error_diagnostic, parse_diagnostics
from .environments import Environment, EnvironmentManager, ExecutionCapture
from .errors import ProjectError, ToolchainError
from .lockfiles import EnvironmentLock
from .models import ExecutionProvenance, ExecutionResult
from .policies import ExecutionPolicy
from .resolver import EnvironmentResolver
from .serialization import sha256_id, sha256_text
from .specs import EnvironmentSpec, GitPackage
from .store import EnvironmentStore, GarbageCollectionReport, platform_record
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
    ) -> None:
        self.toolchains = toolchains or ToolchainManager(home)
        self.home = self.toolchains.home
        self.backend = backend or LocalBackend()
        self.store = EnvironmentStore(self.home)
        self.resolver = EnvironmentResolver(self.toolchains, self.store)
        self.environments = EnvironmentManager(self.store, self.toolchains, self.backend)

    def resolve(self, spec: EnvironmentSpec, *, timeout: float = 900) -> EnvironmentLock:
        return self.resolver.resolve(spec, timeout=timeout)

    def ensure(
        self,
        lock: EnvironmentLock,
        *,
        name: str | None = None,
        build_profile: str = "release",
    ) -> Environment:
        return self.environments.ensure(lock, name=name, build_profile=build_profile)

    def open(self, identifier: str) -> Environment:
        """Open a published environment without resolution or network access."""
        return self.environments.open(identifier)

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

    def check(
        self,
        source: str,
        *,
        environment: EnvironmentReference | None = None,
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
            toolchain=toolchain,
            project=project,
            timeout=timeout,
            policy=policy,
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

    def replay_capture(self, capture: ExecutionCapture | str | os.PathLike[str]) -> ExecutionResult:
        """Materialize a capture's lock if needed and replay its check request."""
        resolved = (
            ExecutionCapture.load(capture) if isinstance(capture, (str, os.PathLike)) else capture
        )
        if resolved.operation != "check" or len(resolved.files) != 1:
            raise ProjectError("the initial replay API supports one-file check captures")
        environment = self.ensure(resolved.lock)
        filename, source = next(iter(resolved.files.items()))
        return environment.check(source, filename=filename, policy=resolved.policy)

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
        execution_id = sha256_id(
            "execution",
            {
                "environment_id": None,
                "toolchain": toolchain,
                "command": list(command[3:]),
                "source_digest": source_digest,
                "policy": policy.to_dict(),
                "backend": self.backend.name,
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
