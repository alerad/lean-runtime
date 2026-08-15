"""Execution boundary for trusted, mutable Lake projects.

Lake remains authoritative for project builds.  This service only selects the
resolved toolchain and shared package workspace, then records the execution
through :class:`Runtime`.  Keeping this policy outside the high-level facade
gives project checks and builds one place to integrate Lake facilities such as
its artifact cache without changing the public ``Runtime`` API.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised by the Python 3.10 CI job
    import tomli as tomllib

from .errors import ProjectError
from .models import ExecutionResult
from .policies import ExecutionPolicy
from .project_sharing import project_sharing_enabled
from .projects import ProjectContext
from .serialization import sha256_text

if TYPE_CHECKING:
    from .runtime import Runtime


class ProjectExecutor:
    """Check and build local projects through one compatibility-preserving seam."""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    def check_file(
        self,
        context: ProjectContext,
        source: Path,
        *,
        policy: ExecutionPolicy,
        cancel: threading.Event | None = None,
    ) -> ExecutionResult:
        relative = source.relative_to(context.root).as_posix()
        command = self.runtime.toolchains.command(
            context.toolchain, "lake", "env", "lean", relative
        )
        text = source.read_text(encoding="utf-8")
        provenance = context.provenance()
        with self.runtime.header_cache.command(
            context.toolchain, provenance.workspace_digest, text, command
        ) as selected_command:
            result = self.runtime._raw_result(
                selected_command,
                cwd=context.root,
                toolchain=context.toolchain,
                source_digest=sha256_text(text),
                policy=policy,
                project=provenance,
                packages=context.package_provenance(),
                logical_command=("lake", "env", "lean", relative),
                cancel=cancel,
            )
        return self._with_identifier_hints(context, result)

    def check_source(
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
        with tempfile.TemporaryDirectory(prefix="check-", dir=jobs) as temporary:
            source_path = Path(temporary) / safe_filename
            source_path.write_text(source, encoding="utf-8")
            relative = source_path.relative_to(context.root).as_posix()
            command = self.runtime.toolchains.command(
                context.toolchain, "lake", "env", "lean", relative
            )
            provenance = context.provenance()
            with self.runtime.header_cache.command(
                context.toolchain, provenance.workspace_digest, source, command
            ) as selected_command:
                result = self.runtime._raw_result(
                    selected_command,
                    cwd=context.root,
                    toolchain=context.toolchain,
                    source_digest=sha256_text(source),
                    policy=policy,
                    project=provenance,
                    packages=context.package_provenance(),
                    logical_command=("lake", "env", "lean", safe_filename),
                    path_map={relative: safe_filename, str(source_path): safe_filename},
                    cancel=cancel,
                )
            return self._with_identifier_hints(context, result)

    def _with_identifier_hints(
        self, context: ProjectContext, result: ExecutionResult
    ) -> ExecutionResult:
        hints = self.runtime.identifier_resolver.suggestions(context, result)
        return result if not hints else replace(result, hints=hints)

    def build(
        self,
        context: ProjectContext,
        *,
        targets: Sequence[str],
        policy: ExecutionPolicy,
        cancel: threading.Event | None = None,
        shared: bool | None = None,
    ) -> ExecutionResult:
        selected_shared = project_sharing_enabled(context.root) if shared is None else shared
        workspace = (
            self.runtime.shared_projects.prepare(context, cancel=cancel)
            if selected_shared
            else None
        )
        lake_arguments = (
            (f"--packages={workspace.overrides_file}", "build", *targets)
            if workspace is not None
            else ("build", *targets)
        )
        command = self.runtime.toolchains.command(context.toolchain, "lake", *lake_arguments)
        environment = self.runtime.lake_cache.environment(context)

        def run() -> ExecutionResult:
            return self.runtime._raw_result(
                command,
                cwd=context.root,
                toolchain=context.toolchain,
                source_digest=sha256_text(""),
                policy=policy,
                project=context.provenance(),
                packages=context.package_provenance(),
                logical_command=(
                    "lake",
                    "build",
                    *(("--shared",) if selected_shared else ()),
                    *targets,
                ),
                environment=environment,
                cancel=cancel,
            )

        if workspace is None:
            return run()
        with self.runtime.shared_projects.build_lock(workspace, cancel=cancel):
            return run()

    def check_project(
        self,
        context: ProjectContext,
        *,
        policy: ExecutionPolicy,
        cancel: threading.Event | None = None,
    ) -> ExecutionResult:
        """Build local library oleans; Lake supplies ordering and module expansion."""
        libraries = self._local_libraries(context)
        if not libraries:
            raise ProjectError("project declares no local Lean libraries to check")
        targets = tuple(f"@/{name}:leanArts" for name in libraries)
        return self.build(context, targets=targets, policy=policy, cancel=cancel)

    def _local_libraries(self, context: ProjectContext) -> tuple[str, ...]:
        """Ask Lake to normalize either configuration language, then read target names."""
        self.runtime.home.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="lake-config-", dir=self.runtime.home) as raw:
            output = Path(raw) / "lakefile.toml"
            command = self.runtime.toolchains.command(
                context.toolchain, "lake", "translate-config", "toml", str(output)
            )
            process = subprocess.run(
                command,
                cwd=context.root,
                env=self.runtime.toolchains.environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=120,
                check=False,
            )
            if process.returncode or not output.is_file():
                detail = process.stdout.strip()
                raise ProjectError(
                    "Lake could not enumerate local libraries" + (f":\n{detail}" if detail else "")
                )
            with output.open("rb") as stream:
                value = tomllib.load(stream)
        libraries = value.get("lean_lib", [])
        names = tuple(
            str(library["name"])
            for library in libraries
            if isinstance(library, dict) and isinstance(library.get("name"), str)
        )
        return tuple(dict.fromkeys(names))
