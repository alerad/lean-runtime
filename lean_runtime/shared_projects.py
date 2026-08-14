"""Content-addressed dependency workspaces for mutable Lake projects."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import uuid
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ._git import git_command
from ._paths import remove_tree
from .errors import ProjectError
from .events import EventEmitter
from .locking import FileLock
from .projects import ProjectContext, discover_project
from .serialization import sha256_id, write_json_atomic
from .store import clone_tree, platform_compatibility, source_snapshot_digest

SHARED_PROJECT_SCHEMA = "lean-runtime-shared-project/2"
PROJECT_SEED_REGISTRY_SCHEMA = "lean-runtime-project-seeds/1"
_PACKAGE_ID_PATTERN = re.compile(r"project_package_[0-9a-f]{64}\Z")


def _canonical_git_url(value: str) -> str:
    url = value.strip().rstrip("/")
    if url.startswith("git@github.com:"):
        url = "https://github.com/" + url.removeprefix("git@github.com:")
    elif url.startswith("ssh://git@github.com/"):
        url = "https://github.com/" + url.removeprefix("ssh://git@github.com/")
    return url.removesuffix(".git")


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
        "type",
        "url",
        "rev",
        "subDir",
        "dir",
        "configFile",
        "manifestFile",
        "content_digest",
    )
    identity = {key: entry[key] for key in keys if key in entry}
    url = identity.get("url")
    if entry.get("type") == "git" and isinstance(url, str):
        identity["url"] = _canonical_git_url(url)
    return identity


def _package_subdir(entry: dict[str, Any]) -> Path | None:
    raw = entry.get("subDir")
    if raw in {None, ""}:
        return None
    if not isinstance(raw, str):
        raise ProjectError(f"package {entry.get('name')!r} has a non-string subDir")
    subdir = Path(raw)
    if subdir.is_absolute() or ".." in subdir.parts:
        raise ProjectError(f"package {entry.get('name')!r} has an unsafe subDir: {raw}")
    return subdir


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


def _normalized_package_identity(identity: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize legacy marker spellings without weakening graph compatibility."""
    package = identity.get("package")
    dependencies = identity.get("effective_dependencies")
    if not isinstance(package, dict) or not isinstance(dependencies, list):
        return None
    normalized_dependencies: list[dict[str, Any]] = []
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            return None
        normalized_dependencies.append(_entry_identity(dependency))
    return {
        "toolchain": identity.get("toolchain"),
        "platform": identity.get("platform"),
        "package": _entry_identity(package),
        "effective_dependencies": normalized_dependencies,
        "package_manifest": identity.get("package_manifest"),
    }


