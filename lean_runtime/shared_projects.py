"""Content-addressed dependency workspaces for mutable Lake projects."""

from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from functools import cache
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
    source_snapshot_digest,
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


class SourceSelectionInventory:
    """Facts memoized for one read-only plan, never retained for execution."""

    def __init__(self, path_digests: dict[Path, str] | None = None) -> None:
        self.head = cache(git_head)
        self.remote = cache(git_remote)
        self.has_commit = cache(git_has_commit)
        self.clean = cache(git_clean)
        self.clean_seed = cache(_git_clean_project_seed)
        self.path_digests = dict(path_digests or {})

    def digest(self, path: Path) -> str:
        if path not in self.path_digests:
            self.path_digests[path] = source_snapshot_digest(path)
        return self.path_digests[path]


@dataclass(frozen=True, slots=True)
class PackagePreparationInput:
    seed: Path
    reusable: Path | None
    artifact_miss: str | None

    donor: Path | None = None
    donor_miss: str | None = None


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
    """Accept clean sources plus generated Lake state and reserved runtime metadata."""

    status = git_output("-C", str(path), "status", "--porcelain", "--untracked-files=normal")
    if status is None:
        return False
    changes = tuple(
        line
        for line in status.splitlines()
        if line and line not in {"?? .lean-runtime-package.json", "?? .lake/"}
    )
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


def _strip_build_state(root: Path) -> None:
    """Source caches, including older caches, never authorize copied build state."""
    for directory, directories, _files in os.walk(root):
        directories[:] = [name for name in directories if name != ".git"]
        if ".lake" not in directories:
            continue
        build = Path(directory) / ".lake"
        if is_link(build):
            if build.is_symlink():
                build.unlink()
            else:
                build.rmdir()
        else:
            remove_tree(build)
        directories.remove(".lake")


