"""Content-addressed dependency workspaces for mutable Lake projects."""

from __future__ import annotations

import json
import os
import threading
import uuid
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ._git import git_clean, git_has_commit, git_head, git_remote
from ._paths import is_link, remove_tree
from ._process import git_output, run_git
from ._project_identity import (
    SHARED_PROJECT_SCHEMA,
    PackageArtifactKey,
    PackageSourceKey,
    canonical_git_url,
    compute_package_identity,
    entry_identity,
    normalized_package_identity,
    package_artifact_key,
    package_source_key,
    package_subdir,
    resolved_dependency_names,
    resolved_path_entries,
)
from ._transaction import publish_tree, staged_tree
from .errors import ProjectError
from .events import EventEmitter
from .locking import FileLock, LockPaths
from .package_ids import (
    PACKAGE_ID_PATTERN,
    package_directories,
    package_directory_id,
    package_id_matches,
)
from .projects import ProjectContext, discover_project
from .serialization import sha256_id, write_json_atomic
from .store import (
    clone_tree,
    invalidate_storage_ledger,
    platform_compatibility,
)
from .toolchains import ToolchainBuildIdentity

if TYPE_CHECKING:
    from .toolchains import ToolchainManager

PROJECT_SEED_REGISTRY_SCHEMA = "lean-runtime-project-seeds/1"
_PACKAGE_ID_PATTERN = PACKAGE_ID_PATTERN
_MANAGED_PROJECT_CONFIG = 'schema = "lean-runtime-project/1"\ndependencies = "shared"\n'


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


@dataclass(frozen=True, slots=True)
class RememberedPackageSeed:
    """A clean remembered project that can donate source and possibly artifacts."""

    path: Path
    source: PackageSourceKey
    artifact: PackageArtifactKey | None
    artifact_miss: str | None = None


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


def _git_clean_project_seed(path: Path) -> bool:
    """Accept only Git-clean roots plus Lean Runtime's exact untracked config."""

    status = git_output("-C", str(path), "status", "--porcelain", "--untracked-files=normal")
    if status is None:
        return False
    changes = tuple(line for line in status.splitlines() if line)
    if not changes:
        return True
    config = path / "lean-runtime.toml"
    try:
        managed = config.read_text(encoding="utf-8") == _MANAGED_PROJECT_CONFIG
    except OSError:
        managed = False
    return managed and changes == ("?? lean-runtime.toml",)


def _remove_managed_project_config(path: Path) -> None:
    config = path / "lean-runtime.toml"
    try:
        if config.read_text(encoding="utf-8") == _MANAGED_PROJECT_CONFIG:
            config.unlink()
    except OSError:
        pass


def _run_git(arguments: list[str], *, purpose: str, cancel: threading.Event | None = None) -> None:
    result = run_git(*arguments, cancel=cancel)
    if not result.ok:
        detail = result.output.strip()
        if result.cancelled:
            raise ProjectError(f"cancelled while {purpose}")
        if result.timed_out:
            raise ProjectError(f"Git timed out while {purpose}")
        raise ProjectError(f"Git failed while {purpose}" + (f":\n{detail}" if detail else ""))


