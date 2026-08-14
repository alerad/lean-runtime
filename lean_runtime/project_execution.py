"""Execution boundary for trusted, mutable Lake projects.

Lake remains authoritative for project builds.  This service only selects the
resolved toolchain and shared package workspace, then records the execution
through :class:`Runtime`.  Keeping this policy outside the high-level facade
gives project checks and builds one place to integrate Lake facilities such as
its artifact cache without changing the public ``Runtime`` API.
"""

from __future__ import annotations

import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

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
        return self.runtime._raw_result(
            command,
            cwd=context.root,
            toolchain=context.toolchain,
            source_digest=sha256_text(source.read_text(encoding="utf-8")),
            policy=policy,
            project=context.provenance(),
            packages=context.package_provenance(),
            logical_command=("lake", "env", "lean", relative),
            cancel=cancel,
        )

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
            return self.runtime._raw_result(
                command,
                cwd=context.root,
                toolchain=context.toolchain,
                source_digest=sha256_text(source),
                policy=policy,
                project=context.provenance(),
                packages=context.package_provenance(),
                logical_command=("lake", "env", "lean", safe_filename),
                path_map={relative: safe_filename, str(source_path): safe_filename},
                cancel=cancel,
            )

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
                cancel=cancel,
            )

        if workspace is None:
            return run()
        with self.runtime.shared_projects.build_lock(workspace, cancel=cancel):
            return run()
