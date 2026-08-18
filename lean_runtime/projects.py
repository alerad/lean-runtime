"""Discovery and execution handles for mutable local Lake projects."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised by the Python 3.10 CI job
    import tomli as tomllib

from ._git import git_command
from .errors import ProjectError
from .models import ExecutionResult, PackageProvenance, ProjectProvenance
from .policies import ExecutionPolicy
from .store import source_snapshot_digest
from .toolchains import normalize_toolchain

if TYPE_CHECKING:
    from .runtime import Runtime


_GITHUB_HTTPS = re.compile(r"https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$")
_GITHUB_SSH = re.compile(
    r"(?:git@github\.com:|ssh://git@github\.com/)(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$"
)
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*")


def _file_digest(path: Path) -> str | None:
    if not path.is_file():
        return None
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _git(root: Path, *arguments: str) -> str | None:
    result = subprocess.run(
        git_command("-C", str(root), *arguments),
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _publication_url(value: str, *, root: Path) -> str:
    match = _GITHUB_HTTPS.fullmatch(value) or _GITHUB_SSH.fullmatch(value)
    if match is not None:
        repository = match.group("repo").removesuffix(".git")
        return f"https://github.com/{match.group('owner')}/{repository}.git"
    if not value or value.startswith("-"):
        raise ProjectError("project origin is not a usable Git remote")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme:
        if parsed.scheme not in {"file", "git", "http", "https", "ssh"}:
            raise ProjectError("project origin must use file, git, HTTP(S), or SSH transport")
        if parsed.scheme != "file" and not parsed.netloc:
            raise ProjectError("project origin URL has no host")
        return value
    # SCP-style SSH remotes are understood by Git but not by urlsplit.
    if re.fullmatch(r"[^/@:]+@[^/:]+:.+", value):
        return value
    local = Path(value).expanduser()
    if not local.is_absolute():
        local = root / local
    return local.resolve().as_uri()


def _lake_metadata(path: Path, toolchain: str, runtime: Runtime) -> tuple[str, tuple[str, ...]]:
    lakefile = path / "lakefile.toml"
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if not lakefile.is_file():
        if not (path / "lakefile.lean").is_file():
            raise ProjectError("project has neither lakefile.toml nor lakefile.lean")
        available = getattr(runtime.toolchains, "is_available_locally", lambda _value: False)
        if not available(toolchain):
            raise ProjectError(
                "inspecting lakefile.lean requires its pinned Lean toolchain to be installed; "
                f"run `lean-runtime toolchain install {toolchain}` explicitly, then retry"
            )
        temporary = tempfile.TemporaryDirectory(prefix="lean-runtime-project-")
        lakefile = Path(temporary.name) / "lakefile.toml"
        command = runtime.toolchains.command(
            toolchain, "lake", "translate-config", "toml", str(lakefile)
        )
        try:
            process = subprocess.run(
                command,
                cwd=path,
                env=runtime.toolchains.environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            temporary.cleanup()
            raise ProjectError("Lake timed out while inspecting lakefile.lean") from exc
        if process.returncode or not lakefile.is_file():
            if temporary is not None:
                temporary.cleanup()
            raise ProjectError(
                "Lake could not inspect lakefile.lean"
                + (f":\n{process.stdout.strip()}" if process.stdout.strip() else "")
            )
    try:
        with lakefile.open("rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProjectError(f"could not read project Lake metadata: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.cleanup()
    name = value.get("name")
    libraries = value.get("lean_lib")
    if not isinstance(name, str) or not name:
        raise ProjectError("project Lake configuration has no package name")
    if not isinstance(libraries, list) or not libraries:
        raise ProjectError("project declares no [[lean_lib]] to publish")
    modules: list[str] = []
    for library in libraries:
        if not isinstance(library, dict):
            raise ProjectError("project contains a malformed [[lean_lib]] declaration")
        roots = library.get("roots")
        candidates = roots if isinstance(roots, list) and roots else [library.get("name")]
        for candidate in candidates:
            if (
                not isinstance(candidate, str)
                or not candidate
                or _MODULE.fullmatch(candidate) is None
            ):
                raise ProjectError("project contains a [[lean_lib]] without an importable root")
            modules.append(candidate)
    return name, tuple(dict.fromkeys(modules))


def _remote_contains_commit(url: str, revision: str, directory: Path) -> None:
    """Prove that the exact commit can be acquired from the advertised remote."""
    with tempfile.TemporaryDirectory(prefix="lean-runtime-remote-", dir=directory) as raw:
        process = subprocess.run(
            git_command("init", "--quiet", raw),
            text=True,
            capture_output=True,
            check=False,
        )
        if process.returncode == 0:
            try:
                process = subprocess.run(
                    git_command("-C", raw, "fetch", "--quiet", "--depth", "1", url, revision),
                    text=True,
                    capture_output=True,
                    timeout=120,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ProjectError(
                    "timed out proving that HEAD is available from the configured origin"
                ) from exc
        if process.returncode:
            detail = (process.stdout + process.stderr).strip()
            raise ProjectError(
                f"HEAD {revision[:12]} is not available from origin; push the commit before "
                "publishing an immutable environment" + (f"\nGit: {detail}" if detail else "")
            )


@dataclass(frozen=True, slots=True)
class ProjectPublicationPlan:
    """Read-only assessment of a Git-backed project publication."""

    root: Path
    toolchain: str
    package: str
    modules: tuple[str, ...]
    selected_module: str | None
    repository: str | None
    revision: str | None
    dirty_files: tuple[str, ...]
    blockers: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.blockers

    @property
    def reference(self) -> str | None:
        if self.repository is None or self.revision is None:
            return None
        match = _GITHUB_HTTPS.fullmatch(self.repository)
        if match is None:
            return None
        owner = match.group("owner")
        repository = match.group("repo").removesuffix(".git")
        return f"github:{owner}/{repository}@{self.revision}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "project-publication",
            "root": str(self.root),
            "toolchain": self.toolchain,
            "package": self.package,
            "modules": list(self.modules),
            "selected_module": self.selected_module,
            "repository": self.repository,
            "revision": self.revision,
            "reference": self.reference,
            "dirty_files": list(self.dirty_files),
            "ready": self.ready,
            "blockers": list(self.blockers),
        }

    def require_ready(self) -> ProjectPublicationPlan:
        if self.blockers:
            raise ProjectError("project is not publishable:\n- " + "\n- ".join(self.blockers))
        return self


def inspect_project_publication(
    runtime: Runtime,
    path: str | os.PathLike[str],
    *,
    module: str | None = None,
    check_remote: bool = False,
) -> ProjectPublicationPlan:
    """Assess whether a clean root Git project can become an exact environment."""
    context = discover_project(path)
    root = context.root
    blockers: list[str] = []
    git_root = _git(root, "rev-parse", "--show-toplevel")
    if git_root is None:
        blockers.append("project is not inside a Git repository")
    elif Path(git_root).resolve() != root:
        blockers.append("the Lake project must be at the Git repository root")
    revision = _git(root, "rev-parse", "HEAD")
    if revision is None:
        blockers.append("Git repository has no HEAD commit")
    raw_status = _git(root, "status", "--porcelain", "--untracked-files=all")
    dirty_files = tuple(line[2:].lstrip() for line in raw_status.splitlines()) if raw_status else ()
    if dirty_files:
        blockers.append("checkout is dirty; commit or remove local changes")
    origin = _git(root, "remote", "get-url", "origin")
    repository: str | None = None
    if origin is None:
        blockers.append("Git remote 'origin' is not configured")
    else:
        try:
            repository = _publication_url(origin, root=root)
        except ProjectError as exc:
            blockers.append(str(exc))
    package, modules = _lake_metadata(root, context.toolchain, runtime)
    selected = module
    if selected is None and len(modules) == 1:
        selected = modules[0]
    elif selected is None:
        blockers.append("multiple importable roots found; select one with --module")
    elif selected not in modules:
        blockers.append(f"module {selected!r} is not declared; choose one of: {', '.join(modules)}")
    if check_remote and not blockers and repository is not None and revision is not None:
        runtime.home.mkdir(parents=True, exist_ok=True)
        _remote_contains_commit(repository, revision, runtime.home)
    return ProjectPublicationPlan(
        root,
        context.toolchain,
        package,
        modules,
        selected,
        repository,
        revision,
        dirty_files,
        tuple(blockers),
    )


def project_publication_workflow(*, library: str, module: str) -> str:
    """Return the small caller workflow for the public reusable publisher."""
    if not library.startswith("ghcr.io/") or any(character.isspace() for character in library):
        raise ProjectError("publication library must look like ghcr.io/OWNER/REPOSITORY")
    if not module or _MODULE.fullmatch(module) is None:
        raise ProjectError("publication module must be a Lean module name")
    return f"""name: Publish Lean environment

