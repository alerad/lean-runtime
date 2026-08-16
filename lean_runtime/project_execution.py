"""Execution boundary for trusted, mutable Lake projects.

Lake remains authoritative for project builds.  This service only selects the
resolved toolchain and shared package workspace, then records the execution
through :class:`Runtime`.  Keeping this policy outside the high-level facade
gives project checks and builds one place to integrate Lake facilities such as
its artifact cache without changing the public ``Runtime`` API.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised by the Python 3.10 CI job
    import tomli as tomllib

from .errors import ProjectError
from .models import ExecutionResult, PhaseTiming
from .policies import ExecutionPolicy
from .project_sharing import project_sharing_enabled
from .projects import ProjectContext
from .serialization import sha256_text

_MISSING_MODULE = re.compile(
    r"object file .*? of module [`'\"]?(?P<module>[A-Za-z_][A-Za-z0-9_'.]*)[`'\"]? "
    r"does not exist"
)
_MODULE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*")
_IMPORT = re.compile(r"^\s*(?:public\s+)?import\s+(.+?)\s*$", re.MULTILINE)


def _source_imports(source: str) -> tuple[str, ...]:
    imports: list[str] = []
    for match in _IMPORT.finditer(source):
        for token in match.group(1).split():
            if _MODULE_NAME.fullmatch(token) is not None and token not in imports:
                imports.append(token)
    return tuple(imports)


def _local_module_exists(root: Path, module: str) -> bool:
    relative = Path(*module.split(".")).with_suffix(".lean")
    if (root / relative).is_file():
        return True
    for current, directories, filenames in os.walk(root):
        directories[:] = [name for name in directories if name not in {".git", ".lake"}]
        if relative.name not in filenames:
            continue
        candidate = Path(current) / relative.name
        if candidate.parts[-len(relative.parts) :] == relative.parts:
            return True
    return False


if TYPE_CHECKING:
    from .runtime import Runtime


def _snapshot_suspect(selected: Sequence[str], result: ExecutionResult) -> bool:
    """Decide whether a failed check plausibly failed because of a header snapshot."""
    snapshot_paths = [
        argument.split("=", 1)[1]
        for argument in selected
        if argument.startswith(("--incr-load=", "--incr-header-save="))
    ]
    if not snapshot_paths or result.ok or result.cancelled:
        return False
    if result.timed_out:
        return True
    output = "\n".join(
        (result.stdout, result.stderr, *(item.message for item in result.diagnostics))
    )
    return any(path in output for path in snapshot_paths)


class ProjectExecutor:
    """Check and build local projects through one compatibility-preserving seam."""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    def _checked_with_header_snapshots(
        self,
        context: ProjectContext,
        workspace_digest: str,
        module: str,
        source: str,
        command: Sequence[str],
        execute: Callable[[list[str]], ExecutionResult],
        cancel: threading.Event | None,
    ) -> ExecutionResult:
        """Run one check through the header cache, falling back to a plain check once."""
        cache = self.runtime.header_cache
        entered = time.monotonic()
        coordination_ms = 0
        with cache.command(
            context.toolchain, workspace_digest, module, source, command, cancel=cancel
        ) as selected:
            coordination_ms = round((time.monotonic() - entered) * 1000)
            result = execute(selected)
        snapshot_timing = PhaseTiming("header_snapshot", coordination_ms)
        if selected != list(command):
            result = replace(result, timings=(snapshot_timing, *result.timings))
        if not _snapshot_suspect(selected, result):
            return result
        cache.discard(context.toolchain, workspace_digest, module, source)
        self.runtime.events.emit(
            "project.header_snapshot_discarded",
            f"Retrying without header snapshot: {module}",
            phase="check",
            module=module,
        )
        retried = execute(list(command))
        return replace(
            retried,
            elapsed_seconds=result.elapsed_seconds + retried.elapsed_seconds,
            timings=(snapshot_timing, *retried.timings),
        )

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

        def execute(selected_command: list[str]) -> ExecutionResult:
            return self.runtime._raw_result(
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

        def run() -> ExecutionResult:
            return self._checked_with_header_snapshots(
                context, provenance.workspace_digest, relative, text, command, execute, cancel
            )

        result = self._build_missing_local_import(context, text, run(), run, policy, cancel)
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

            def execute(selected_command: list[str]) -> ExecutionResult:
                return self.runtime._raw_result(
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

            def run() -> ExecutionResult:
                return self._checked_with_header_snapshots(
                    context,
                    provenance.workspace_digest,
                    safe_filename,
                    source,
                    command,
                    execute,
                    cancel,
                )

            result = self._build_missing_local_import(context, source, run(), run, policy, cancel)
            return self._with_identifier_hints(context, result)

    def _build_missing_local_import(
        self,
        context: ProjectContext,
        source: str,
        result: ExecutionResult,
        retry: Callable[[], ExecutionResult],
        policy: ExecutionPolicy,
        cancel: threading.Event | None,
    ) -> ExecutionResult:
        """Let Lake materialize one missing local import, then retry the check once."""
        if result.ok or result.cancelled:
            return result
        output = "\n".join(
            (result.stdout, result.stderr, *(item.message for item in result.diagnostics))
        )
        candidates = [match.group("module") for match in _MISSING_MODULE.finditer(output)]
        if "unknown module prefix" in output or (
            "failed to open file" in output and ".olean" in output and "No such file" in output
        ):
            candidates.extend(_source_imports(source))
        modules: list[str] = []
        for module in candidates:
            if _local_module_exists(context.root, module) and module not in modules:
                modules.append(module)
        if not modules:
            return result
        self.runtime.events.emit(
            "project.check_dependency_build_started",
            f"Building missing local import artifacts: {', '.join(modules)}",
            phase="build",
            modules=modules,
        )
        built = self.build(
            context,
            targets=tuple(f"{module}:leanArts" for module in modules),
            policy=policy,
            cancel=cancel,
        )
        preparation_seconds = result.elapsed_seconds + built.elapsed_seconds
        preparation_timing = PhaseTiming("build", round(preparation_seconds * 1000))
        if not built.ok:
            return replace(
                built,
                elapsed_seconds=preparation_seconds,
                timings=(preparation_timing,),
            )
        retried = retry()
        return replace(
            retried,
            elapsed_seconds=preparation_seconds + retried.elapsed_seconds,
            timings=(preparation_timing, *retried.timings),
        )

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
        lock_started = time.monotonic()
        with self.runtime.shared_projects.build_lock(workspace, cancel=cancel):
            waited_ms = round((time.monotonic() - lock_started) * 1000)
            result = run()
        return replace(result, timings=(PhaseTiming("workspace_lock", waited_ms), *result.timings))

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
