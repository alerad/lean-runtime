"""Small batteries-included entry points over the explicit Runtime API."""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Sequence
from pathlib import Path

from .environments import Environment, ExecutionCapture
from .errors import SpecificationError
from .lockfiles import EnvironmentLock
from .matrix import MatrixContext, MatrixResult
from .models import ExecutionResult
from .policies import ExecutionPolicy
from .projects import ProjectEnvironment
from .references import PackageReference
from .runtime import Runtime

PreparedEnvironment = Environment | ProjectEnvironment
DependencyInput = str | PackageReference | Sequence[str | PackageReference]
_default_lock = threading.Lock()
_default: Runtime | None = None


def default_runtime() -> Runtime:
    """Return the process-wide runtime, creating it only on first use."""
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = Runtime()
    return _default


def setup(
    deps: DependencyInput | None = None,
    *,
    project: str | os.PathLike[str] | None = None,
    lock: EnvironmentLock | str | os.PathLike[str] | None = None,
    environment: Environment | str | None = None,
    name: str | None = None,
    toolchain: str | None = None,
    runtime: Runtime | None = None,
    cancel: threading.Event | None = None,
) -> PreparedEnvironment:
    """Prepare exactly one dependency, lock, named environment, or local-project context."""
    selected = runtime or default_runtime()
    contexts = sum(
        (
            deps is not None,
            project is not None,
            lock is not None,
            environment is not None,
        )
    )
    if contexts != 1:
        raise SpecificationError(
            "setup requires exactly one of deps, project, lock, or environment"
        )
    if deps is not None:
        normalized_deps = (deps,) if isinstance(deps, (str, PackageReference)) else tuple(deps)
        if not normalized_deps:
            raise SpecificationError("setup deps must contain at least one dependency")
        return selected.ensure_references(
            normalized_deps, toolchain=toolchain, name=name, cancel=cancel
        )
    if project is not None:
        if name is not None:
            raise SpecificationError("local projects cannot be assigned environment aliases")
        return selected.project(project, toolchain=toolchain)
    if lock is not None:
        if toolchain is not None:
            raise SpecificationError("an exact lock cannot be combined with a toolchain override")
        resolved = (
            EnvironmentLock.load(Path(lock)) if isinstance(lock, (str, os.PathLike)) else lock
        )
        return selected.ensure(resolved, name=name, cancel=cancel)
    if toolchain is not None or name is not None:
        raise SpecificationError("opened environments do not accept name or toolchain overrides")
    return environment if isinstance(environment, Environment) else selected.open(str(environment))


def check(
    source: str,
    *,
    deps: DependencyInput | None = None,
    project: str | os.PathLike[str] | None = None,
    lock: EnvironmentLock | str | os.PathLike[str] | None = None,
    environment: Environment | str | None = None,
    toolchain: str | None = None,
    filename: str = "Main.lean",
    policy: ExecutionPolicy | None = None,
    runtime: Runtime | None = None,
    cancel: threading.Event | None = None,
) -> ExecutionResult:
    """Check one source string, preparing its declared context when necessary."""
    selected = runtime or default_runtime()
    if all(value is None for value in (deps, project, lock, environment)):
        return selected.check(
            source,
            toolchain=toolchain,
            filename=filename,
            policy=policy,
            cancel=cancel,
        )
    prepared = setup(
        deps,
        project=project,
        lock=lock,
        environment=environment,
        toolchain=toolchain,
        runtime=selected,
        cancel=cancel,
    )
    return prepared.check(source, filename=filename, policy=policy, cancel=cancel)


def check_file(
    path: str | os.PathLike[str],
    *,
    deps: DependencyInput | None = None,
    project: str | os.PathLike[str] | None = None,
    lock: EnvironmentLock | str | os.PathLike[str] | None = None,
    environment: Environment | str | None = None,
    toolchain: str | None = None,
    policy: ExecutionPolicy | None = None,
    runtime: Runtime | None = None,
    cancel: threading.Event | None = None,
) -> ExecutionResult:
    """Check a file, automatically discovering a local project when no context is given."""
    selected = runtime or default_runtime()
    source = Path(path).expanduser().resolve()
    if all(value is None for value in (deps, project, lock, environment)):
        return selected.check_file(source, toolchain=toolchain, policy=policy, cancel=cancel)
    prepared = setup(
        deps,
        project=project,
        lock=lock,
        environment=environment,
        toolchain=toolchain,
        runtime=selected,
        cancel=cancel,
    )
    if isinstance(prepared, ProjectEnvironment):
        return prepared.check_file(source, policy=policy, cancel=cancel)
    return prepared.check(
        source.read_text(encoding="utf-8"),
        filename=source.name,
        policy=policy,
        cancel=cancel,
    )


def replay(
    capture: ExecutionCapture | str | os.PathLike[str], *, runtime: Runtime | None = None
) -> ExecutionResult:
    return (runtime or default_runtime()).replay_capture(capture)


def check_matrix(
    source: str,
    *,
    contexts: Sequence[MatrixContext],
    filename: str = "Main.lean",
    concurrency: int = 1,
    runtime: Runtime | None = None,
    cancel: threading.Event | None = None,
) -> MatrixResult:
    """Check one source across named contexts using bounded ordinary executions."""
    return (runtime or default_runtime()).check_matrix(
        source,
        contexts=contexts,
        filename=filename,
        concurrency=concurrency,
        cancel=cancel,
    )


async def check_matrix_async(
    source: str,
    *,
    contexts: Sequence[MatrixContext],
    filename: str = "Main.lean",
    concurrency: int = 1,
    runtime: Runtime | None = None,
) -> MatrixResult:
    cancel = threading.Event()
    task = asyncio.create_task(
        asyncio.to_thread(
            check_matrix,
            source,
            contexts=contexts,
            filename=filename,
            concurrency=concurrency,
            runtime=runtime,
            cancel=cancel,
        )
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancel.set()
        await task
        raise