on:
  workflow_dispatch:
  push:
    tags: ["v*"]

permissions:
  contents: read
  packages: write
  id-token: write

jobs:
  publish:
    uses: alerad/lean-runtime/.github/workflows/publish-project.yml@v4
    with:
      project: .
      library: {library}
      module: {module}
      public: true
    secrets: inherit
"""


def project_check_workflow(*, runtime_version: str) -> str:
    """Return CI that exercises the same manifest and lean-runtime check path as local use."""
    cache_key = (
        "lean-runtime-${{ runner.os }}-${{ hashFiles('lean-toolchain', 'lake-manifest.json') }}"
    )
    return f"""name: Lean Runtime

on:
  push:
  pull_request:

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - uses: actions/setup-python@v6
        with:
          python-version: '3.12'
      - uses: actions/cache@v5
        with:
          path: ${{{{ runner.temp }}}}/lean-runtime
          key: {cache_key}
      - run: python -m pip install lean-runtime=={runtime_version}
      - run: lean-runtime check
        env:
          LEAN_RUNTIME_HOME: ${{{{ runner.temp }}}}/lean-runtime
"""


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
        workspace_id: str | None = None
        attachment = self.root / ".lake" / "lean-runtime-attachment.json"
        try:
            attachment_value = json.loads(attachment.read_text(encoding="utf-8"))
            raw_workspace_id = attachment_value.get("workspace_id")
            if isinstance(raw_workspace_id, str):
                workspace_id = raw_workspace_id
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        return ProjectProvenance(
            root=str(self.root),
            workspace_digest=source_snapshot_digest(self.root),
            lakefile_digest=_file_digest(self.lakefile) or "",
            manifest_digest=_file_digest(manifest) if manifest is not None else None,
            git_revision=revision,
            git_dirty=bool(status) if status is not None else None,
            workspace_id=workspace_id,
        )

    def package_provenance(self) -> tuple[PackageProvenance, ...]:
        """Return the exact Git package graph used by a local project."""
        manifest = self.current_manifest()
        if manifest is None:
            return ()
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
            entries = value.get("packages")
            packages_dir = value.get("packagesDir", ".lake/packages")
        except (OSError, json.JSONDecodeError, AttributeError):
            return ()
        if not isinstance(entries, list) or not isinstance(packages_dir, str):
            return ()
        result: list[PackageProvenance] = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("type") != "git":
                continue
            name = entry.get("name")
            url = entry.get("url")
            revision = entry.get("rev")
            if not all(isinstance(item, str) for item in (name, url, revision)):
                continue
            package = self.root / packages_dir / str(name)
            tree_hash = _git(package, "rev-parse", f"{revision}^{{tree}}") or ""
            result.append(
                PackageProvenance(
                    name=str(name),
                    url=str(url),
                    revision=str(revision),
                    tree_hash=tree_hash,
                )
            )
        return tuple(result)


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

    def check_all(
        self,
        *,
        timeout: float = 900,
        cancel: threading.Event | None = None,
    ) -> ExecutionResult:
        """Check declared local libraries without building executables."""
        return self.runtime.check_project(self.root, timeout=timeout, cancel=cancel)

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
        shared: bool | None = None,
    ) -> ExecutionResult:
        return self.runtime._build_project(
            self.context,
            targets=targets,
            policy=policy or ExecutionPolicy(timeout_seconds=900, max_output_bytes=10_000_000),
            cancel=cancel,
            shared=shared,
        )