def _valid_package_marker(package: Path, package_id: str) -> bool:
    try:
        marker = json.loads((package / ".lean-runtime-package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(marker, dict) and package_id_matches(marker, package_id)


def _has_root_olean(build: Path, package_name: str) -> bool:
    lean_root = build / "lib" / "lean"
    expected = package_name.rsplit(".", 1)[-1].lower() + ".olean"
    try:
        return any(path.name.lower() == expected for path in lean_root.iterdir())
    except OSError:
        return False


class SharedProjectManager:
    """Prepare and lock reusable Lake dependency workspaces."""

    def __init__(
        self,
        home: Path,
        events: EventEmitter,
        toolchains: ToolchainManager | None = None,
    ) -> None:
        self.home = home
        self.events = events
        self.toolchains = toolchains
        self.root = home / "project-workspaces"
        self.packages = home / "project-packages"
        self.sources = home / "project-sources"
        self.lock_paths = LockPaths(home)
        self.seed_registry = home / "project-seeds.json"

    def _build_identity(
        self, toolchain: str, cancel: threading.Event | None
    ) -> ToolchainBuildIdentity | None:
        if self.toolchains is None:
            return None
        identity = getattr(self.toolchains, "build_identity", None)
        if callable(identity):
            return cast(ToolchainBuildIdentity, identity(toolchain, cancel=cancel))
        digest = getattr(self.toolchains, "executable_digest", None)
        if not callable(digest):
            return None
        return ToolchainBuildIdentity(
            toolchain,
            str(digest(toolchain, "lean")),
            str(digest(toolchain, "lake")),
        )

    def remember_project(self, context: ProjectContext) -> None:
        """Remember a Lake project as a future exact dependency seed."""
        manifest = context.current_manifest()
        if manifest is None:
            return
        self.home.mkdir(parents=True, exist_ok=True)
        with FileLock(self.lock_paths.project_seeds(), timeout=30):
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
        _toolchain: str,
        entries: list[dict[str, Any]],
        *,
        roots: tuple[Path, ...] = (),
        exclude_root: Path | None = None,
    ) -> tuple[dict[str, Path], Path | None]:
        """Find one exact complete source graph in registered or explicit projects."""
        required = {
            str(entry.get("name")): (
                str(entry.get("rev")),
                str(entry.get("subDir") or ""),
                canonical_git_url(str(entry.get("url"))),
            )
            for entry in entries
            if entry.get("type") == "git"
        }
        candidates = tuple(dict.fromkeys((*roots, *self.remembered_roots())))
        best: dict[str, Path] | None = None
        best_root: Path | None = None
        best_score: tuple[int, int, int] = (-1, -1, -1)
        for root in candidates:
            if exclude_root is not None and root.resolve() == exclude_root.resolve():
                continue
            try:
                context = discover_project(root)
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
                    canonical_git_url(str(entry.get("url"))),
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
                if is_link(package):
                    package = package.resolve()
                marker_id = package.name
                managed = _PACKAGE_ID_PATTERN.fullmatch(
                    marker_id
                ) is not None and _valid_package_marker(package, marker_id)
                if (
                    not package.is_dir()
                    or git_head(package) != revision
                    or (not managed and not git_clean(package))
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

    def registered_package_seeds(
        self,
        context: ProjectContext,
        entries: list[dict[str, Any]],
        *,
        effective_entries: dict[str, dict[str, Any]] | None = None,
        toolchain_identity: ToolchainBuildIdentity | None = None,
    ) -> dict[str, RememberedPackageSeed]:
        """Match remembered project roots to individual exact Git dependencies.

        A clean URL/revision match may always donate source. Compiled artifacts
        additionally require the same toolchain, platform, and resolved package
        dependency cone. Unrelated packages in the consumer do not participate.
        """

        if effective_entries is None:
            identity_entries = resolved_path_entries(context, entries)
            effective_entries = {
                str(entry["name"]): entry_identity(identity_entry)
                for entry, identity_entry in zip(entries, identity_entries, strict=True)
            }
        selected: dict[str, RememberedPackageSeed] = {}
        scores: dict[str, tuple[bool, int]] = {}
        for root in self.remembered_roots():
            if root.resolve() == context.root.resolve():
                continue
            try:
                producer = discover_project(root)
                producer_manifest = _load_manifest(producer)
                producer_entries = producer_manifest["packages"]
                producer_resolved = resolved_path_entries(producer, producer_entries)
            except ProjectError:
                continue
            remote = git_remote(producer.root)
            head = git_head(producer.root)
            if remote is None or head is None or not _git_clean_project_seed(producer.root):
                continue
            producer_effective = {
                str(entry["name"]): entry_identity(identity_entry)
                for entry, identity_entry in zip(producer_entries, producer_resolved, strict=True)
            }
            for entry in entries:
                if entry.get("type") != "git":
                    continue
                name = str(entry["name"])
                url = entry.get("url")
                revision = entry.get("rev")
                if (
                    not isinstance(url, str)
                    or not isinstance(revision, str)
                    or canonical_git_url(remote) != canonical_git_url(url)
                    or head != revision
                ):
                    continue
                subdir = package_subdir(entry)
                source_package = producer.root / subdir if subdir is not None else producer.root
                source = package_source_key(entry, source_package)
                if source is None:
                    continue
                artifact: PackageArtifactKey | None = None
                miss: str | None = None
                expected_config = str(entry.get("configFile", "lakefile.toml"))
                expected_manifest = str(entry.get("manifestFile", "lake-manifest.json"))
                producer_manifest_path = producer.current_manifest()
                if subdir is not None:
                    miss = "subdirectory project artifacts require an independently pinned donor"
                elif producer.lakefile.name != expected_config:
                    miss = (
                        f"package configuration differs: donor {producer.lakefile.name}, "
                        f"consumer {expected_config}"
                    )
                elif (
                    producer_manifest_path is None
                    or producer_manifest_path.name != expected_manifest
                ):
                    miss = f"package manifest differs: consumer expects {expected_manifest}"
                elif producer.toolchain != context.toolchain:
                    miss = (
                        f"toolchain differs: donor {producer.toolchain}, "
                        f"consumer {context.toolchain}"
                    )
                else:
                    dependency_names = resolved_dependency_names(
                        source_package,
                        str(entry.get("manifestFile", "lake-manifest.json")),
                    )
                    if dependency_names is None:
                        miss = "dependency manifest is unavailable"
                    else:
                        divergent = next(
                            (
                                dependency_name
                                for dependency_name in sorted(dependency_names)
                                if producer_effective.get(dependency_name)
                                != effective_entries.get(dependency_name)
                            ),
                            None,
                        )
                        if divergent is not None:
                            miss = f"resolved dependency differs: {divergent}"
                        else:
                            expected_artifact = package_artifact_key(
                                context=context,
                                entry=entry,
                                source_package=source_package,
                                effective_entries=effective_entries,
                                toolchain_identity=toolchain_identity,
                            )
                            try:
                                donor_marker = json.loads(
                                    (producer.root / ".lean-runtime-package.json").read_text(
                                        encoding="utf-8"
                                    )
                                )
                            except (OSError, json.JSONDecodeError):
                                donor_marker = None
                            recorded_artifact = (
                                donor_marker.get("artifact_key")
                                if isinstance(donor_marker, dict)
                                else None
                            )
                            if expected_artifact is None:
                                miss = "artifact dependency cone is incomplete"
                            elif recorded_artifact != expected_artifact.to_dict():
                                miss = "donor has no matching toolchain artifact identity"
                            elif not (source_package / ".lake" / "build").is_dir():
                                miss = "donor has no compiled artifacts"
                            else:
                                artifact = expected_artifact
                try:
                    modified = (source_package / ".lake" / "build").stat().st_mtime_ns
                except OSError:
                    modified = 0
                score = (artifact is not None, modified)
                if score > scores.get(name, (False, -1)):
                    selected[name] = RememberedPackageSeed(producer.root, source, artifact, miss)
                    scores[name] = score
        return selected

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
                canonical_git_url(str(entry.get("url"))),
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
                        canonical_git_url(str(entry.get("url"))),
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
        for package_directory in package_directories(self.packages):
            marker = package_directory / ".lean-runtime-package.json"
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
                canonical_git_url(str(package_entry.get("url"))),
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
        toolchain_identity: ToolchainBuildIdentity | None = None,
    ) -> dict[str, Path]:
        """Return managed packages whose recorded graph exactly matches this project."""
        if effective_entries is None:
            identity_entries = resolved_path_entries(context, entries)
            effective_entries = {
                str(entry["name"]): entry_identity(identity_entry)
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
            normalized = normalized_package_identity(marker) if isinstance(marker, dict) else None
            entry = next((item for item in entries if str(item.get("name")) == name), None)
            if normalized is None or entry is None:
                continue
            if (
                normalized["toolchain"] != context.toolchain
                or normalized["platform"] != platform_compatibility()
                or normalized["package"] != entry_identity(entry)
            ):
                continue
            subdir = package_subdir(entry)
            source_package = package / subdir if subdir is not None else package
            expected_artifact = package_artifact_key(
                context=context,
                entry=entry,
                source_package=source_package,
                effective_entries=effective_entries,
                toolchain_identity=toolchain_identity,
            )
            if (
                expected_artifact is None
                or normalized["artifact_key"] != expected_artifact.to_dict()
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
                if current is None or entry_identity(current) != dependency:
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
        candidates.extend(self.remembered_roots())
        for candidate in candidates:
            if (
                candidate is not None
                and canonical_git_url(git_remote(candidate) or "") == canonical_git_url(url)
                and git_has_commit(candidate, revision)
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
            "project_source", {"url": canonical_git_url(url), "revision": revision}
        )
        destination = self.sources / source_id
        with FileLock(self.lock_paths.project_source(source_id), timeout=1800, cancel=cancel):
            if (
                destination.is_dir()
                and git_head(destination) == revision
                and git_clean(destination)
            ):
                return destination
            with staged_tree(self.sources, self.lock_paths) as staging:
                # Git and clone_tree create the checkout directory themselves.
                staging.rmdir()
                if (
                    seed is not None
                    and seed.is_dir()
                    and git_head(seed) == revision
                    and _git_clean_project_seed(seed)
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
                    _remove_managed_project_config(staging)
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
                        cancel=cancel,
                    )
                    _run_git(
                        ["-C", str(staging), "checkout", "--quiet", "--detach", revision],
                        purpose=f"checking out {revision}",
                        cancel=cancel,
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
                        cancel=cancel,
                    )
                    _run_git(
                        ["-C", str(staging), "checkout", "--quiet", "--detach", "FETCH_HEAD"],
                        purpose=f"checking out {revision}",
                    )
                if git_head(staging) != revision:
                    raise ProjectError(f"dependency checkout did not resolve to {revision}")
                publish_tree(staging, destination, replace=True)
        return destination

    def prepare(
        self,
        context: ProjectContext,
        *,
        cancel: threading.Event | None = None,
        seed_packages: Path | None = None,
        seed_package_paths: dict[str, Path] | None = None,
        display_name: str | None = None,
    ) -> SharedProjectWorkspace:
        manifest = _load_manifest(context)
        toolchain_identity = self._build_identity(context.toolchain, cancel)
        packages = manifest["packages"]
        identity_packages = resolved_path_entries(context, packages)
        identity = {
            "schema": SHARED_PROJECT_SCHEMA,
            "toolchain": context.toolchain,
            "toolchain_build": (
                toolchain_identity.to_dict() if toolchain_identity is not None else None
            ),
            "platform": platform_compatibility(),
            "packages": identity_packages,
        }
        workspace_id = sha256_id("project_workspace", identity)
        destination = self.root / workspace_id
        overrides_file = destination / "package-overrides.json"
        package_names = tuple(str(entry["name"]) for entry in packages)
        project_name = display_name or context.root.name
        with FileLock(
            self.lock_paths.project_workspace(workspace_id),
            timeout=1800,
            cancel=cancel,
            owner={"operation": "workspace preparation", "project": project_name},
            on_wait=self._announce_lock_wait,
        ):
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
                            f"Reusing shared dependency workspace for {project_name}",
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
                f"Preparing shared dependencies for {project_name}",
                phase="shared-project",
                project=project_name,
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
                    str(entry["name"]): entry_identity(identity_entry)
                    for entry, identity_entry in zip(packages, identity_packages, strict=True)
                }
                reusable_packages = self.reusable_packages(
                    context,
                    packages,
                    effective_entries=effective_entries,
                    toolchain_identity=toolchain_identity,
                )
                remembered_packages = self.registered_package_seeds(
                    context,
                    packages,
                    effective_entries=effective_entries,
                    toolchain_identity=toolchain_identity,
                )
                registered_graph, registered_root = self.registered_graph_seeds(
                    context.toolchain,
                    packages,
                    exclude_root=context.root,
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
                        remembered = remembered_packages.get(package_name)
                        preserve_seed_artifacts = False
                        if seed_package_paths is not None:
                            seed = seed_package_paths.get(package_name, local)
                        elif seed_packages is not None:
                            seed = seed_packages / package_name
                        elif package_name in registered_graph:
                            seed = registered_graph[package_name]
                            requested = entry.get("inputRev")
                            self.events.emit(
                                "project.shared.project_seed_selected",
                                f"Reusing registered project source for {package_name}",
                                phase="shared-project",
                                package=package_name,
                                input_revision=requested,
                                revision=revision,
                                donor=str(registered_root),
                                artifacts=False,
                                artifact_miss=(
                                    "toolchain differs; compiled artifacts are not eligible"
                                    if registered_root is not None
                                    and discover_project(registered_root).toolchain
                                    != context.toolchain
                                    else "registered graph donates source only"
                                ),
                            )
                        elif local.is_dir() and git_head(local) == revision and git_clean(local):
                            seed = local
                        elif remembered is not None:
                            seed = remembered.path
                            preserve_seed_artifacts = remembered.artifact is not None
                            requested = entry.get("inputRev")
                            label = (
                                f"{package_name} {requested} ({revision[:8]}…)"
                                if isinstance(requested, str) and requested
                                else f"{package_name} {revision[:8]}…"
                            )
                            if preserve_seed_artifacts:
                                message = f"Reusing remembered project artifacts for {label}"
                            else:
                                message = f"Reusing remembered project source for {label}"
                            self.events.emit(
                                "project.shared.project_seed_selected",
                                message,
                                phase="shared-project",
                                package=package_name,
                                input_revision=requested,
                                revision=revision,
                                donor=str(remembered.path),
                                artifacts=preserve_seed_artifacts,
                                artifact_miss=remembered.artifact_miss,
                            )
                        else:
                            seed = local
                        if is_link(seed):
                            seed = seed.resolve()
                        subdir = package_subdir(entry)
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
                            package_identity = compute_package_identity(
                                context=context,
                                entry=entry,
                                source_package=source_package,
                                effective_entries=effective_entries,
                                toolchain_identity=toolchain_identity,
                            )
                            package_id = package_directory_id(package_identity)
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
                            and normalized_package_identity(seed_marker)
                            == normalized_package_identity(package_identity)
                        ):
                            package_id = seed_id
                            final_target = seed
                        package_ids.append(package_id)
                        with FileLock(
                            self.lock_paths.package(package_id), timeout=1800, cancel=cancel
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
                                        if preserve_seed_artifacts
                                        and seed.is_dir()
                                        and git_head(seed) == revision
                                        and _git_clean_project_seed(seed)
                                        else source
                                    )
                                    clone_tree(donor, package_staging)
                                    _remove_managed_project_config(package_staging)
                                    # A verified sparse environment carries compiled package
                                    # artifacts but intentionally omits Git sources. Graft those
                                    # artifacts onto the independently verified exact checkout so
                                    # project onboarding does not discard the capsule and rebuild
                                    # the dependency graph from scratch.
                                    if (
                                        preserve_seed_artifacts
                                        and seed.is_dir()
                                        and donor.resolve() != seed.resolve()
                                    ):
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

    def _announce_lock_wait(self, holder: dict[str, Any] | None) -> None:
        """Attribute a shared workspace lock wait to its current holder."""
        details = holder or {}
        pid = details.get("pid")
        operation = details.get("operation")
        subject = details.get("project") or details.get("packages")
        if isinstance(subject, list):
            names = [str(item) for item in subject]
            subject = ", ".join(names[:3]) + ("…" if len(names) > 3 else "")
        described = str(operation or "another operation") + (f" of {subject}" if subject else "")
        held_by = f"PID {pid} ({described})" if pid else "another process"
        self.events.emit(
            "project.workspace_lock_wait",
            f"Waiting for shared workspace lock held by {held_by}",
            phase="shared-project",
            holder=details,
        )

    @contextmanager
    def build_lock(
        self,
        workspace: SharedProjectWorkspace,
        *,
        cancel: threading.Event | None = None,
    ) -> Any:
        """Serialize builds that may update any shared dependency artifacts."""
        owner = {"operation": "shared build", "packages": sorted(set(workspace.packages))}
        with ExitStack() as stack:
            for package_id in sorted(set(workspace.package_ids)):
                stack.enter_context(
                    FileLock(
                        self.lock_paths.package_build(package_id),
                        timeout=1800,
                        cancel=cancel,
                        owner=owner,
                        on_wait=self._announce_lock_wait,
                    )
                )
            try:
                yield
            finally:
                # Shared package trees were (possibly) rebuilt in place.
                invalidate_storage_ledger(self.home)