def _copy_build_outputs(source: Path, destination: Path) -> None:
    """Retain build products, regenerating path-sensitive traces and linked state."""

    def ignored(directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if is_link(Path(directory) / name) or name.endswith((".trace", ".hash"))
        }

    destination.mkdir(parents=True, exist_ok=True)
    for name in ("lib", "ir", "bin"):
        tree = source / name
        if tree.is_dir() and not is_link(tree):
            shutil.copytree(tree, destination / name, symlinks=True, ignore=ignored)


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

    def local_toolchain_build_identity(self, toolchain: str) -> ToolchainBuildIdentity | None:
        """Planning must never acquire a missing toolchain."""
        local = getattr(self.toolchains, "local_build_identity", None)
        if callable(local):
            return cast(ToolchainBuildIdentity | None, local(toolchain))
        # Custom providers can expose a local executable-digest API.
        available = getattr(self.toolchains, "is_available_locally", None)
        digest = getattr(self.toolchains, "executable_digest", None)
        if not callable(available) or not available(toolchain) or not callable(digest):
            return None
        return ToolchainBuildIdentity(
            toolchain, str(digest(toolchain, "lean")), str(digest(toolchain, "lake"))
        )

    def artifact_donor_rejection(
        self,
        context: ProjectContext,
        entry: dict[str, Any],
        donor: Path,
        effective_entries: dict[str, dict[str, Any]],
        toolchain_identity: ToolchainBuildIdentity | None,
    ) -> str | None:
        """An exact source revision alone never proves compiled compatibility."""
        if git_head(donor) != entry.get("rev") or not _git_clean_project_seed(donor):
            return "source revision or cleanliness differs"
        subdir = package_subdir(entry)
        package = donor / subdir if subdir is not None else donor
        expected = package_artifact_key(
            context=context,
            entry=entry,
            source_package=package,
            effective_entries=effective_entries,
            toolchain_identity=toolchain_identity,
        )
        try:
            marker = json.loads((donor / ".lean-runtime-package.json").read_text())
        except (OSError, json.JSONDecodeError):
            marker = None
        normalized = normalized_package_identity(marker) if isinstance(marker, dict) else None
        if (
            expected is None
            or normalized is None
            or normalized["artifact_key"] != expected.to_dict()
        ):
            return "no matching recorded compiler and effective-graph provenance; rebuild required"
        if is_link(package / ".lake") or is_link(package / ".lake" / "build"):
            return "linked build state has no independent provenance; rebuild required"
        if not (package / ".lake" / "build").is_dir():
            return "no compiled build outputs"
        return None

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
        inventory: SourceSelectionInventory | None = None,
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
                    or (inventory.head(package) if inventory else git_head(package)) != revision
                    or (
                        not managed
                        and not (inventory.clean(package) if inventory else git_clean(package))
                    )
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
        inventory: SourceSelectionInventory | None = None,
    ) -> dict[str, RememberedPackageSeed]:
        """Match remembered project roots to individual exact Git dependencies.

        A clean URL/revision match may always donate source. Compiled artifacts
        additionally require the same toolchain, platform, and conservative
        full effective dependency graph.
        """

        if effective_entries is None:
            identity_entries = resolved_path_entries(context, entries)
            effective_entries = {
                str(entry["name"]): entry_identity(identity_entry)
                for entry, identity_entry in zip(entries, identity_entries, strict=True)
            }
        selected: dict[str, RememberedPackageSeed] = {}
        scores: dict[str, tuple[bool, int]] = {}
        requested_sources = {
            (canonical_git_url(str(e.get("url", ""))), e.get("rev"))
            for e in entries
            if e.get("type") == "git"
        }
        for root in self.remembered_roots():
            if root.resolve() == context.root.resolve():
                continue
            remote = inventory.remote(root) if inventory else git_remote(root)
            head = inventory.head(root) if inventory else git_head(root)
            if (
                remote is None
                or head is None
                or (canonical_git_url(remote), head) not in requested_sources
            ):
                continue
            if not (inventory.clean_seed(root) if inventory else _git_clean_project_seed(root)):
                continue
            try:
                producer = discover_project(root)
                producer_manifest = _load_manifest(producer)
                producer_entries = producer_manifest["packages"]
                producer_resolved = (
                    resolved_path_entries(producer, producer_entries, digest=inventory.digest)
                    if inventory
                    else resolved_path_entries(producer, producer_entries)
                )
            except ProjectError:
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
                            elif (
                                not isinstance(donor_marker, dict)
                                or normalized_package_identity(donor_marker) is None
                                or recorded_artifact != expected_artifact.to_dict()
                            ):
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
        preferred = list(self.graph_seeds(context.toolchain, entries).values())
        candidates = list(dict.fromkeys([*preferred, *package_directories(self.packages)]))
        for package in candidates:
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
            name = str(normalized["package"].get("name")) if normalized is not None else ""
            entry = next((item for item in entries if str(item.get("name")) == name), None)
            if name in reusable or normalized is None or entry is None:
                continue
            if (
                normalized["toolchain"] != context.toolchain
                or normalized["platform"] != platform_compatibility()
                or normalized["package"] != entry_identity(entry)
            ):
                continue
            if git_head(package) != entry.get("rev") or not _git_clean_project_seed(package):
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

    def _object_donor(
        self,
        url: str,
        revision: str,
        seed: Path | None,
        inventory: SourceSelectionInventory | None = None,
    ) -> Path | None:
        candidates = [seed] if seed is not None else []
        if self.sources.is_dir():
            candidates.extend(path for path in self.sources.iterdir() if path.is_dir())
        candidates.extend(self.remembered_roots())
        for candidate in candidates:
            if (
                candidate is not None
                and canonical_git_url(
                    (inventory.remote(candidate) if inventory else git_remote(candidate)) or ""
                )
                == canonical_git_url(url)
                and (
                    inventory.has_commit(candidate, revision)
                    if inventory
                    else git_has_commit(candidate, revision)
                )
            ):
                return candidate
        return None

    def source_input(
        self,
        entry: dict[str, Any],
        seed: Path | None,
        inventory: SourceSelectionInventory | None = None,
    ) -> tuple[str, Path | None]:
        """Read-only source selection, repeated under the source lock by execution."""
        cached = self.cached_source(entry, inventory)
        if cached is not None:
            return "use_cached_source", cached
        revision = str(entry["rev"])
        if (
            seed is not None
            and seed.is_dir()
            and (inventory.head(seed) if inventory else git_head(seed)) == revision
            and (inventory.clean_seed(seed) if inventory else _git_clean_project_seed(seed))
        ):
            return "import_local", seed
        donor = self._object_donor(str(entry["url"]), revision, seed, inventory)
        if donor is not None:
            return "clone_local_objects", donor
        return "fetch_source", None

    def preparation_inputs(
        self,
        context: ProjectContext,
        packages: list[dict[str, Any]],
        *,
        effective_entries: dict[str, dict[str, Any]],
        toolchain_identity: ToolchainBuildIdentity | None,
        inventory: SourceSelectionInventory | None = None,
        seed_packages: Path | None = None,
        seed_package_paths: dict[str, Path] | None = None,
    ) -> dict[str, PackagePreparationInput]:
        """Shared donor/compatibility decisions; callers must revalidate before mutation."""
        reusable = self.reusable_packages(
            context,
            packages,
            effective_entries=effective_entries,
            toolchain_identity=toolchain_identity,
        )
        remembered: dict[str, RememberedPackageSeed] | None = None
        graph: dict[str, Path] = {}
        graph_root: Path | None = None
        manifest = _load_manifest(context)
        local_packages = context.root / str(manifest.get("packagesDir", ".lake/packages"))
        result = {}
        for entry in packages:
            if entry["type"] != "git":
                continue
            name = str(entry["name"])
            local = local_packages / name
            if name in reusable:
                result[name] = PackagePreparationInput(reusable[name], reusable[name], None)
                continue
            donor = None
            donor_miss = None
            if seed_package_paths is not None:
                seed = seed_package_paths.get(name, local)
            elif seed_packages is not None:
                seed = seed_packages / name
            elif (
                local.is_dir()
                and (inventory.head(local) if inventory else git_head(local)) == entry["rev"]
                and (inventory.clean_seed(local) if inventory else _git_clean_project_seed(local))
            ):
                seed = local
            else:
                if remembered is None:
                    remembered = self.registered_package_seeds(
                        context,
                        packages,
                        effective_entries=effective_entries,
                        toolchain_identity=toolchain_identity,
                        inventory=inventory,
                    )
                    graph, graph_root = self.registered_graph_seeds(
                        context.toolchain,
                        packages,
                        exclude_root=context.root,
                        inventory=inventory,
                    )
                if name in graph:
                    seed = graph[name]
                    donor = graph_root
                    donor_miss = "registered graph donates source only"
                elif name in remembered:
                    seed = remembered[name].path
                    donor = seed
                    donor_miss = remembered[name].artifact_miss
                else:
                    seed = local
            if is_link(seed):
                seed = seed.resolve()
            result[name] = PackagePreparationInput(
                seed,
                reusable.get(name),
                self.artifact_donor_rejection(
                    context, entry, seed, effective_entries, toolchain_identity
                ),
                donor,
                donor_miss,
            )
        return result

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
            kind, selected = self.source_input({"url": url, "rev": revision}, seed)
            if kind == "use_cached_source":
                return destination
            with staged_tree(self.sources, self.lock_paths) as staging:
                staging.rmdir()
                if kind == "import_local":
                    assert selected is not None
                    self.events.emit(
                        "project.shared.source_started",
                        f"Importing cached source for {url}",
                        phase="shared-project",
                        source=url,
                    )
                    clone_tree(selected, staging)
                    _strip_build_state(staging)
                    _remove_managed_project_config(staging)
                    (staging / ".lean-runtime-package.json").unlink(missing_ok=True)
                elif kind == "clone_local_objects":
                    assert selected is not None
                    donor = selected
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

    @staticmethod
    def _workspace_identity(
        context: ProjectContext,
        identity_packages: list[dict[str, Any]],
        toolchain_identity: ToolchainBuildIdentity | None,
    ) -> dict[str, Any]:
        return {
            "schema": SHARED_PROJECT_SCHEMA,
            "toolchain": context.toolchain,
            "toolchain_build": toolchain_identity.to_dict() if toolchain_identity else None,
            "platform": platform_compatibility(),
            "packages": identity_packages,
        }

    def existing_workspace(
        self,
        context: ProjectContext,
        *,
        toolchain_identity: ToolchainBuildIdentity | None,
        identity_packages: list[dict[str, Any]],
    ) -> SharedProjectWorkspace | None:
        """Validate retained graph records and every target without acquiring anything."""
        identity = self._workspace_identity(context, identity_packages, toolchain_identity)
        workspace_id = sha256_id("project_workspace", identity)
        directory = self.root / workspace_id
        overrides_file = directory / "package-overrides.json"
        effective = {str(entry["name"]): entry_identity(entry) for entry in identity_packages}
        try:
            record = json.loads((directory / "workspace.json").read_text())
            overrides = json.loads(overrides_file.read_text())["packages"]
            if not isinstance(record, dict) or any(record.get(k) != v for k, v in identity.items()):
                return None
            package_ids = record["package_ids"]
            git_entries = [entry for entry in identity_packages if entry["type"] == "git"]
            if (
                not isinstance(package_ids, list)
                or len(package_ids) != len(git_entries)
                or not isinstance(overrides, list)
                or len(overrides) != len(identity_packages)
            ):
                return None
            if git_entries and toolchain_identity is None:
                return None
            ids = iter(package_ids)
            for entry, override in zip(identity_packages, overrides, strict=True):
                expected_override: dict[str, Any] = {
                    key: entry[key]
                    for key in ("name", "scope", "inherited", "configFile", "manifestFile")
                    if key in entry
                }
                expected_override.setdefault("inherited", False)
                if entry["type"] == "path":
                    target = Path(str(entry["dir"]))
                else:
                    package_id = next(ids)
                    if not isinstance(package_id, str) or not PACKAGE_ID_PATTERN.fullmatch(
                        package_id
                    ):
                        return None
                    package = self.packages / package_id
                    if (
                        not _valid_package_marker(package, package_id)
                        or git_head(package) != entry.get("rev")
                        or not _git_clean_project_seed(package)
                    ):
                        return None
                    subdir = package_subdir(entry)
                    target = package / subdir if subdir else package
                    marker = json.loads((package / ".lean-runtime-package.json").read_text())
                    expected = compute_package_identity(
                        context=context,
                        entry=entry,
                        source_package=target,
                        effective_entries=effective,
                        toolchain_identity=toolchain_identity,
                    )
                    normalized = normalized_package_identity(marker)
                    if (
                        normalized is None
                        or expected["artifact_key"] is None
                        or normalized != normalized_package_identity(expected)
                    ):
                        return None
                if not target.is_dir():
                    return None
                expected_override.update(type="path", dir=str(target))
                if override != expected_override:
                    return None
            return SharedProjectWorkspace(
                workspace_id,
                directory,
                overrides_file,
                True,
                tuple(str(entry["name"]) for entry in identity_packages),
                tuple(package_ids),
            )
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            StopIteration,
            ProjectError,
        ):
            return None

    def cached_source(
        self, entry: dict[str, Any], inventory: SourceSelectionInventory | None = None
    ) -> Path | None:
        """An exact local source inventory, independent of artifact compatibility."""
        source_id = sha256_id(
            "project_source",
            {
                "url": canonical_git_url(str(entry.get("url", ""))),
                "revision": entry.get("rev"),
            },
        )
        source = self.sources / source_id
        return (
            source
            if (inventory.head(source) if inventory else git_head(source)) == entry.get("rev")
            and (inventory.clean(source) if inventory else git_clean(source))
            else None
        )

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
        identity = self._workspace_identity(context, identity_packages, toolchain_identity)
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
                existing = self.existing_workspace(
                    context,
                    toolchain_identity=toolchain_identity,
                    identity_packages=identity_packages,
                )
                if existing is not None:
                    self.events.emit(
                        "project.shared.workspace_reused",
                        f"Reusing shared dependency workspace for {project_name}",
                        phase="shared-project",
                        workspace_id=workspace_id,
                        packages=len(packages),
                    )
                    return existing
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
                overrides: list[dict[str, Any]] = []
                package_ids: list[str] = []
                effective_entries = {
                    str(entry["name"]): entry_identity(identity_entry)
                    for entry, identity_entry in zip(packages, identity_packages, strict=True)
                }
                preparation = self.preparation_inputs(
                    context,
                    packages,
                    effective_entries=effective_entries,
                    toolchain_identity=toolchain_identity,
                    seed_packages=seed_packages,
                    seed_package_paths=seed_package_paths,
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
                        decision = preparation[package_name]
                        seed = decision.seed
                        if decision.donor is not None:
                            self.events.emit(
                                "project.shared.project_seed_selected",
                                f"Reusing registered project source for {package_name}",
                                phase="shared-project",
                                package=package_name,
                                input_revision=entry.get("inputRev"),
                                revision=revision,
                                donor=str(decision.donor),
                                artifacts=decision.artifact_miss is None,
                                artifact_miss=decision.donor_miss,
                            )
                        artifact_miss = decision.artifact_miss
                        preserve_seed_artifacts = artifact_miss is None
                        self.events.emit(
                            "project.shared.artifact_migration",
                            f"{package_name}: "
                            + (
                                "preserving compatible build outputs"
                                if preserve_seed_artifacts
                                else f"artifacts rejected: {artifact_miss}"
                            ),
                            phase="shared-project",
                            package=package_name,
                            artifacts=preserve_seed_artifacts,
                            artifact_miss=artifact_miss,
                        )
                        subdir = package_subdir(entry)
                        reusable = decision.reusable
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
                            and normalized_package_identity(seed_marker) is not None
                            and package_identity.get("artifact_key") is not None
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
                            if final_target.is_dir() and not _git_clean_project_seed(final_target):
                                raise ProjectError(
                                    f"managed package {package_name} has local changes; "
                                    "refusing to reuse or overwrite it"
                                )
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
                                    clone_tree(source, package_staging)
                                    _strip_build_state(package_staging)
                                    _remove_managed_project_config(package_staging)
                                    if preserve_seed_artifacts:
                                        seed_package = seed / subdir if subdir is not None else seed
                                        staged_package = (
                                            package_staging / subdir
                                            if subdir is not None
                                            else package_staging
                                        )
                                        seed_build = seed_package / ".lake" / "build"
                                        staged_build = staged_package / ".lake" / "build"
                                        if staged_build.exists():
                                            remove_tree(staged_build)
                                        _copy_build_outputs(seed_build, staged_build)
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
