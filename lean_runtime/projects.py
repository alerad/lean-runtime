"""Discovery and execution handles for mutable local Lake projects."""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import ProjectError
from .models import ExecutionResult, ProjectProvenance
from .policies import ExecutionPolicy
from .store import source_snapshot_digest
from .toolchains import normalize_toolchain

if TYPE_CHECKING:
    from .runtime import Runtime


def _file_digest(path: Path) -> str | None:
    if not path.is_file():
        return None
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _git(root: Path, *arguments: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


@dataclass(frozen=True, slots=True)
class ProjectContext:
    root: Path
    toolchain: str
    lakefile: Path
    manifest: Path | None

    def current_manifest(self) -> Path | None:
        path = self.root / "lake-manifest.json"
        return path if path.is_file() else None

    def provenance(self) -> ProjectProvenance:
        manifest = self.current_manifest()
        revision = _git(self.root, "rev-parse", "HEAD")
        status = _git(self.root, "status", "--porcelain", "--untracked-files=normal")
        return ProjectProvenance(
            root=str(self.root),
            workspace_digest=source_snapshot_digest(self.root),
            lakefile_digest=_file_digest(self.lakefile) or "",
            manifest_digest=_file_digest(manifest) if manifest is not None else None,
            git_revision=revision,
            git_dirty=bool(status) if status is not None else None,
        )


def discover_project(path: str | os.PathLike[str]) -> ProjectContext:
    """Find the nearest pinned Lake project containing ``path``."""
    selected = Path(path).expanduser().resolve()
    if not selected.exists():
        raise ProjectError(f"project path does not exist: {selected}")
    start = selected if selected.is_dir() else selected.parent
    for root in (start, *start.parents):
        lakefiles = [
            candidate
            for name in ("lakefile.toml", "lakefile.lean")
            if (candidate := root / name).is_file()
        ]
        toolchain = root / "lean-toolchain"
        if lakefiles and toolchain.is_file():
            manifest = root / "lake-manifest.json"
            return ProjectContext(
                root,
                normalize_toolchain(toolchain.read_text(encoding="utf-8")),
                lakefiles[0],
                manifest if manifest.is_file() else None,
            )
    raise ProjectError(f"no pinned Lake project found containing: {selected}")


class ProjectEnvironment:
    """A handle to one trusted, mutable local Lake project."""

    def __init__(self, runtime: Runtime, context: ProjectContext) -> None:
        self.runtime = runtime
        self.context = context
        self.root = context.root
        self.toolchain = context.toolchain

    def inspect(self) -> dict[str, Any]:
        return {
            "kind": "local-project",
            "root": str(self.root),
            "toolchain": self.toolchain,
            "lakefile": self.context.lakefile.name,
            "manifest": (
                str(manifest) if (manifest := self.context.current_manifest()) is not None else None
            ),
            "provenance": self.context.provenance().to_dict(),
        }

    def check_file(
        self,
        path: str | os.PathLike[str],
        *,
        policy: ExecutionPolicy | None = None,
        cancel: threading.Event | None = None,
    ) -> ExecutionResult:
        source = Path(path).expanduser().resolve()
        try:
            source.relative_to(self.root)
        except ValueError as exc:
            raise ProjectError(f"Lean file is outside the project root: {source}") from exc
        if not source.is_file() or source.suffix != ".lean":
            raise ProjectError(f"project check requires an existing .lean file: {source}")
        return self.runtime._check_project_file(
            self.context, source, policy=policy or ExecutionPolicy(), cancel=cancel
        )

    def check(
        self,
        source: str,
        *,
        filename: str = "Main.lean",
        policy: ExecutionPolicy | None = None,
        cancel: threading.Event | None = None,
    ) -> ExecutionResult:
        return self.runtime._check_project_source(
            self.context,
            source,
            filename=filename,
            policy=policy or ExecutionPolicy(),
            cancel=cancel,
        )

    async def check_async(
        self,
        source: str,
        *,
        filename: str = "Main.lean",
        policy: ExecutionPolicy | None = None,
    ) -> ExecutionResult:
        cancel = threading.Event()
        task = asyncio.create_task(
            asyncio.to_thread(self.check, source, filename=filename, policy=policy, cancel=cancel)
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancel.set()
            await task
            raise

    def check_many(
        self,
        sources: Sequence[str],
        *,
        concurrency: int = 4,
        policy: ExecutionPolicy | None = None,
        cancel: threading.Event | None = None,
    ) -> tuple[ExecutionResult, ...]:
        if concurrency < 1:
            raise ValueError("concurrency must be positive")
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [
                executor.submit(self.check, source, policy=policy, cancel=cancel)
                for source in sources
            ]
            return tuple(future.result() for future in futures)

    async def check_many_async(
        self,
        sources: Sequence[str],
        *,
        concurrency: int = 4,
        policy: ExecutionPolicy | None = None,
        cancel: threading.Event | None = None,
    ) -> tuple[ExecutionResult, ...]:
        if concurrency < 1:
            raise ValueError("concurrency must be positive")
        semaphore = asyncio.Semaphore(concurrency)

        async def check_one(source: str) -> ExecutionResult:
            async with semaphore:
                if cancel is not None and cancel.is_set():
                    raise asyncio.CancelledError
                return await self.check_async(source, policy=policy)

        return tuple(await asyncio.gather(*(check_one(source) for source in sources)))

    def build(
        self,
        targets: Sequence[str] = (),
        *,
        policy: ExecutionPolicy | None = None,
        cancel: threading.Event | None = None,
    ) -> ExecutionResult:
        return self.runtime._build_project(
            self.context,
            targets=targets,
            policy=policy or ExecutionPolicy(timeout_seconds=900, max_output_bytes=10_000_000),
            cancel=cancel,
        )
