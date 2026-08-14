"""Content-addressed dependency workspaces for mutable Lake projects."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ._git import git_command
from ._paths import remove_tree
from .errors import ProjectError
from .events import EventEmitter
from .locking import FileLock
from .projects import ProjectContext
from .serialization import sha256_id, write_json_atomic
from .store import clone_tree, platform_compatibility, source_snapshot_digest

SHARED_PROJECT_SCHEMA = "lean-runtime-shared-project/1"
_PACKAGE_ID_PATTERN = re.compile(r"project_package_[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class SharedProjectWorkspace:
    """Lake package overrides backed by one exact, shared dependency set."""

    workspace_id: str
    root: Path
    overrides_file: Path
    reused: bool
    packages: tuple[str, ...]
    package_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SHARED_PROJECT_SCHEMA,
            "workspace_id": self.workspace_id,
            "root": str(self.root),
            "overrides_file": str(self.overrides_file),
            "reused": self.reused,
            "packages": list(self.packages),
            "package_ids": list(self.package_ids),
        }


def _load_manifest(context: ProjectContext) -> dict[str, Any]:
    manifest_path = context.current_manifest()
    if manifest_path is None:
        raise ProjectError(
            "shared project builds require lake-manifest.json; run `lake update` once to "
            "lock the dependency graph, then retry"
        )
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectError(f"could not read Lake manifest: {exc}") from exc
    if not isinstance(value, dict):
        raise ProjectError("Lake manifest must be a JSON object")
    manifest = cast(dict[str, Any], value)
    packages = manifest.get("packages")
    if not isinstance(packages, list):
        raise ProjectError("Lake manifest has no package entries")
    for entry in packages:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise ProjectError("Lake manifest contains a malformed package entry")
        if entry.get("type") not in {"git", "path"}:
            raise ProjectError(
                f"shared project builds do not support package source type {entry.get('type')!r}"
            )
    return manifest


def _resolved_path_entries(
    context: ProjectContext, packages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    identity: list[dict[str, Any]] = []
    for entry in packages:
        normalized = dict(entry)
        if entry.get("type") == "path":
            raw = entry.get("dir")
            if not isinstance(raw, str):
                raise ProjectError(f"path dependency {entry['name']!r} has no directory")
            path = (context.root / raw).resolve()
            if not path.is_dir():
                raise ProjectError(f"path dependency {entry['name']!r} does not exist: {path}")
            normalized["dir"] = str(path)
            normalized["content_digest"] = source_snapshot_digest(path)
        identity.append(normalized)
    return identity


def _entry_identity(entry: dict[str, Any]) -> dict[str, Any]:
    """Keep only fields that can alter a materialized package or its name."""
    keys = (
        "name",
        "scope",
        "type",
        "url",
        "rev",
        "subDir",
        "dir",
        "configFile",
        "manifestFile",
        "content_digest",
    )
    return {key: entry[key] for key in keys if key in entry}


def _package_identity(
    *,
    context: ProjectContext,
    entry: dict[str, Any],
    source_package: Path,
    effective_entries: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    manifest_name = entry.get("manifestFile", "lake-manifest.json")
    manifest_path = source_package / str(manifest_name)
    dependency_names: set[str] | None = None
    package_manifest: Any = None
    if manifest_path.is_file():
        try:
            package_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProjectError(
                f"could not read dependency manifest {manifest_path}: {exc}"
            ) from exc
        own_entries = (
            package_manifest.get("packages") if isinstance(package_manifest, dict) else None
        )
        if isinstance(own_entries, list):
            dependency_names = {
                str(item["name"])
                for item in own_entries
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            }
    if dependency_names is None:
        # A package without its own manifest gets the full root graph as a conservative key.
        dependencies = list(effective_entries.values())
    else:
        dependencies = [
            effective_entries[name]
            for name in sorted(dependency_names)
            if name in effective_entries
        ]
    return {
        "schema": SHARED_PROJECT_SCHEMA,
        "toolchain": context.toolchain,
        "platform": platform_compatibility(),
        "package": _entry_identity(entry),
        "effective_dependencies": dependencies,
        "package_manifest": package_manifest,
    }


def _git_head(path: Path) -> str | None:
    result = subprocess.run(
        git_command("-C", str(path), "rev-parse", "HEAD"),
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _git_clean(path: Path) -> bool:
    result = subprocess.run(
        git_command("-C", str(path), "status", "--porcelain", "--untracked-files=normal"),
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and not result.stdout.strip()


def _git_has_commit(path: Path, revision: str) -> bool:
    result = subprocess.run(
        git_command("-C", str(path), "cat-file", "-e", f"{revision}^{{commit}}"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _git_remote(path: Path) -> str | None:
    result = subprocess.run(
        git_command("-C", str(path), "config", "--get", "remote.origin.url"),
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _run_git(arguments: list[str], *, purpose: str) -> None:
    result = subprocess.run(
        git_command(*arguments),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode:
        detail = result.stdout.strip()
        raise ProjectError(f"Git failed while {purpose}" + (f":\n{detail}" if detail else ""))


def _valid_package_marker(package: Path, package_id: str) -> bool:
    try:
        marker = json.loads((package / ".lean-runtime-package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(marker, dict) and sha256_id("project_package", marker) == package_id


class SharedProjectManager:
    """Prepare and lock reusable Lake dependency workspaces."""

    def __init__(self, home: Path, events: EventEmitter) -> None:
        self.home = home
        self.events = events
        self.root = home / "project-workspaces"
        self.packages = home / "project-packages"
        self.sources = home / "project-sources"
        self.locks = home / "locks"

    def _object_donor(self, url: str, revision: str, seed: Path | None) -> Path | None:
        candidates = [seed] if seed is not None else []
        if self.sources.is_dir():
            candidates.extend(path for path in self.sources.iterdir() if path.is_dir())
        for candidate in candidates:
            if (
                candidate is not None
                and _git_remote(candidate) == url
                and _git_has_commit(candidate, revision)
            ):
                return candidate
        return None

    def _source_checkout(
        self,
        *,
        url: str,
        revision: str,
        seed: Path | None,
        cancel: threading.Event | None = None,
    ) -> Path:
        source_id = sha256_id("project_source", {"url": url, "revision": revision})
        destination = self.sources / source_id
        with FileLock(self.locks / f"{source_id}.lock", timeout=1800, cancel=cancel):
            if (
                destination.is_dir()
                and _git_head(destination) == revision
                and _git_clean(destination)
            ):
                return destination
            self.sources.mkdir(parents=True, exist_ok=True)
            staging = self.sources / f".{source_id}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            try:
                if (
                    seed is not None
                    and seed.is_dir()
                    and _git_head(seed) == revision
                    and _git_clean(seed)
                ):
                    self.events.emit(
                        "project.shared.source_started",
                        f"Importing cached source for {url}",
                        phase="shared-project",
                        source=url,
                    )
                    clone_tree(seed, staging)
                    build = staging / ".lake"
                    if build.exists():
                        remove_tree(build)
                elif (donor := self._object_donor(url, revision, seed)) is not None:
                    self.events.emit(
                        "project.shared.source_started",
                        f"Reusing local Git objects for {url}",
                        phase="shared-project",
                        source=url,
                    )
                    _run_git(
                        ["clone", "--quiet", "--no-checkout", "--local", str(donor), str(staging)],
                        purpose=f"cloning local objects for {url}",
                    )
                    _run_git(
                        ["-C", str(staging), "checkout", "--quiet", "--detach", revision],
                        purpose=f"checking out {revision}",
                    )
                else:
                    self.events.emit(
                        "project.shared.source_started",
                        f"Fetching exact project dependency from {url}",
                        phase="shared-project",
                        source=url,
                    )
                    staging.mkdir()
                    _run_git(["-C", str(staging), "init", "--quiet"], purpose="initializing Git")
                    _run_git(
                        ["-C", str(staging), "remote", "add", "origin", url],
                        purpose=f"configuring {url}",
                    )
                    _run_git(
                        ["-C", str(staging), "fetch", "--depth", "1", "origin", revision],
                        purpose=f"fetching {revision}",
                    )
                    _run_git(
                        ["-C", str(staging), "checkout", "--quiet", "--detach", "FETCH_HEAD"],
                        purpose=f"checking out {revision}",
                    )
                if _git_head(staging) != revision:
                    raise ProjectError(f"dependency checkout did not resolve to {revision}")
                if destination.exists():
                    remove_tree(destination)
                staging.replace(destination)
            except BaseException:
                if staging.exists():
                    remove_tree(staging)
                raise
        return destination

    def prepare(
        self,
        context: ProjectContext,
        *,
        cancel: threading.Event | None = None,
    ) -> SharedProjectWorkspace:
        manifest = _load_manifest(context)
        packages = manifest["packages"]
        identity_packages = _resolved_path_entries(context, packages)
        identity = {
            "schema": SHARED_PROJECT_SCHEMA,
            "toolchain": context.toolchain,
            "platform": platform_compatibility(),
            "packages": identity_packages,
        }
        workspace_id = sha256_id("project_workspace", identity)
        destination = self.root / workspace_id
        overrides_file = destination / "package-overrides.json"
        package_names = tuple(str(entry["name"]) for entry in packages)
        with FileLock(self.locks / f"{workspace_id}.lock", timeout=1800, cancel=cancel):
            if overrides_file.is_file():
                try:
                    workspace_record = json.loads(
                        (destination / "workspace.json").read_text(encoding="utf-8")
                    )
                    ready_package_ids = tuple(
                        str(value) for value in workspace_record["package_ids"]
                    )
                    if all(
                        _PACKAGE_ID_PATTERN.fullmatch(package_id) is not None
                        for package_id in ready_package_ids
                    ) and all(
                        _valid_package_marker(self.packages / package_id, package_id)
                        for package_id in ready_package_ids
                    ):
                        return SharedProjectWorkspace(
                            workspace_id,
                            destination,
                            overrides_file,
                            True,
                            package_names,
                            ready_package_ids,
                        )
                except (OSError, json.JSONDecodeError, KeyError, TypeError):
                    pass
                remove_tree(destination)
            self.events.emit(
                "project.shared.workspace_started",
                f"Preparing shared dependencies for {context.root.name}",
                phase="shared-project",
                workspace_id=workspace_id,
                packages=len(packages),
            )
            self.root.mkdir(parents=True, exist_ok=True)
            staging = self.root / f".{workspace_id}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            try:
                local_packages = context.root / str(manifest.get("packagesDir", ".lake/packages"))
                overrides: list[dict[str, Any]] = []
                package_ids: list[str] = []
                effective_entries = {
                    str(entry["name"]): _entry_identity(identity_entry)
                    for entry, identity_entry in zip(packages, identity_packages, strict=True)
                }
                for entry in packages:
                    override = {
                        key: entry[key]
                        for key in ("name", "scope", "inherited", "configFile", "manifestFile")
                        if key in entry
                    }
                    override.setdefault("inherited", False)
                    if entry["type"] == "path":
                        override.update(
                            type="path", dir=str((context.root / str(entry["dir"])).resolve())
                        )
                    else:
                        url = entry.get("url")
                        revision = entry.get("rev")
                        if not isinstance(url, str) or not isinstance(revision, str):
                            raise ProjectError(
                                f"git dependency {entry['name']!r} has no exact URL and revision"
                            )
                        local = local_packages / str(entry["name"])
                        source = self._source_checkout(
                            url=url,
                            revision=revision,
                            seed=local if local.is_dir() else None,
                            cancel=cancel,
                        )
                        subdir = entry.get("subDir")
                        source_package = (
                            source / subdir if isinstance(subdir, str) and subdir else source
                        )
                        package_identity = _package_identity(
                            context=context,
                            entry=entry,
                            source_package=source_package,
                            effective_entries=effective_entries,
                        )
                        package_id = sha256_id("project_package", package_identity)
                        package_ids.append(package_id)
                        final_target = self.packages / package_id
                        with FileLock(
                            self.locks / f"{package_id}.lock", timeout=1800, cancel=cancel
                        ):
                            if final_target.is_dir() and not _valid_package_marker(
                                final_target, package_id
                            ):
                                remove_tree(final_target)
                            if not final_target.is_dir():
                                self.packages.mkdir(parents=True, exist_ok=True)
                                package_staging = self.packages / (
                                    f".{package_id}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
                                )
                                try:
                                    # Preserve compatible local artifacts on first import. CoW
                                    # cloning prevents later writes from mutating the donor.
                                    donor = (
                                        local
                                        if local.is_dir()
                                        and _git_head(local) == revision
                                        and _git_clean(local)
                                        else source
                                    )
                                    clone_tree(donor, package_staging)
                                    write_json_atomic(
                                        package_staging / ".lean-runtime-package.json",
                                        package_identity,
                                    )
                                    package_staging.replace(final_target)
                                except BaseException:
                                    if package_staging.exists():
                                        remove_tree(package_staging)
                                    raise
                        package_dir = (
                            final_target / subdir
                            if isinstance(subdir, str) and subdir
                            else final_target
                        )
                        override.update(type="path", dir=str(package_dir))
                    overrides.append(override)
                write_json_atomic(
                    staging / "package-overrides.json",
                    {"version": manifest.get("version", "1.0.0"), "packages": overrides},
                )
                write_json_atomic(
                    staging / "workspace.json", {**identity, "package_ids": package_ids}
                )
                if destination.exists():
                    remove_tree(destination)
                staging.replace(destination)
            except BaseException:
                if staging.exists():
                    remove_tree(staging)
                raise
        self.events.emit(
            "project.shared.workspace_ready",
            f"Shared dependency workspace ready for {context.root.name}",
            phase="shared-project",
            workspace_id=workspace_id,
            packages=len(packages),
        )
        return SharedProjectWorkspace(
            workspace_id, destination, overrides_file, False, package_names, tuple(package_ids)
        )

    @contextmanager
    def build_lock(
        self,
        workspace: SharedProjectWorkspace,
        *,
        cancel: threading.Event | None = None,
    ) -> Any:
        """Serialize builds that may update any shared dependency artifacts."""
        with ExitStack() as stack:
            for package_id in sorted(set(workspace.package_ids)):
                stack.enter_context(
                    FileLock(self.locks / f"{package_id}-build.lock", timeout=1800, cancel=cancel)
                )
            yield