def _git_head(path: Path) -> str | None:
    if _git_root(path) != path.resolve():
        return None
    result = subprocess.run(
        git_command("-C", str(path), "rev-parse", "HEAD"),
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _git_clean(path: Path) -> bool:
    if _git_root(path) != path.resolve():
        return False
    result = subprocess.run(
        git_command("-C", str(path), "status", "--porcelain", "--untracked-files=normal"),
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and not result.stdout.strip()


def _git_has_commit(path: Path, revision: str) -> bool:
    if _git_root(path) != path.resolve():
        return False
    result = subprocess.run(
        git_command("-C", str(path), "cat-file", "-e", f"{revision}^{{commit}}"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _git_remote(path: Path) -> str | None:
    if _git_root(path) != path.resolve():
        return None
    result = subprocess.run(
        git_command("-C", str(path), "config", "--get", "remote.origin.url"),
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _git_root(path: Path) -> Path | None:
    result = subprocess.run(
        git_command("-C", str(path), "rev-parse", "--show-toplevel"),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode or not result.stdout.strip():
        return None
    try:
        return Path(result.stdout.strip()).resolve()
    except OSError:
        return None


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


def _has_root_olean(build: Path, package_name: str) -> bool:
    lean_root = build / "lib" / "lean"
    expected = package_name.rsplit(".", 1)[-1].lower() + ".olean"
    try:
        return any(path.name.lower() == expected for path in lean_root.iterdir())
    except OSError:
        return False


class SharedProjectManager:
    """Prepare and lock reusable Lake dependency workspaces."""

    def __init__(self, home: Path, events: EventEmitter) -> None:
        self.home = home
        self.events = events
        self.root = home / "project-workspaces"
        self.packages = home / "project-packages"
        self.sources = home / "project-sources"
        self.locks = home / "locks"
        self.seed_registry = home / "project-seeds.json"

    def remember_project(self, context: ProjectContext) -> None:
        """Remember a Lake project as a future exact dependency seed."""
        manifest = context.current_manifest()
        if manifest is None:
            return
        self.home.mkdir(parents=True, exist_ok=True)
        with FileLock(self.locks / "project-seeds.lock", timeout=30):
            roots: list[str] = []
            try:
                value = json.loads(self.seed_registry.read_text(encoding="utf-8"))
                if (
                    isinstance(value, dict)
                    and value.get("schema") == PROJECT_SEED_REGISTRY_SCHEMA
                    and isinstance(value.get("roots"), list)
                ):
                    roots = [str(item) for item in value["roots"] if isinstance(item, str)]
            except (OSError, json.JSONDecodeError):
                pass
            selected = str(context.root.resolve())
            roots = [root for root in roots if root != selected and Path(root).is_dir()]
            roots.append(selected)
            write_json_atomic(
                self.seed_registry,
                {"schema": PROJECT_SEED_REGISTRY_SCHEMA, "roots": roots},
            )

    def remembered_roots(self) -> tuple[Path, ...]:
        """Return live project roots in most-recently-used order."""
        try:
            value = json.loads(self.seed_registry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ()
        if (
            not isinstance(value, dict)
            or value.get("schema") != PROJECT_SEED_REGISTRY_SCHEMA
            or not isinstance(value.get("roots"), list)
        ):
            return ()
        return tuple(
            Path(item)
            for item in reversed(value["roots"])
            if isinstance(item, str) and Path(item).is_dir()
        )

    def registered_graph_seeds(
        self,
        toolchain: str,
        entries: list[dict[str, Any]],
        *,
        roots: tuple[Path, ...] = (),
    ) -> tuple[dict[str, Path], Path | None]:
        """Find one exact complete graph in registered or explicitly supplied projects."""
        required = {
            str(entry.get("name")): (
                str(entry.get("rev")),
                str(entry.get("subDir") or ""),
                _canonical_git_url(str(entry.get("url"))),
            )
            for entry in entries
            if entry.get("type") == "git"
        }
        candidates = tuple(dict.fromkeys((*roots, *self.remembered_roots())))
        best: dict[str, Path] | None = None
        best_root: Path | None = None
        best_score: tuple[int, int, int] = (-1, -1, -1)
        for root in candidates:
            try:
                context = discover_project(root)
                if context.toolchain != toolchain:
                    continue
                manifest = _load_manifest(context)
            except ProjectError:
                continue
            raw_entries = manifest.get("packages")
            raw_packages_dir = manifest.get("packagesDir", ".lake/packages")
            if not isinstance(raw_entries, list) or not isinstance(raw_packages_dir, str):
                continue
            available = {
                str(entry.get("name")): (
                    str(entry.get("rev")),
                    str(entry.get("subDir") or ""),
                    _canonical_git_url(str(entry.get("url"))),
                )
                for entry in raw_entries
                if isinstance(entry, dict) and entry.get("type") == "git"
            }
            if available != required:
                continue
            package_root = context.root / raw_packages_dir
            selected: dict[str, Path] = {}
            built = 0
            roots_built = 0
            modified = 0
            valid = True
            for name, (revision, subdir, _url) in required.items():
                package = package_root / name
                if package.is_symlink():
                    package = package.resolve()
                marker_id = package.name
                managed = _PACKAGE_ID_PATTERN.fullmatch(
                    marker_id
                ) is not None and _valid_package_marker(package, marker_id)
                if (
                    not package.is_dir()
                    or _git_head(package) != revision
                    or (not managed and not _git_clean(package))
                ):
                    valid = False
                    break
                selected[name] = package
                build = (
                    package / subdir / ".lake" / "build" if subdir else package / ".lake" / "build"
                )
                if build.is_dir():
                    built += 1
                    if _has_root_olean(build, name):
                        roots_built += 1
                    with suppress(OSError):
                        modified = max(modified, build.stat().st_mtime_ns)
            score = (roots_built, built, modified)
            if valid and set(selected) == set(required) and score > best_score:
                best = selected
                best_root = context.root
                best_score = score
        return (best or {}, best_root)

    def graph_seeds(
        self,
        toolchain: str,
        entries: list[dict[str, Any]],
    ) -> dict[str, Path]:
        """Find existing exact shared packages despite older identity spellings."""
        required = {
            str(entry.get("name")): (
                str(entry.get("rev")),
                str(entry.get("subDir") or ""),
                _canonical_git_url(str(entry.get("url"))),
            )
            for entry in entries
            if entry.get("type") == "git"
        }
        # Prefer one complete existing workspace. Lake traces include absolute dependency
        # paths, so mixing individually warm packages from different workspaces can make
        # an otherwise reusable graph rebuild itself.
        coherent: dict[str, Path] | None = None
        coherent_score: tuple[int, int, int] = (-1, -1, -1)
        if self.root.is_dir():
            for record_path in self.root.glob("project_workspace_*/workspace.json"):
                try:
                    record = json.loads(record_path.read_text(encoding="utf-8"))
                    if not isinstance(record, dict):
                        continue
                    workspace_entries = record["packages"]
                    package_ids = record["package_ids"]
                except (OSError, json.JSONDecodeError, KeyError, TypeError):
                    continue
                if (
                    record.get("toolchain") != toolchain
                    or not isinstance(workspace_entries, list)
                    or not isinstance(package_ids, list)
                ):
                    continue
                workspace_required = {
                    str(entry.get("name")): (
                        str(entry.get("rev")),
                        str(entry.get("subDir") or ""),
                        _canonical_git_url(str(entry.get("url"))),
                    )
                    for entry in workspace_entries
                    if isinstance(entry, dict) and entry.get("type") == "git"
                }
                if workspace_required != required or len(package_ids) != len(workspace_entries):
                    continue
                candidate: dict[str, Path] = {}
                roots = 0
                built = 0
                modified = 0
                valid = True
                for entry, package_id in zip(workspace_entries, package_ids, strict=True):
                    if not isinstance(entry, dict) or entry.get("type") != "git":
                        continue
                    package_id = str(package_id)
                    package = self.packages / package_id
                    if not _PACKAGE_ID_PATTERN.fullmatch(package_id) or not _valid_package_marker(
                        package, package_id
                    ):
                        valid = False
                        break
                    name = str(entry.get("name"))
                    candidate[name] = package
                    subdir = str(entry.get("subDir") or "")
                    build = package / subdir / ".lake" / "build"
                    if build.is_dir():
                        built += 1
                        if _has_root_olean(build, name):
                            roots += 1
                        with suppress(OSError):
                            modified = max(modified, build.stat().st_mtime_ns)
                score = (roots, built, modified)
                if valid and set(candidate) == set(required) and score > coherent_score:
                    coherent = candidate
                    coherent_score = score
        if coherent is not None:
            return coherent

        found: dict[str, Path] = {}
        scores: dict[str, tuple[bool, bool, int]] = {}
        for marker in self.packages.glob("project_package_*/.lean-runtime-package.json"):
            try:
                identity = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            package_entry = identity.get("package") if isinstance(identity, dict) else None
            if (
                not isinstance(identity, dict)
                or identity.get("toolchain") != toolchain
                or not isinstance(package_entry, dict)
            ):
                continue
            name = str(package_entry.get("name"))
            key = (
                str(package_entry.get("rev")),
                str(package_entry.get("subDir") or ""),
                _canonical_git_url(str(package_entry.get("url"))),
            )
            if required.get(name) == key:
                package_root = marker.parent / key[1] if key[1] else marker.parent
                build = package_root / ".lake" / "build"
                try:
                    modified = build.stat().st_mtime_ns if build.is_dir() else 0
                except OSError:
                    modified = 0
                score = (_has_root_olean(build, name), build.is_dir(), modified)
                if score > scores.get(name, (False, False, 0)):
                    found[name] = marker.parent
                    scores[name] = score
        return found

    def reusable_packages(
        self,
        context: ProjectContext,
        entries: list[dict[str, Any]],
        *,
        effective_entries: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Path]:
        """Return managed packages whose recorded graph exactly matches this project."""
        if effective_entries is None:
            identity_entries = _resolved_path_entries(context, entries)
            effective_entries = {
                str(entry["name"]): _entry_identity(identity_entry)
                for entry, identity_entry in zip(entries, identity_entries, strict=True)
            }
        reusable: dict[str, Path] = {}
        for name, package in self.graph_seeds(context.toolchain, entries).items():
            package_id = package.name
            if (
                package.resolve().parent != self.packages.resolve()
                or _PACKAGE_ID_PATTERN.fullmatch(package_id) is None
                or not _valid_package_marker(package, package_id)
            ):
                continue
            try:
                marker = json.loads(
                    (package / ".lean-runtime-package.json").read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                continue
            normalized = _normalized_package_identity(marker) if isinstance(marker, dict) else None
            entry = next((item for item in entries if str(item.get("name")) == name), None)
            if normalized is None or entry is None:
                continue
            if (
                normalized["toolchain"] != context.toolchain
                or normalized["platform"] != platform_compatibility()
                or normalized["package"] != _entry_identity(entry)
            ):
                continue
            dependencies = normalized["effective_dependencies"]
            if not isinstance(dependencies, list):
                continue
            compatible = True
            for dependency in dependencies:
                if not isinstance(dependency, dict):
                    compatible = False
                    break
                dependency_name = dependency.get("name")
                current = effective_entries.get(str(dependency_name))
                if current is None or _entry_identity(current) != dependency:
                    compatible = False
                    break
            if compatible:
                reusable[name] = package
        return reusable

    def has_built_graph(
        self,
        toolchain: str,
        entries: list[dict[str, Any]],
        *,
        roots: frozenset[str],
    ) -> bool:
        seeds = self.graph_seeds(toolchain, entries)
        for name in roots:
            seed = seeds.get(name)
            if seed is None:
                return False
            entry = next((item for item in entries if str(item.get("name")).lower() == name), None)
            subdir = str(entry.get("subDir") or "") if entry is not None else ""
            build = seed / subdir / ".lake" / "build"
            if not _has_root_olean(build, name):
                return False
        return True

    def _object_donor(self, url: str, revision: str, seed: Path | None) -> Path | None:
        candidates = [seed] if seed is not None else []
        if self.sources.is_dir():
            candidates.extend(path for path in self.sources.iterdir() if path.is_dir())
        for candidate in candidates:
            if (
                candidate is not None
                and _canonical_git_url(_git_remote(candidate) or "") == _canonical_git_url(url)
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
        source_id = sha256_id(
            "project_source", {"url": _canonical_git_url(url), "revision": revision}
        )
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
        seed_packages: Path | None = None,
        seed_package_paths: dict[str, Path] | None = None,
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
                        self.events.emit(
                            "project.shared.workspace_reused",
                            f"Reusing shared dependency workspace for {context.root.name}",
                            phase="shared-project",
                            workspace_id=workspace_id,
                            packages=len(packages),
                        )
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
                reusable_packages = self.reusable_packages(
                    context,
                    packages,
                    effective_entries=effective_entries,
                )
                git_packages = [entry for entry in packages if entry["type"] == "git"]
                git_position = 0
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
                        git_position += 1
                        url = entry.get("url")
                        revision = entry.get("rev")
                        if not isinstance(url, str) or not isinstance(revision, str):
                            raise ProjectError(
                                f"git dependency {entry['name']!r} has no exact URL and revision"
                            )
                        package_name = str(entry["name"])
                        self.events.emit(
                            "project.shared.package_started",
                            f"Resolving {package_name} ({git_position}/{len(git_packages)})",
                            phase="shared-project",
                            package=package_name,
                            current=git_position,
                            total=len(git_packages),
                        )
                        local = local_packages / str(entry["name"])
                        seed = (
                            seed_package_paths.get(str(entry["name"]), local)
                            if seed_package_paths is not None
                            else seed_packages / str(entry["name"])
                            if seed_packages
                            else local
                        )
                        if seed.is_symlink():
                            seed = seed.resolve()
                        subdir = _package_subdir(entry)
                        reusable = reusable_packages.get(package_name)
                        if reusable is not None:
                            package_id = reusable.name
                            final_target = reusable
                            package_identity = json.loads(
                                (reusable / ".lean-runtime-package.json").read_text(
                                    encoding="utf-8"
                                )
                            )
                            source = reusable
                            self.events.emit(
                                "project.shared.package_reused",
                                f"Reusing {package_name} ({git_position}/{len(git_packages)})",
                                phase="shared-project",
                                package=package_name,
                                current=git_position,
                                total=len(git_packages),
                            )
                        else:
                            source = self._source_checkout(
                                url=url,
                                revision=revision,
                                seed=seed if seed.is_dir() else None,
                                cancel=cancel,
                            )
                            source_package = source / subdir if subdir is not None else source
                            package_identity = _package_identity(
                                context=context,
                                entry=entry,
                                source_package=source_package,
                                effective_entries=effective_entries,
                            )
                            package_id = sha256_id("project_package", package_identity)
                            final_target = self.packages / package_id
                        # A marker created by an older schema may describe the exact same
                        # graph with cosmetic scope/URL differences. Reuse that managed
                        # path directly so its absolute-path Lake traces remain warm.
                        try:
                            seed_marker = json.loads(
                                (seed / ".lean-runtime-package.json").read_text(encoding="utf-8")
                            )
                        except (OSError, json.JSONDecodeError):
                            seed_marker = None
                        seed_id = seed.name
                        if (
                            isinstance(seed_marker, dict)
                            and seed.resolve().parent == self.packages.resolve()
                            and _PACKAGE_ID_PATTERN.fullmatch(seed_id) is not None
                            and _valid_package_marker(seed, seed_id)
                            and _normalized_package_identity(seed_marker)
                            == _normalized_package_identity(package_identity)
                        ):
                            package_id = seed_id
                            final_target = seed
                        package_ids.append(package_id)
                        with FileLock(
                            self.locks / f"{package_id}.lock", timeout=1800, cancel=cancel
                        ):
                            if final_target.is_dir() and not _valid_package_marker(
                                final_target, package_id
                            ):
                                remove_tree(final_target)
                            if not final_target.is_dir():
                                self.events.emit(
                                    "project.shared.package_import_started",
                                    f"Importing {package_name} "
                                    f"({git_position}/{len(git_packages)})",
                                    phase="shared-project",
                                    package=package_name,
                                    current=git_position,
                                    total=len(git_packages),
                                )
                                self.packages.mkdir(parents=True, exist_ok=True)
                                package_staging = self.packages / (
                                    f".{package_id}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
                                )
                                try:
                                    # Preserve compatible local artifacts on first import. CoW
                                    # cloning prevents later writes from mutating the donor.
                                    donor = (
                                        seed
                                        if seed.is_dir()
                                        and _git_head(seed) == revision
                                        and _git_clean(seed)
                                        else source
                                    )
                                    clone_tree(donor, package_staging)
                                    # A verified sparse environment carries compiled package
                                    # artifacts but intentionally omits Git sources. Graft those
                                    # artifacts onto the independently verified exact checkout so
                                    # project onboarding does not discard the capsule and rebuild
                                    # the dependency graph from scratch.
                                    if seed.is_dir() and donor.resolve() != seed.resolve():
                                        seed_package = seed / subdir if subdir is not None else seed
                                        staged_package = (
                                            package_staging / subdir
                                            if subdir is not None
                                            else package_staging
                                        )
                                        seed_build = seed_package / ".lake" / "build"
                                        staged_build = staged_package / ".lake" / "build"
                                        if seed_build.is_dir() and not staged_build.exists():
                                            staged_build.parent.mkdir(parents=True, exist_ok=True)
                                            clone_tree(seed_build, staged_build)
                                    write_json_atomic(
                                        package_staging / ".lean-runtime-package.json",
                                        package_identity,
                                    )
                                    package_staging.replace(final_target)
                                except BaseException:
                                    if package_staging.exists():
                                        remove_tree(package_staging)
                                    raise
                        package_dir = final_target / subdir if subdir is not None else final_target
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
