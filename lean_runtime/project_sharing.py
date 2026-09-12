"""Plan and perform reversible adoption of shared Lake dependencies."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ._adoption_estimates import AdoptionEstimates, PackageAdoptionAction
from ._git import git_head
from ._paths import is_link, link_directory, remove_tree
from ._project_identity import (
    package_subdir,
)
from ._published_estimates import PublishedPackageSize, SizeReferenceRequest
from ._relpath import packages_directory
from .errors import ProjectError
from .policies import format_byte_size
from .projects import ProjectContext, discover_project
from .serialization import write_json_atomic
from .shared_projects import SharedProjectManager, SharedProjectWorkspace, _git_clean_project_seed
from .store import clone_tree, tree_usage
from .store import source_snapshot_digest as source_snapshot_digest

PROJECT_CONFIG = "lean-runtime.toml"
PROJECT_CONFIG_SCHEMA = "lean-runtime-project/1"
ATTACHMENT_RECORD = "lean-runtime-attachment.json"
ATTACHMENT_SCHEMA = "lean-runtime-project-attachment/1"
_CONFIG_CONTENT = f'schema = "{PROJECT_CONFIG_SCHEMA}"\ndependencies = "shared"\n'


def _read_bytes_or_none(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _restore_file(path: Path, previous: bytes | None) -> None:
    """Put ``path`` back to ``previous`` (its earlier contents, or absence)."""
    with suppress(OSError):
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(previous)


def _remove_path(path: Path) -> None:
    if is_link(path):
        if path.is_symlink():
            path.unlink()
        else:
            path.rmdir()
    elif path.is_file():
        path.unlink()
    elif path.exists():
        remove_tree(path)


def _tree_bytes(root: Path) -> int:
    """Size a local checkout without following links into shared storage."""
    total = 0
    if not root.is_dir() or is_link(root):
        return total
    for directory, directories, _filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        directories[:] = [name for name in directories if not is_link(current / name)]
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _read_manifest(context: ProjectContext) -> dict[str, Any]:
    path = context.current_manifest()
    if path is None:
        raise ProjectError(
            "shared project adoption requires lake-manifest.json; run `lake update` once, "
            "then retry"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectError(f"could not read Lake manifest: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("packages"), list):
        raise ProjectError("Lake manifest has no package entries")
    return value


def _manifest_packages(context: ProjectContext) -> list[dict[str, Any]]:
    raw = _read_manifest(context)["packages"]
    packages: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise ProjectError("Lake manifest contains a malformed package entry")
        if entry.get("type") not in {"git", "path"}:
            raise ProjectError(
                f"shared adoption does not support package source type {entry.get('type')!r}"
            )
        if entry.get("type") == "git":
            package_subdir(entry)
        packages.append(entry)
    return packages


def _packages_directory(context: ProjectContext) -> Path:
    manifest = _read_manifest(context)
    try:
        relative = packages_directory(manifest)
    except ValueError as exc:
        raise ProjectError(f"Lake manifest packagesDir is unsafe: {exc}") from exc
    return context.root.joinpath(*relative.parts)


def project_sharing_enabled(root: Path) -> bool:
    config = root / PROJECT_CONFIG
    try:
        return config.read_text(encoding="utf-8") == _CONFIG_CONTENT
    except OSError:
        return False


def discover_shareable_projects(path: Path, *, recursive: bool) -> tuple[Path, ...]:
    source = path.expanduser().resolve()
    if not recursive:
        return (discover_project(source).root,)
    if not source.is_dir():
        raise ProjectError(f"recursive project discovery requires a directory: {source}")
    roots: list[Path] = []
    for current, directories, files in os.walk(source, followlinks=False):
        directories[:] = [
            name
            for name in directories
            if name not in {".git", ".lake"} and not name.startswith(".lake.")
        ]
        if "lean-toolchain" in files and ({"lakefile.toml", "lakefile.lean"} & set(files)):
            roots.append(Path(current).resolve())
    return tuple(sorted(set(roots)))


@dataclass(frozen=True, slots=True)
class ProjectAdoption:
    root: Path
    toolchain: str | None
    packages: tuple[str, ...]
    dependency_bytes: int
    attached: bool
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "toolchain": self.toolchain,
            "packages": list(self.packages),
            "dependency_bytes": self.dependency_bytes,
            "attached": self.attached,
            "ready": self.ready,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class AdoptionPlan:
    projects: tuple[ProjectAdoption, ...]
    recursive: bool
    current_dependency_bytes: int
    estimated_shared_bytes: int
    shared_bytes_reused: int = 0
    new_shared_bytes: int = 0
    storage_estimate_complete: bool = True
    unknown_dependencies: tuple[str, ...] = ()
    download_bytes: int | None = None
    download_estimate_complete: bool = False
    new_source_bytes: int = 0
    jobs: int = 1
    size_references: tuple[dict[str, Any], ...] = ()
    actions: tuple[PackageAdoptionAction, ...] = ()
    estimates: AdoptionEstimates | None = None

    @property
    def ready(self) -> int:
        return sum(project.ready for project in self.projects)

    @property
    def blocked(self) -> int:
        return len(self.projects) - self.ready

    @property
    def estimated_reclaimable_bytes(self) -> int | None:
        """Compatibility alias for estimated machine-level recovery."""
        return self.estimated_machine_reclaimable_bytes

    @property
    def checkout_bytes_removed(self) -> int:
        return self.current_dependency_bytes

    @property
    def estimated_machine_reclaimable_bytes(self) -> int | None:
        # Logical copies cannot establish exclusive physical allocation.
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "recursive": self.recursive,
            "jobs": self.jobs,
            "size_references": list(self.size_references),
            "approximate_artifact_bytes": None,  # deprecated: samples are not additive
            "actions": [action.to_dict() for action in self.actions],
            "estimates": self.estimates.to_dict() if self.estimates else None,
            "projects": [project.to_dict() for project in self.projects],
            "ready": self.ready,
            "blocked": self.blocked,
            "current_dependency_bytes": self.current_dependency_bytes,
            "estimated_shared_bytes": self.estimated_shared_bytes,
            "checkout_bytes_removed": self.checkout_bytes_removed,
            "shared_bytes_reused": self.shared_bytes_reused,
            "new_shared_bytes": self.new_shared_bytes,
            "new_source_bytes": self.new_source_bytes,
            "storage_estimate_complete": self.storage_estimate_complete,
            "unknown_dependencies": list(self.unknown_dependencies),
            "download_bytes": self.download_bytes,
            "download_estimate_complete": self.download_estimate_complete,
            "storage_byte_basis": "estimated materialized bytes; not physical allocation",
            "estimated_machine_reclaimable_bytes": (self.estimated_machine_reclaimable_bytes),
            "estimated_reclaimable_bytes": self.estimated_reclaimable_bytes,
        }


@dataclass(frozen=True, slots=True)
class AdoptionResult:
    root: Path
    action: str
    packages: int
    reclaimed_bytes: int
    workspace_id: str | None = None
    observations: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "action": self.action,
            "packages": self.packages,
            "reclaimed_bytes": self.reclaimed_bytes,
            "workspace_id": self.workspace_id,
            "observations": self.observations,
            "logical_directory_bytes_replaced": self.reclaimed_bytes,
            "physical_recovery_bytes": None,
        }


@dataclass(frozen=True, slots=True)
class AdoptionBatchResult:
    plan: AdoptionPlan
    results: tuple[AdoptionResult, ...]
    failures: tuple[tuple[Path, str], ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures and self.plan.blocked == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "plan": self.plan.to_dict(),
            "results": [result.to_dict() for result in self.results],
            "failures": [{"root": str(root), "error": message} for root, message in self.failures],
        }


@dataclass(frozen=True, slots=True)
class ProjectInitPlan:
    """Read-only plan for creating or adopting a Lean Runtime project."""

    root: Path
    action: str
    toolchain: str
    mathlib_version: str | None
    packages: tuple[str, ...]
    seed_root: Path | None
    download_bytes: int | None
    download_bytes_complete: bool
    already_attached: bool = False
    toolchain_installed: bool = False
    project_name: str | None = None
    blockers: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "action": self.action,
            "toolchain": self.toolchain,
            "mathlib_version": self.mathlib_version,
            "packages": list(self.packages),
            "seed_root": str(self.seed_root) if self.seed_root is not None else None,
            "download_bytes": self.download_bytes,
            "download_bytes_complete": self.download_bytes_complete,
            "already_attached": self.already_attached,
            "toolchain_installed": self.toolchain_installed,
            "project_name": self.project_name,
            "ready": self.ready,
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True, slots=True)
class ProjectScanResult:
    """Projects registered as future exact dependency seeds."""

    root: Path
    projects: tuple[Path, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"root": str(self.root), "projects": [str(path) for path in self.projects]}


@dataclass(frozen=True, slots=True)
class ProjectUpdatePlan:
    """Read-only latest-Mathlib update plan for one project."""

    root: Path
    current_version: str
    target_version: str
    current_revision: str
    target_revision: str
    current_toolchain: str
    target_toolchain: str
    packages: tuple[str, ...]
    seed_root: Path | None
    download_bytes: int | None
    download_bytes_complete: bool
    blockers: tuple[str, ...] = ()
    toolchain_installed: bool = False

    @property
    def changed(self) -> bool:
        return (
            self.current_version != self.target_version
            or self.current_revision != self.target_revision
            or self.current_toolchain != self.target_toolchain
        )

    @property
    def ready(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "current_version": self.current_version,
            "target_version": self.target_version,
            "current_revision": self.current_revision,
            "target_revision": self.target_revision,
            "current_toolchain": self.current_toolchain,
            "target_toolchain": self.target_toolchain,
            "packages": list(self.packages),
            "seed_root": str(self.seed_root) if self.seed_root is not None else None,
            "download_bytes": self.download_bytes,
            "download_bytes_complete": self.download_bytes_complete,
            "changed": self.changed,
            "ready": self.ready,
            "blockers": list(self.blockers),
            "toolchain_installed": self.toolchain_installed,
        }


@dataclass(frozen=True, slots=True)
class DetachmentPlan:
    root: Path
    packages: tuple[str, ...]
    materialize_bytes: int
    bytes_free: int
    blockers: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.blockers and self.bytes_free >= self.materialize_bytes

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "packages": list(self.packages),
            "materialize_bytes": self.materialize_bytes,
            "bytes_free": self.bytes_free,
            "ready": self.ready,
            "blockers": list(self.blockers),
        }


def inspect_adoption(root: Path) -> ProjectAdoption:
    try:
        context = discover_project(root)
        packages = _manifest_packages(context)
        packages_dir = _packages_directory(context)
    except ProjectError as exc:
        return ProjectAdoption(root.resolve(), None, (), 0, False, (str(exc),))
    blockers: list[str] = []
    warnings: list[str] = []
    if is_link(context.root / ".lake"):
        blockers.append(".lake itself is a symlink; only dependency directories can be adopted")
    config = context.root / PROJECT_CONFIG
    if config.exists() and not project_sharing_enabled(context.root):
        blockers.append(f"{PROJECT_CONFIG} exists but is not managed shared-mode configuration")
    dependency_bytes = 0
    names: list[str] = []
    for entry in packages:
        name = str(entry["name"])
        names.append(name)
        if entry["type"] == "path":
            raw = entry.get("dir")
            dependency = (context.root / str(raw)).resolve()
            if not dependency.is_dir():
                blockers.append(f"local path dependency {name} does not exist: {dependency}")
            continue
        local = packages_dir / name
        if is_link(local):
            continue
        if not local.exists():
            warnings.append(f"{name} is not local yet and will be fetched at its exact revision")
            continue
        if local.is_dir() and not any(local.iterdir()):
            warnings.append(f"{name} has an empty local placeholder and will be fetched exactly")
            continue
        dependency_bytes += _tree_bytes(local)
        revision = entry.get("rev")
        head = git_head(local)
        if head is None:
            blockers.append(f"dependency {name} is not a Git checkout")
        elif not isinstance(revision, str) or head != revision:
            blockers.append(
                f"dependency {name} is checked out at {head[:12]}, not manifest revision "
                f"{str(revision)[:12]}"
            )
        elif not _git_clean_project_seed(local):
            blockers.append(f"dependency {name} has local changes")
    attached = (
        project_sharing_enabled(context.root)
        and (context.root / ".lake" / ATTACHMENT_RECORD).is_file()
    )
    return ProjectAdoption(
        context.root,
        context.toolchain,
        tuple(names),
        dependency_bytes,
        attached,
        tuple(blockers),
        tuple(warnings),
    )


def attachment_matches_workspace(
    context: ProjectContext, workspace: SharedProjectWorkspace
) -> bool:
    """Validate links/junctions against an independently validated expected workspace."""
    if not project_sharing_enabled(context.root):
        return False
    try:
        attachment = json.loads((context.root / ".lake" / ATTACHMENT_RECORD).read_text())
        if (
            not isinstance(attachment, dict)
            or attachment.get("schema") != ATTACHMENT_SCHEMA
            or attachment.get("workspace_id") != workspace.workspace_id
        ):
            return False
        overrides = {
            entry["name"]: Path(entry["dir"])
            for entry in json.loads(workspace.overrides_file.read_text())["packages"]
        }
        package_dir = _packages_directory(context)
        expected_links = {}
        for entry in _manifest_packages(context):
            if entry["type"] != "git":
                continue
            name = str(entry["name"])
            target = overrides[name]
            if not target.is_dir():
                return False
            subdir = package_subdir(entry)
            if subdir:
                for _part in subdir.parts:
                    target = target.parent
            link = package_dir / name
            if not is_link(link) or link.resolve() != target.resolve():
                return False
            expected_links[name] = str(target.resolve())
        return attachment.get("packages") == expected_links
    except (OSError, ValueError, KeyError, TypeError, ProjectError):
        return False


def plan_adoption(
    path: Path,
    *,
    recursive: bool,
    shared: SharedProjectManager | None = None,
    jobs: int | None = None,
    size_samples: Callable[[], tuple[PublishedPackageSize, ...]] | None = None,
    reference_provider: Callable[
        [tuple[SizeReferenceRequest, ...]], tuple[PublishedPackageSize, ...]
    ]
    | None = None,
) -> AdoptionPlan:
    from ._adoption_planning import build_adoption_plan

    return build_adoption_plan(
        path,
        recursive=recursive,
        shared=shared,
        jobs=jobs,
        size_samples=size_samples,
        reference_provider=reference_provider,
    )


def plan_detachment(root: Path) -> DetachmentPlan:
    context = discover_project(root)
    adoption = inspect_adoption(context.root)
    blockers: list[str] = []
    if not adoption.attached:
        blockers.append("project is not attached to shared dependencies")
    packages_dir = _packages_directory(context)
    total = 0
    names: list[str] = []
    for entry in _manifest_packages(context):
        if entry["type"] != "git":
            continue
        name = str(entry["name"])
        names.append(name)
        source = packages_dir / name
        if not is_link(source):
            blockers.append(f"attached package link is missing or replaced: {name}")
            continue
        target = source.resolve()
        if not target.is_dir():
            blockers.append(f"attached package target is unavailable: {name}")
            continue
        total += _tree_bytes(target)
    packages_dir.parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(packages_dir.parent).free
    if free < total:
        blockers.append(
            f"detachment may need {format_byte_size(total)} but only "
            f"{format_byte_size(free)} is free"
        )
    return DetachmentPlan(context.root, tuple(names), total, free, tuple(blockers))


class ProjectAdopter:
    """Atomically replace generated Lake package copies with shared package links."""

    def __init__(self, shared: SharedProjectManager) -> None:
        self.shared = shared

    def attach(
        self,
        context: ProjectContext,
        *,
        probe: Callable[[Path | None], None],
        seed_packages: Path | None = None,
        seed_package_paths: dict[str, Path] | None = None,
        display_name: str | None = None,
    ) -> AdoptionResult:
        adoption = inspect_adoption(context.root)
        if adoption.blockers:
            raise ProjectError("cannot attach project:\n- " + "\n- ".join(adoption.blockers))
        workspace = self.shared.prepare(
            context,
            seed_packages=seed_packages,
            seed_package_paths=seed_package_paths,
            display_name=display_name,
        )
        self.shared.events.emit(
            "project.attach.shared_probe_started",
            f"Verifying shared graph for {display_name or context.root.name}",
            phase="project-attach",
            packages=len(workspace.package_ids),
        )
        probe(workspace.overrides_file)
        manifest_packages = _manifest_packages(context)
        override_value = json.loads(workspace.overrides_file.read_text(encoding="utf-8"))
        override_package_dirs = {
            str(entry["name"]): Path(str(entry["dir"]))
            for entry in override_value["packages"]
            if isinstance(entry, dict)
            and entry.get("type") == "path"
            and isinstance(entry.get("name"), str)
            and isinstance(entry.get("dir"), str)
        }
        lake_dir = context.root / ".lake"
        lake_dir.mkdir(parents=True, exist_ok=True)
        packages_dir = _packages_directory(context)
        marker = lake_dir / ATTACHMENT_RECORD
        links_match = attachment_matches_workspace(context, workspace)
        if links_match:
            return AdoptionResult(
                context.root,
                "already-attached",
                len(workspace.package_ids),
                0,
                workspace.workspace_id,
            )
        if packages_dir.parent != lake_dir:
            packages_dir.parent.mkdir(parents=True, exist_ok=True)
        token = f"{os.getpid()}.{uuid.uuid4().hex}"
        staging = packages_dir.parent / f".{packages_dir.name}.lean-runtime.{token}.tmp"
        backup = packages_dir.parent / f".{packages_dir.name}.lean-runtime.{token}.backup"
        before_usage = tree_usage(packages_dir, missing_ok=True)
        reclaimed = before_usage.logical_bytes
        staging.mkdir()
        swapped = False
        had_original = packages_dir.exists() or is_link(packages_dir)
        config = context.root / PROJECT_CONFIG
        previous_marker = _read_bytes_or_none(marker)
        previous_config = _read_bytes_or_none(config)
        try:
            self.shared.events.emit(
                "project.attach.swap_started",
                f"Replacing local dependency copies for {context.root.name}",
                phase="project-attach",
                packages=len(workspace.package_ids),
                checkout_bytes=reclaimed,
            )
            for entry in manifest_packages:
                if entry["type"] != "git":
                    continue
                name = str(entry["name"])
                package_dir = override_package_dirs.get(name)
                if package_dir is None or not package_dir.is_dir():
                    raise ProjectError(f"shared workspace has no materialized package {name}")
                subdir = package_subdir(entry)
                target = package_dir
                if subdir is not None:
                    for _part in subdir.parts:
                        target = target.parent
                    if (target / subdir).resolve() != package_dir.resolve():
                        raise ProjectError(f"shared workspace has an invalid subDir for {name}")
                link_directory(target, staging / name)
            if had_original:
                packages_dir.replace(backup)
            staging.replace(packages_dir)
            swapped = True
            self.shared.events.emit(
                "project.attach.attached_probe_started",
                f"Verifying attached graph for {context.root.name}",
                phase="project-attach",
                packages=len(workspace.package_ids),
            )
            probe(None)
            record = {
                "schema": ATTACHMENT_SCHEMA,
                "workspace_id": workspace.workspace_id,
                "packages": {
                    entry.name: str(entry.resolve())
                    for entry in sorted(packages_dir.iterdir(), key=lambda path: path.name)
                },
            }
            write_json_atomic(marker, record)
            config.write_text(_CONFIG_CONTENT, encoding="utf-8")
        except BaseException:
            _remove_path(staging)
            if swapped:
                _remove_path(packages_dir)
            if backup.exists() or is_link(backup):
                backup.replace(packages_dir)
            # Restore the records exactly as they were, rather than deleting
            # them: a previous attachment or user config must survive a failed
            # re-attach.
            _restore_file(marker, previous_marker)
            _restore_file(config, previous_config)
            raise
        cleanup_complete = self._cleanup_committed_backup(backup)
        self.shared.events.emit(
            "project.attach.completed",
            f"Attached shared dependencies for {context.root.name}",
            phase="project-attach",
            packages=len(workspace.package_ids),
            checkout_bytes=reclaimed,
        )
        return AdoptionResult(
            context.root,
            "attached",
            len(workspace.package_ids),
            reclaimed,
            workspace.workspace_id,
            {
                "local_inventory_before": asdict(before_usage),
                "local_inventory_after": asdict(tree_usage(packages_dir)),
                "backup_cleanup_complete": cleanup_complete,
                "retained_backup": str(backup) if not cleanup_complete else None,
                "measurement_scope": "local dependency directories; not machine disk recovery",
            },
        )

    def _cleanup_committed_backup(self, backup: Path) -> bool:
        try:
            _remove_path(backup)
            return True
        except OSError as exc:
            self.shared.events.emit(
                "project.backup.cleanup_failed",
                f"Attachment transaction committed; backup cleanup failed: {exc}",
                phase="project-cleanup",
                backup=str(backup),
            )
            return False

    def detach(
        self,
        context: ProjectContext,
        *,
        probe: Callable[[Path | None], None],
    ) -> AdoptionResult:
        detachment = plan_detachment(context.root)
        if not detachment.ready:
            raise ProjectError("cannot detach project:\n- " + "\n- ".join(detachment.blockers))
        marker = context.root / ".lake" / ATTACHMENT_RECORD
        if not marker.is_file() or not project_sharing_enabled(context.root):
            raise ProjectError(f"project is not attached to shared dependencies: {context.root}")
        manifest_packages = _manifest_packages(context)
        packages_dir = _packages_directory(context)
        token = f"{os.getpid()}.{uuid.uuid4().hex}"
        staging = packages_dir.parent / f".{packages_dir.name}.lean-runtime.{token}.tmp"
        backup = packages_dir.parent / f".{packages_dir.name}.lean-runtime.{token}.backup"
        staging.mkdir()
        copied = 0
        swapped = False
        git_package_count = sum(1 for entry in manifest_packages if entry["type"] == "git")
        marker_contents = marker.read_text(encoding="utf-8")
        config = context.root / PROJECT_CONFIG
        try:
            for entry in manifest_packages:
                if entry["type"] != "git":
                    continue
                name = str(entry["name"])
                source = packages_dir / name
                if not is_link(source):
                    raise ProjectError(f"attached package link is missing or replaced: {name}")
                target = source.resolve()
                if not target.is_dir():
                    raise ProjectError(f"attached package target is unavailable: {name}")
                destination = staging / name
                self.shared.events.emit(
                    "project.detach.package_started",
                    f"Materializing {name} ({copied + 1}/{git_package_count})",
                    phase="project-detach",
                    package=name,
                    current=copied + 1,
                    total=git_package_count,
                )
                clone_tree(target, destination)
                package_marker = destination / ".lean-runtime-package.json"
                if package_marker.is_file():
                    package_marker.unlink()
                copied += 1
            packages_dir.replace(backup)
            staging.replace(packages_dir)
            swapped = True
            probe(None)
            marker.unlink()
            if project_sharing_enabled(context.root):
                config.unlink()
        except BaseException:
            _remove_path(staging)
            if swapped:
                _remove_path(packages_dir)
            if backup.exists() or is_link(backup):
                backup.replace(packages_dir)
            if not marker.exists():
                marker.write_text(marker_contents, encoding="utf-8")
            if not config.exists():
                config.write_text(_CONFIG_CONTENT, encoding="utf-8")
            raise
        cleanup_complete = self._cleanup_committed_backup(backup)
        return AdoptionResult(
            context.root,
            "detached",
            copied,
            0,
            observations={
                "local_inventory_after": asdict(tree_usage(packages_dir)),
                "backup_cleanup_complete": cleanup_complete,
                "retained_backup": str(backup) if not cleanup_complete else None,
                "measurement_scope": "local dependency directories; not machine disk recovery",
            },
        )
