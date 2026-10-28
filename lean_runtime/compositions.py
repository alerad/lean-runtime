"""Exact, named multi-repository environments over the shared Lake build engine.

Composition locks are separate from Git-only EnvironmentLock v1. Source locations
and aliases never participate in portable semantic identity. Original checkouts
are read-only inputs; generated build state lives in Runtime's owned store.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import zipfile
from builtins import list as List
from collections.abc import Callable
from contextlib import nullcontext
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

from ._paths import link_directory, remove_tree
from ._platform import platform_compatibility
from ._project_identity import canonical_git_url
from ._relpath import packages_directory, safe_relative_posix
from ._transaction import publish_tree, staged_tree
from .comparison import ComparisonEntry, EnvironmentComparison
from .composition_sources import (
    EXCLUDED,
    inventory,
    lean_name,
    module_index,
    raw_name,
    validate_inventory,
    write_lake_root,
)
from .composition_storage import CompositionStorageScope
from .dependency_views import dependency_view
from .errors import EnvironmentError, ProjectError
from .locking import FileLock
from .policies import ExecutionPolicy, NetworkPolicy
from .projects import ProjectContext, discover_project
from .serialization import sha256_id, write_json_atomic
from .shared_projects import SharedProjectWorkspace
from .storage_limits import StorageGuard, active_scope_budget, budgeted, owned_bytes

if TYPE_CHECKING:
    from .models import ExecutionResult
    from .runtime import Runtime

SCHEMA = "lean-runtime-composition/1"
PIN = "lean-runtime.environment.json"
ID = re.compile(r"composed_[0-9a-f]{64}\Z")
ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def _case_folded_alias(lock: dict[str, Any], prior: str | None, name: str) -> bool:
    """Whether ``name`` may share the already linked package ``prior``.

    A case-insensitive filesystem folds e.g. `LeanCert` and `leancert` onto one
    link. Lake then shares that checkout, which is only sound when both names pin
    the same exact Git source.
    """

    if prior is None:
        return False
    sources = {
        p["name"]: (canonical_git_url(str(p.get("url", ""))), p.get("rev"))
        for p in lock["git_packages"]
    }
    return name in sources and sources.get(prior) == sources[name]


class CompositionManager:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.home = runtime.home
        self.root = self.home / "compositions"
        self.storage_limits = runtime.storage_limits
        self._scope_context: ContextVar[CompositionStorageScope | None] = ContextVar(
            "composition_scope", default=None
        )
        self._guard_context: ContextVar[StorageGuard | None] = ContextVar(
            "composition_guard", default=None
        )

    @property
    def _active_scope(self) -> CompositionStorageScope:
        scope = self._scope_context.get()
        if scope is None:
            raise ProjectError("composition operation has no active storage scope")
        return scope

    def _lock(
        self, name: str, cancel: threading.Event | None = None, timeout: float = 1800
    ) -> FileLock:
        return FileLock(self.root / ".locks" / f"{name}.lock", timeout=timeout, cancel=cancel)

    def _registry(self) -> dict[str, Any]:
        try:
            result = json.loads((self.root / "references.json").read_text())
        except FileNotFoundError:
            return {
                "schema": "lean-runtime-composition-references/1",
                "aliases": {},
                "projects": {},
            }
        except (OSError, ValueError) as exc:
            raise EnvironmentError("composition reference registry is unreadable") from exc
        if (
            result.get("schema") != "lean-runtime-composition-references/1"
            or not isinstance(result.get("aliases"), dict)
            or not isinstance(result.get("projects"), dict)
        ):
            raise EnvironmentError("invalid composition reference registry")
        return cast(dict[str, Any], result)

    def _reference(self, kind: str, key: str, value: str | None) -> None:
        with self._lock("references"):
            refs = self._registry()
            if value is None:
                refs[kind].pop(key, None)
            else:
                refs[kind][key] = value
            write_json_atomic(self.root / "references.json", refs)

    def metadata(self, root: Path, files: dict[str, Any], toolchain: str) -> dict[str, Any]:
        config = root / "lakefile.toml"
        if config.is_file():
            return tomllib.loads(config.read_text())
        if not (root / "lakefile.lean").is_file():
            raise ProjectError("missing Lake configuration")
        if not self.runtime.toolchains.is_build_available_locally(toolchain):
            raise ProjectError("metadata planning requires an already installed full toolchain")
        self.runtime.toolchains.ensure_full(toolchain)
        # Never let metadata elaboration write into the original checkout.
        with tempfile.TemporaryDirectory(prefix="lean-composition-plan-") as raw:
            stage = Path(raw)
            for item in files["entries"]:
                rel = item["path"]
                if rel in {"lakefile.lean", "lean-toolchain", "lake-manifest.json"}:
                    shutil.copy2(root / rel, stage / rel)
            output = stage / "translated.toml"
            result = self.runtime.backend.execute(
                self.runtime.toolchains.command(
                    toolchain, "lake", "translate-config", "toml", str(output)
                ),
                cwd=stage,
                environment=self.runtime.toolchains.environment_for(toolchain),
                policy=ExecutionPolicy(timeout_seconds=60, max_output_bytes=8192),
            )
            if result.exit_code or not output.is_file():
                raise ProjectError(
                    "cannot translate isolated lakefile.lean: " + result.stdout + result.stderr
                )
            return tomllib.loads(output.read_text())

    def plan(
        self,
        roots: List[str | Path],
        *,
        max_source_bytes: int = 200_000_000,
        max_files: int = 20000,
        exclude: tuple[str, ...] = (),
        metadata: dict[str, dict[str, Any]] | None = None,
        path_bindings: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not roots:
            raise ProjectError("composition needs at least one repository")
        if max_source_bytes < 1 or max_files < 1:
            raise ValueError("source allowances must be positive")
        projects: dict[str, dict[str, Any]] = {}
        by_root: dict[Path, str] = {}
        git: dict[str, dict[str, Any]] = {}
        conflicts: List[dict[str, Any]] = []
        locations: dict[str, str] = {}
        selected: List[str] = []
        total_bytes = 0
        total_files = 0

        def visit(root: Path) -> str:
            nonlocal total_bytes, total_files
            root = root.expanduser().resolve()
            if root in by_root:
                return by_root[root]
            context = discover_project(root)
            if context.root != root:
                raise ProjectError("each composition source must name a Lake project root")
            files = inventory(
                root, max_bytes=max_source_bytes, max_files=max_files, exclude=exclude
            )
            data = (metadata or {}).get(str(root)) or self.metadata(root, files, context.toolchain)
            name = lean_name(str(data.get("name", "")))
            if not re.fullmatch(r"[^\W\d][\w\'-]*(?:\.[^\W\d][\w\'-]*)*", raw_name(name)):
                raise ProjectError("invalid Lake package name")
            if name in projects:
                if projects[name]["source"]["source_id"] != files["source_id"]:
                    conflicts.append({"kind": "package_source_conflict", "package": name})
                by_root[root] = name
                return name
            manifest_path = context.current_manifest()
            if manifest_path is None:
                raise ProjectError("composition requires an exact lake-manifest.json")
            manifest = json.loads(manifest_path.read_text())
            packages_directory(manifest)
            if not isinstance(manifest.get("packages"), list):
                raise ProjectError("invalid manifest package list")
            libraries = data.get("lean_lib", [])
            if not isinstance(libraries, list):
                raise ProjectError("invalid library metadata")
            if any(
                not isinstance(lib, dict) or not isinstance(lib.get("name"), str)
                for lib in libraries
            ):
                raise ProjectError("each exported library needs a name")
            record: dict[str, Any] = {
                "name": name,
                "toolchain": context.toolchain,
                "source": files,
                "libraries": libraries,
                "metadata": data,
                "requires": [],
                "modules": module_index(data, files["entries"]),
                "configFile": "lakefile.toml"
                if (root / "lakefile.toml").is_file()
                else "lakefile.lean",
            }
            projects[name] = record
            by_root[root] = name
            locations[files["source_id"]] = str(root)
            total_bytes += files["bytes"]
            total_files += len(files["entries"])
            if total_files > max_files:
                raise ProjectError("combined selected source exceeds file allowance")
            if total_bytes > max_source_bytes:
                raise ProjectError("combined selected source exceeds composition allowance")
            names: set[str] = set()
            for entry in manifest["packages"]:
                if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                    raise ProjectError("malformed dependency entry")
                dependency = lean_name(entry["name"])
                if dependency in names or dependency == name:
                    raise ProjectError("duplicate/self dependency in manifest")
                names.add(dependency)
                record["requires"].append(dependency)
                if entry.get("type") == "path":
                    if not isinstance(entry.get("dir"), str):
                        raise ProjectError("missing path dependency directory")
                    target = (root / entry["dir"]).resolve()
                    target = Path((path_bindings or {}).get(str(target), str(target)))
                    actual = visit(target)
                    if actual != dependency:
                        conflicts.append(
                            {
                                "kind": "path_package_name_conflict",
                                "package": dependency,
                                "actual": actual,
                            }
                        )
                elif entry.get("type") == "git":
                    if not re.fullmatch(r"[0-9a-fA-F]{40}", str(entry.get("rev", ""))):
                        raise ProjectError(f"{dependency} needs an exact Git revision")
                    if not isinstance(entry.get("url"), str):
                        raise ProjectError("missing Git source URL")
                    self._safe_source_url(entry["url"])
                    if entry.get("subDir"):
                        safe_relative_posix(entry["subDir"])
                    normalized = {
                        key: entry[key]
                        for key in (
                            "url",
                            "rev",
                            "subDir",
                            "scope",
                            "configFile",
                            "manifestFile",
                            "inputRev",
                        )
                        if key in entry
                    }
                    normalized.update(name=dependency, type="git", inherited=False)
                    normalized["url"] = canonical_git_url(normalized["url"])
                    prior = git.get(dependency)
                    if prior is not None and self.git_identity(prior) != self.git_identity(
                        normalized
                    ):
                        conflicts.append(
                            {
                                "kind": "dependency_revision_conflict",
                                "package": dependency,
                                "revisions": [prior["rev"], normalized["rev"]],
                            }
                        )
                    git[dependency] = normalized
                else:
                    raise ProjectError("unsupported manifest dependency kind")

            def bound_path(value: str) -> Path:
                resolved = str((root / value).resolve())
                return Path((path_bindings or {}).get(resolved, resolved))

            manifest_entries = {lean_name(e["name"]): e for e in manifest["packages"]}
            for requirement in data.get("require", []):
                if not isinstance(requirement, dict) or not isinstance(
                    requirement.get("name"), str
                ):
                    raise ProjectError("invalid declarative dependency")
                dependency = lean_name(requirement["name"])
                pinned = manifest_entries.get(dependency)
                if pinned is None:
                    conflicts.append(
                        {"kind": "unlocked_configuration_dependency", "package": dependency}
                    )
                    continue
                if "git" in requirement and (
                    pinned.get("type") != "git"
                    or canonical_git_url(str(requirement["git"]))
                    != canonical_git_url(str(pinned.get("url", "")))
                ):
                    conflicts.append(
                        {"kind": "configuration_source_conflict", "package": dependency}
                    )
                if "path" in requirement and (
                    pinned.get("type") != "path"
                    or bound_path(requirement["path"]) != bound_path(pinned.get("dir", ""))
                ):
                    conflicts.append({"kind": "configuration_path_conflict", "package": dependency})
                requested = requirement.get("rev")
                if requested is not None and (
                    (pinned.get("inputRev") is not None and requested != pinned["inputRev"])
                    or (
                        re.fullmatch(r"[0-9a-fA-F]{40}", str(requested))
                        and requested != pinned.get("rev")
                    )
                ):
                    conflicts.append(
                        {"kind": "configuration_revision_conflict", "package": dependency}
                    )
            return name

        for root in roots:
            selected.append(visit(Path(root)))
        toolchains = sorted({p["toolchain"] for p in projects.values()})
        if len(toolchains) != 1:
            conflicts.append({"kind": "toolchain_conflict", "toolchains": toolchains})
        # A captured local repository must never impersonate a pinned Git dependency.
        for name in sorted(set(projects) & set(git)):
            conflicts.append({"kind": "snapshot_overrides_git_pin", "package": name})
        providers: dict[str, str] = {}
        for name, project in sorted(projects.items()):
            for module in project["modules"]:
                if module in providers and providers[module] != name:
                    conflicts.append(
                        {
                            "kind": "module_ambiguity",
                            "module": module,
                            "providers": [providers[module], name],
                        }
                    )
                providers[module] = name

        # Explicit cycle diagnosis, rather than an unbounded recursive build.
        visited: set[str] = set()

        def cycles(name: str, stack: tuple[str, ...]) -> None:
            if name in stack:
                conflicts.append({"kind": "dependency_cycle", "packages": [*stack, name]})
                return
            if name in visited:
                return
            visited.add(name)
            for child in projects[name]["requires"]:
                if child in projects:
                    cycles(child, (*stack, name))

        for name in sorted(projects):
            cycles(name, ())
        lock = {
            "schema": SCHEMA,
            "toolchain": toolchains[0],
            "projects": [projects[k] for k in sorted(projects)],
            "git_packages": [git[k] for k in sorted(git)],
            "selected": sorted(set(selected)),
            "source_policy": {
                "exclude": list(exclude),
                "mandatory_exclude": sorted(EXCLUDED),
            },
        }
        env_id = sha256_id("composed", lock)
        missing = []
        source_new = 0
        for p in lock["projects"]:
            if not (self.root / "sources" / p["source"]["source_id"]).is_dir():
                source_new += p["source"]["bytes"]
        if not self.runtime.toolchains.is_build_available_locally(lock["toolchain"]):
            missing.append({"kind": "missing_build_toolchain", "toolchain": lock["toolchain"]})
        seeds = self.runtime.shared_projects.graph_seeds(lock["toolchain"], lock["git_packages"])
        for entry in lock["git_packages"]:
            if (
                entry["name"] not in seeds
                and self.runtime.shared_projects.cached_source(entry) is None
            ):
                # Original project dependencies can donate exact sources/artifacts.
                found = False
                for root in by_root:
                    manifest = json.loads((root / "lake-manifest.json").read_text())
                    local = root.joinpath(*packages_directory(manifest).parts) / entry["name"]
                    from ._git import git_head

                    if git_head(local) == entry["rev"]:
                        found = True
                        break
                if not found:
                    missing.append(
                        {
                            "kind": "missing_exact_dependency",
                            "package": entry["name"],
                            "revision": entry["rev"],
                        }
                    )
        portable = {
            "environment_id": env_id,
            "lock": lock,
            "conflicts": conflicts,
            "missing": missing,
            "new_source_bytes": source_new,
            "build_output_bytes": None,
            "source_bytes": total_bytes,
            "ready": not conflicts,
            "local_ready": not conflicts and not missing,
        }
        plan_digest = sha256_id("compose_plan", portable)
        return {
            **portable,
            "plan_digest": plan_digest,
            "locations": locations,
            "path_bindings": path_bindings or {},
            "max_source_bytes": max_source_bytes,
            "max_files": max_files,
            "exclude": list(exclude),
        }

    @staticmethod
    def _safe_source_url(value: str) -> None:
        try:
            parsed = urlsplit(value)
            if (
                parsed.password is not None
                or parsed.query
                or parsed.fragment
                or (parsed.scheme in {"http", "https"} and parsed.username is not None)
            ):
                raise ProjectError("credentials/query values cannot be embedded in a source URL")
        except ValueError as exc:
            raise ProjectError("invalid Git source URL") from exc

    @staticmethod
    def git_identity(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            key: entry.get(key)
            for key in ("name", "url", "rev", "subDir", "configFile", "manifestFile")
        }

    def _source(self, source_id: str) -> Path:
        if not re.fullmatch(r"snapshot_[0-9a-f]{64}", source_id):
            raise ProjectError("invalid snapshot identity")
        return self.root / "sources" / source_id

    @budgeted("dependency", lambda self, _args: [self.root])
    def compose(
        self,
        plan: dict[str, Any],
        *,
        name: str | None = None,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        if name is not None and not ALIAS.fullmatch(name):
            raise ProjectError("invalid environment alias")
        # Re-read exact source bytes; plan locations are local bindings, never lock identity.
        original_roots = [
            plan["locations"][p["source"]["source_id"]]
            for p in plan["lock"]["projects"]
            if p["name"] in plan["lock"]["selected"]
        ]
        hints = {
            plan["locations"][p["source"]["source_id"]]: p["metadata"]
            for p in plan["lock"]["projects"]
        }
        current = self.plan(
            original_roots,
            max_source_bytes=plan["max_source_bytes"],
            max_files=plan["max_files"],
            exclude=tuple(plan["exclude"]),
            metadata=hints,
            path_bindings=plan.get("path_bindings"),
        )
        if current["plan_digest"] != plan["plan_digest"]:
            raise ProjectError("stale composition plan; source, pins, or availability changed")
        if not current["ready"]:
            raise ProjectError("composition conflicts: " + json.dumps(current["conflicts"]))
        env_id = current["environment_id"]
        with self._lock("lifecycle", cancel), self._lock(env_id, cancel):
            for project in current["lock"]["projects"]:
                if cancel is not None and cancel.is_set():
                    raise EnvironmentError("composition cancelled")
                target = self._source(project["source"]["source_id"])
                if target.exists():
                    validate_inventory(target, project["source"])
                    continue
                source = Path(current["locations"][project["source"]["source_id"]])
                with staged_tree(target.parent, None) as stage:
                    for item in project["source"]["entries"]:
                        relative = safe_relative_posix(item["path"])
                        destination = stage.joinpath(*relative.parts)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source / relative, destination)
                        destination.chmod(0o755 if item["executable"] else 0o644)
                    validate_inventory(stage, project["source"])
                    publish_tree(stage, target)
            destination = self.root / "environments" / env_id
            if not destination.exists():
                with staged_tree(destination.parent, None) as stage:
                    write_json_atomic(stage / "lock.json", current["lock"])
                    write_json_atomic(
                        stage / "status.json", {"state": "sources_available", "builds": []}
                    )
                    write_json_atomic(
                        stage / "artifact-references.json",
                        {
                            "schema": "lean-runtime-composition-artifacts/1",
                            "legacy_untracked": False,
                            "buckets": {},
                        },
                    )
                    publish_tree(stage, destination)
            if name is not None:
                self._reference("aliases", name, env_id)
        return self.inspect(env_id)

    @staticmethod
    def validate_lock(lock: dict[str, Any]) -> None:
        try:
            CompositionManager._validate_lock(lock)
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise ProjectError("malformed composition lock") from exc

    @staticmethod
    def _validate_lock(lock: dict[str, Any]) -> None:
        """Validate portable paths before importing or following any source reference."""
        from .toolchains import normalize_toolchain

        if (
            lock.get("schema") != SCHEMA
            or normalize_toolchain(lock["toolchain"]) != lock["toolchain"]
        ):
            raise ProjectError("invalid composition lock/toolchain")
        names = set()
        for item in [*lock["projects"], *lock["git_packages"]]:
            name = item["name"]
            if (
                not re.fullmatch(r"[^\W\d][\w\'-]*(?:\.[^\W\d][\w\'-]*)*", raw_name(name))
                or name in names
            ):
                raise ProjectError("invalid or duplicate composition package")
            names.add(name)
            if item.get("configFile"):
                safe_relative_posix(item["configFile"])
        for item in lock["projects"]:
            if item["toolchain"] != lock["toolchain"] or item["configFile"] not in {
                "lakefile.lean",
                "lakefile.toml",
            }:
                raise ProjectError("inconsistent composition project metadata")
            source = item["source"]
            if not re.fullmatch(r"snapshot_[0-9a-f]{64}", source["source_id"]):
                raise ProjectError("invalid snapshot identity")
            paths = set()
            size = 0
            for entry in source["entries"]:
                rel = safe_relative_posix(entry["path"]).as_posix()
                if rel in paths or any(part in EXCLUDED for part in Path(rel).parts):
                    raise ProjectError("invalid/duplicate snapshot member")
                paths.add(rel)
                if (
                    not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
                    or not isinstance(entry["bytes"], int)
                    or entry["bytes"] < 0
                ):
                    raise ProjectError("invalid snapshot member digest/size")
                if not isinstance(entry["executable"], bool):
                    raise ProjectError("invalid snapshot executable metadata")
                size += entry["bytes"]
            if (
                size != source["bytes"]
                or sha256_id("snapshot", source["entries"]) != source["source_id"]
            ):
                raise ProjectError("snapshot identity mismatch")
            if not set(item["requires"]) <= names:
                raise ProjectError("composition has unresolved dependencies")
        graph = {item["name"]: item["requires"] for item in lock["projects"]}
        visited: set[str] = set()

        def visit(name: str, active: set[str]) -> None:
            if name in active:
                raise ProjectError("composition dependency cycle")
            if name in visited or name not in graph:
                return
            for child in graph[name]:
                visit(child, active | {name})
            visited.add(name)

        for name in graph:
            visit(name, set())
        if not set(lock["selected"]) <= {p["name"] for p in lock["projects"]}:
            raise ProjectError("invalid selected package")
        for entry in lock["git_packages"]:
            CompositionManager._safe_source_url(entry["url"])
            if entry.get("type") != "git" or not re.fullmatch(r"[0-9a-fA-F]{40}", entry["rev"]):
                raise ProjectError("invalid exact Git dependency")
            if entry.get("subDir"):
                safe_relative_posix(entry["subDir"])
            if entry.get("manifestFile"):
                safe_relative_posix(entry["manifestFile"])

    def export(self, identifier: str, destination: str | Path) -> dict[str, Any]:
        """Portable verified sources and pins; no machine-local artifacts or credentials."""
        env_id, _, lock = self.resolve(identifier)
        target = Path(destination).expanduser().resolve()
        with self._lock("lifecycle"):
            for item in lock["projects"]:
                validate_inventory(self._source(item["source"]["source_id"]), item["source"])
            target.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation preserves an existing export on error.
            try:
                with (
                    target.open("xb") as output,
                    zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive,
                ):
                    archive.writestr("lock.json", json.dumps(lock, sort_keys=True))
                    written = set()
                    for item in lock["projects"]:
                        source = item["source"]
                        if source["source_id"] in written:
                            continue
                        written.add(source["source_id"])
                        for entry in source["entries"]:
                            archive.write(
                                self._source(source["source_id"]) / entry["path"],
                                "sources/" + source["source_id"] + "/" + entry["path"],
                            )
            except BaseException:
                # Never unlink somebody else's pre-existing export.
                if "output" in locals():
                    target.unlink(missing_ok=True)
                raise
        return {
            "environment_id": env_id,
            "path": str(target),
            "bytes": target.stat().st_size,
            "contents": "exact source snapshots and dependency pins; build artifacts excluded",
        }

    @budgeted("dependency", lambda self, _args: [self.root])
    def import_sources(
        self,
        archive_path: str | Path,
        *,
        name: str | None = None,
        apply: bool = False,
        max_source_bytes: int = 200_000_000,
        max_files: int = 20000,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        if name is not None and not ALIAS.fullmatch(name):
            raise ProjectError("invalid alias")
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            if (
                len(entries) > max_files + 1
                or sum(i.file_size for i in entries) > max_source_bytes + 8_000_000
            ):
                raise ProjectError("source capsule exceeds import allowances")
            if len({i.filename for i in entries}) != len(entries):
                raise ProjectError("duplicate archive member")
            if archive.getinfo("lock.json").file_size > 8_000_000:
                raise ProjectError("composition lock too large")
            lock = json.loads(archive.read("lock.json"))
            self.validate_lock(lock)
            sources = {p["source"]["source_id"]: p["source"] for p in lock["projects"]}
            expected = {
                "sources/" + sid + "/" + e["path"]: e
                for sid, src in sources.items()
                for e in src["entries"]
            }
            if set(expected) | {"lock.json"} != {i.filename for i in entries}:
                raise ProjectError("unexpected/missing source capsule members")
            if sum(src["bytes"] for src in sources.values()) > max_source_bytes:
                raise ProjectError("sources exceed import byte allowance")
            for member, entry in expected.items():
                if archive.getinfo(member).file_size != entry["bytes"]:
                    raise ProjectError("capsule member size mismatch")
            env_id = sha256_id("composed", lock)
            preview = {
                "environment_id": env_id,
                "source_bytes": sum(src["bytes"] for src in sources.values()),
                "apply": apply,
            }
            if not apply:
                return preview
            with self._lock("lifecycle", cancel), self._lock(env_id, cancel):
                for sid, src in sources.items():
                    destination = self._source(sid)
                    if destination.exists():
                        validate_inventory(destination, src)
                        continue
                    with staged_tree(destination.parent, None) as stage:
                        for entry in src["entries"]:
                            target = stage / entry["path"]
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(archive.read("sources/" + sid + "/" + entry["path"]))
                            target.chmod(0o755 if entry["executable"] else 0o644)
                        validate_inventory(stage, src)
                        publish_tree(stage, destination)
                destination = self.root / "environments" / env_id
                if not destination.exists():
                    with staged_tree(destination.parent, None) as stage:
                        write_json_atomic(stage / "lock.json", lock)
                        write_json_atomic(
                            stage / "status.json", {"state": "sources_available", "builds": []}
                        )
                        write_json_atomic(
                            stage / "artifact-references.json",
                            {
                                "schema": "lean-runtime-composition-artifacts/1",
                                "legacy_untracked": False,
                                "buckets": {},
                            },
                        )
                        publish_tree(stage, destination)
                if name is not None:
                    self._reference("aliases", name, env_id)
            return {**preview, **self.inspect(env_id)}

    def resolve(self, identifier: str) -> tuple[str, Path, dict[str, Any]]:
        env_id = (
            identifier if ID.fullmatch(identifier) else self._registry()["aliases"].get(identifier)
        )
        if not isinstance(env_id, str) or not ID.fullmatch(env_id):
            raise EnvironmentError("composed environment not found")
        path = self.root / "environments" / env_id
        try:
            lock = json.loads((path / "lock.json").read_text())
        except (OSError, ValueError) as exc:
            raise EnvironmentError("composed environment lock unavailable") from exc
        if lock.get("schema") != SCHEMA or sha256_id("composed", lock) != env_id:
            raise EnvironmentError("composed environment identity mismatch")
        self.validate_lock(lock)
        return env_id, path, lock

    def inspect(self, identifier: str) -> dict[str, Any]:
        env_id, path, lock = self.resolve(identifier)
        try:
            status = json.loads((path / "status.json").read_text())
        except (OSError, ValueError) as exc:
            raise EnvironmentError("invalid composed environment readiness record") from exc
        return {
            "environment_id": env_id,
            "toolchain": lock["toolchain"],
            "projects": lock["projects"],
            "git_packages": lock["git_packages"],
            "selected": lock["selected"],
            "names": sorted(
                k for k, value in self._registry()["aliases"].items() if value == env_id
            ),
            **status,
            "readiness_is": (
                "last successful build; warm revalidates sources/compiler and Lake traces"
            ),
        }

    def list(self) -> List[dict[str, Any]]:
        return [
            self.inspect(path.name)
            for path in sorted((self.root / "environments").glob("composed_*"))
            if path.is_dir()
        ]

    def compare(self, left: str, right: str) -> EnvironmentComparison:
        left_id, _, left_lock = self.resolve(left)
        right_id, _, right_lock = self.resolve(right)

        def fields(lock: dict[str, Any]) -> dict[str, Any]:
            result = {
                key: lock[key] for key in ("schema", "toolchain", "selected", "source_policy")
            }
            for project in lock["projects"]:
                result["projects." + project["name"]] = {
                    "source_id": project["source"]["source_id"],
                    "project_digest": sha256_id("project", project),
                    "dependencies": project["requires"],
                }
            for package in lock["git_packages"]:
                result["git_packages." + package["name"]] = package
            return result

        before, after = fields(left_lock), fields(right_lock)
        changes = tuple(
            ComparisonEntry(
                key,
                "added" if key not in before else "removed" if key not in after else "changed",
                True,
                before.get(key),
                after.get(key),
            )
            for key in sorted(before.keys() | after.keys())
            if before.get(key) != after.get(key)
        )
        return EnvironmentComparison(
            {"environment_id": left_id}, {"environment_id": right_id}, changes
        )

    def _prepare(
        self,
        path: Path,
        lock: dict[str, Any],
        cancel: threading.Event | None,
        *,
        consumer_context: str | None = None,
    ) -> tuple[List[dict[str, Any]], SharedProjectWorkspace]:
        self.runtime.toolchains.ensure_full(lock["toolchain"], cancel=cancel)
        # External dependencies form their own stable graph: adding a snapshot-only
        # repository does not rekey or copy an unchanged Mathlib graph.
        external = path / "external"
        external.mkdir(exist_ok=True)
        (external / "lean-toolchain").write_text(lock["toolchain"])
        (external / "lakefile.toml").write_text('name = "composition_external"\n')
        write_json_atomic(
            external / "lake-manifest.json", {"version": "1.1.0", "packages": lock["git_packages"]}
        )
        scope = self._scope_context.get() or self._scope(lock)
        limit = self.storage_limits.dependency_bytes
        if limit is None:
            workspace = self.runtime.shared_projects.prepare(
                discover_project(external), cancel=cancel
            )
        else:
            guard_context = (
                nullcontext(None)
                if active_scope_budget(scope, limit)
                else StorageGuard("dependency", scope, limit, cancel)
            )
            with guard_context as guard:
                workspace = self.runtime.shared_projects.prepare(
                    discover_project(external), cancel=guard.cancel if guard else cancel
                )
        scope.include(workspace)
        overrides = json.loads(workspace.overrides_file.read_text())["packages"]
        compiler = self.runtime.toolchains.local_build_identity(lock["toolchain"])
        if compiler is None:
            raise ProjectError("full build compiler identity unavailable")
        compiler_identity = compiler.to_dict()
        package_keys: dict[str, str] = {}
        projects = {item["name"]: item for item in lock["projects"]}

        def key(name: str) -> str:
            if name in package_keys:
                return package_keys[name]
            item = projects[name]
            deps = [
                key(dep)
                if dep in projects
                else self.git_identity(
                    next(entry for entry in lock["git_packages"] if entry["name"] == dep)
                )
                for dep in sorted(item["requires"])
            ]
            # TOML has declarative dependencies. Dynamic Lean configurations retain
            # conservative whole-environment invalidation, never guessed narrower cones.
            identity = {
                "source": item["source"]["source_id"],
                "dependencies": deps,
                "compiler": compiler_identity,
                "platform": platform_compatibility(),
                "dynamic_context": sha256_id("graph", {"lock": lock, "consumer": consumer_context})
                if item["configFile"].endswith(".lean")
                else None,
            }
            package_keys[name] = sha256_id("composition_package", identity)
            return package_keys[name]

        for name in sorted(projects):
            item = projects[name]
            source = self._source(item["source"]["source_id"])
            validate_inventory(source, item["source"])
            destination = self.root / "packages" / key(name)
            # Configuration/build state belongs to the package view, never snapshot sources.
            view = dependency_view(source, destination / "view")
            overrides.append(
                {
                    "name": name,
                    "type": "path",
                    "dir": str(view),
                    "inherited": False,
                    "configFile": item["configFile"],
                }
            )
        artifact_file = path / "artifact-references.json"
        try:
            references = json.loads(artifact_file.read_text())
        except FileNotFoundError:
            # Pre-release dynamic consumers may have unrecorded package keys.
            legacy = (
                any(item["configFile"].endswith(".lean") for item in lock["projects"])
                and sha256_id("composed", lock) in self._registry()["projects"].values()
            )
            references = {
                "schema": "lean-runtime-composition-artifacts/1",
                "buckets": {},
                "legacy_untracked": legacy,
            }
        self._validate_artifact_references(references)
        bucket = (
            "environment"
            if consumer_context is None
            else sha256_id("project_reference", consumer_context)
        )
        ids = [
            Path(item["dir"]).parent.parent.name for item in overrides if item["name"] in projects
        ]
        references["buckets"][bucket] = sorted(set(ids))
        write_json_atomic(artifact_file, references)
        return overrides, workspace

    def _scope(self, lock: dict[str, Any]) -> CompositionStorageScope:
        external = self.root / "environments" / sha256_id("composed", lock) / "external"
        context = ProjectContext(
            external, lock["toolchain"], external / "lakefile.toml", external / "lake-manifest.json"
        )
        workspace = self.runtime.shared_projects.existing_workspace(
            context,
            toolchain_identity=self.runtime.toolchains.local_build_identity(lock["toolchain"]),
            identity_packages=lock["git_packages"],
        )
        if workspace is not None:
            entries = json.loads(workspace.overrides_file.read_text())["packages"]
            seeds = {item["name"]: Path(item["dir"]) for item in entries}
        else:
            seeds = self.runtime.shared_projects.graph_seeds(
                lock["toolchain"], lock["git_packages"]
            )
        scope = CompositionStorageScope(self.runtime, self.root, seeds)
        if workspace is not None:
            scope.mutable.append(workspace.root)
        return scope

    def _execute(self, identifier: str, **kwargs: Any) -> ExecutionResult:
        _, _, lock = self.resolve(identifier)
        scope = self._scope(lock)
        token = self._scope_context.set(scope)
        try:
            limit = kwargs.get("max_build_bytes")
            if limit is None:
                limit = self.storage_limits.build_bytes
            if limit is None:
                return self._execute_unbudgeted(identifier, **kwargs)
            if limit < 0:
                raise ValueError("build allowance must be nonnegative")
            with StorageGuard(
                "build", scope, limit, kwargs.get("cancel"), categories=("dependency",)
            ) as guard:
                kwargs["cancel"] = guard.cancel
                guard_token = self._guard_context.set(guard)
                try:
                    return self._execute_unbudgeted(identifier, **kwargs)
                finally:
                    self._guard_context.reset(guard_token)
        finally:
            self._scope_context.reset(token)

    def _execute_unbudgeted(
        self,
        identifier: str,
        *,
        source: str | None = None,
        imports: List[str] | None = None,
        all_libraries: bool = False,
        all_targets: bool = False,
        timeout: float = 900,
        cancel: threading.Event | None = None,
        max_build_bytes: int | None = None,
        network: NetworkPolicy = "inherit",
    ) -> ExecutionResult:
        if (all_libraries or all_targets) and (source is not None or imports):
            raise ProjectError("select either imports/source or all_libraries, not both")
        if source is None and not imports and not all_libraries and not all_targets:
            raise ProjectError("warm needs explicit imports or all_libraries=True")
        segment = r"(?:[^\W\d][\w']*|«[^«»/\\\r\n]+»)"
        for module in imports or []:
            if not re.fullmatch(segment + r"(?:\." + segment + ")*", module):
                raise ProjectError("invalid import module")
        env_id, path, lock = self.resolve(identifier)
        with self._lock("lifecycle", cancel), self._lock(env_id, cancel):
            before = owned_bytes(self._active_scope(), cancel=cancel)
            overrides, workspace = self._prepare(path, lock, cancel)
            build_root = path / "consumer"
            library = "CompositionMain" if source is not None or imports else None
            write_lake_root(
                build_root,
                name="composition_consumer",
                toolchain=lock["toolchain"],
                packages=overrides,
                library=library,
            )
            if library:
                (build_root / "CompositionMain.lean").write_text(
                    source
                    if source is not None
                    else "\n".join("import " + item for item in imports or [])
                )
            override_file = path / "overrides.json"
            write_json_atomic(override_file, {"version": "1.1.0", "packages": overrides})
            if library:
                targets = ["CompositionMain:leanArts"]
            elif all_libraries or all_targets:
                targets = [
                    f"@{item['name']}/{lean_name(lib['name'])}:leanArts"
                    for item in lock["projects"]
                    for lib in item["libraries"]
                ]
            else:
                raise ProjectError("warm needs explicit imports or all_libraries=True")
            if all_targets:
                targets += ["@" + item["name"] for item in lock["projects"]]
            command = self.runtime.toolchains.command(
                lock["toolchain"], "lake", f"--packages={override_file}", "build", *targets
            )
            # Include snapshot build keys in the same ordered package-lock protocol.
            extra = tuple(
                Path(item["dir"]).parent.parent.name
                for item in overrides
                if item["name"] in {p["name"] for p in lock["projects"]}
            )
            workspace = SharedProjectWorkspace(
                workspace.workspace_id,
                workspace.root,
                workspace.overrides_file,
                workspace.reused,
                tuple(item["name"] for item in overrides),
                (*workspace.package_ids, *extra),
            )
            environment = self.runtime.toolchains.environment_for(lock["toolchain"])
            environment.update(
                LAKE_NO_CACHE="true",
                LAKE_ARTIFACT_CACHE="false",
                LAKE_CACHE_DIR=str(self.root / "lake-cache"),
            )
            with self.runtime.shared_projects.build_lock(workspace, cancel=cancel):
                result = self.runtime._raw_result(
                    command,
                    cwd=build_root,
                    toolchain=lock["toolchain"],
                    source_digest="",
                    policy=ExecutionPolicy(
                        timeout_seconds=timeout, max_output_bytes=1_000_000, network=network
                    ),
                    logical_command=("lake", "build", *targets),
                    environment=environment,
                    cancel=cancel,
                )
            status = json.loads((path / "status.json").read_text())
            state = (
                (
                    "build_ready"
                    if all_targets
                    else "libraries_ready"
                    if all_libraries
                    else "check_ready"
                )
                if result.ok
                else status["state"]
            )
            rank = {
                "sources_available": 0,
                "check_ready": 1,
                "libraries_ready": 2,
                "build_ready": 3,
            }
            if result.ok and rank[status["state"]] > rank[state]:
                state = status["state"]
            status.update(
                state=state,
                builds=[
                    *status.get("builds", [])[-19:],
                    {
                        "ok": result.ok,
                        "targets": targets,
                        "elapsed_seconds": result.elapsed_seconds,
                        "sampled_peak_growth_bytes": getattr(
                            self._guard_context.get(), "peak_growth_bytes", None
                        ),
                        "growth_bytes": max(
                            0,
                            owned_bytes(self._active_scope()) - before,
                        ),
                    },
                ],
            )
            write_json_atomic(path / "status.json", status)
            return result

    def warm(
        self,
        identifier: str,
        *,
        imports: List[str] | None = None,
        all_libraries: bool = False,
        all_targets: bool = False,
        timeout: float = 900,
        cancel: threading.Event | None = None,
        max_build_bytes: int | None = None,
        network: NetworkPolicy = "inherit",
    ) -> ExecutionResult:
        return self._execute(
            identifier,
            imports=imports,
            all_libraries=all_libraries,
            all_targets=all_targets,
            timeout=timeout,
            cancel=cancel,
            max_build_bytes=max_build_bytes,
            network=network,
        )

    def check(
        self,
        identifier: str,
        source: str,
        *,
        timeout: float = 300,
        cancel: threading.Event | None = None,
        max_build_bytes: int | None = None,
        network: NetworkPolicy = "inherit",
    ) -> ExecutionResult:
        return self._execute(
            identifier,
            source=source,
            timeout=timeout,
            cancel=cancel,
            max_build_bytes=max_build_bytes,
            network=network,
        )

    @budgeted("dependency", lambda self, _args: [self.root])
    def init_project(
        self,
        identifier: str,
        destination: str | Path,
        *,
        name: str = "Research",
        apply: bool = False,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        env_id, path, lock = self.resolve(identifier)
        target = Path(destination).expanduser().absolute()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ProjectError("project name must be a Lean identifier")
        if target.exists():
            raise ProjectError("new project destination must not exist")
        preview = {
            "environment_id": env_id,
            "destination": str(target),
            "name": name,
            "toolchain": lock["toolchain"],
            "apply": apply,
        }
        if not apply:
            return preview
        with self._lock("lifecycle", cancel), self._lock(env_id, cancel):
            overrides, _ = self._prepare(path, lock, cancel, consumer_context=str(target))
            target.parent.mkdir(parents=True, exist_ok=True)
            # Stable final paths, not staging paths, participate in Lake trace reuse.
            token = sha256_id("project_reference", str(target))
            self._reference("projects", token, env_id)
            try:
                with staged_tree(target.parent, None) as stage:
                    local = stage / ".lake" / "packages"
                    local.mkdir(parents=True)
                    entries = []
                    linked: dict[str, str] = {}
                    for item in overrides:
                        raw = raw_name(item["name"])
                        entry = {**item, "dir": ".lake/packages/" + raw}
                        link = local / raw
                        prior = linked.get(raw.casefold())
                        if prior is not None and os.path.lexists(link):
                            if not _case_folded_alias(lock, prior, item["name"]):
                                raise ProjectError(
                                    f"packages {prior} and {item['name']} collide on a "
                                    "case-insensitive filesystem"
                                )
                            entries.append(entry)
                            continue
                        link_directory(Path(item["dir"]), link)
                        linked[raw.casefold()] = item["name"]
                        entries.append(entry)
                    write_lake_root(
                        stage,
                        name=name.lower(),
                        toolchain=lock["toolchain"],
                        packages=entries,
                        library=name,
                    )
                    (stage / f"{name}.lean").write_text(
                        "-- Import libraries from the pinned environment here.\n"
                    )
                    (stage / ".gitignore").write_text(".lake/\n")
                    write_json_atomic(
                        stage / PIN,
                        {
                            "schema": "lean-runtime-project-environment/1",
                            "environment_id": env_id,
                            "lock": lock,
                        },
                    )
                    (stage / "AGENTS.md").write_text(
                        "# Lean project\n\nUse `lean-runtime build`. "
                        "The exact composed environment is pinned in "
                        + PIN
                        + ". Do not run lake update or edit shared dependencies.\n"
                    )
                    if not publish_tree(stage, target):
                        raise ProjectError("destination appeared during initialization")
            except BaseException:
                self._reference("projects", token, None)
                raise
        return preview

    @budgeted("dependency", lambda self, _args: [self.root])
    def attach(
        self, destination: str | Path, *, apply: bool = False, cancel: threading.Event | None = None
    ) -> dict[str, Any]:
        target = Path(destination).expanduser().resolve()
        pin = json.loads((target / PIN).read_text())
        env_id = pin["environment_id"]
        if (
            pin.get("schema") != "lean-runtime-project-environment/1"
            or sha256_id("composed", pin["lock"]) != env_id
        ):
            raise ProjectError("invalid project environment pin")
        self.resolve(env_id)  # Never silently acquire/re-resolve a pinned environment.
        if not apply:
            return {"environment_id": env_id, "destination": str(target), "apply": False}
        with self._lock("lifecycle", cancel), self._lock(env_id, cancel):
            _, path, lock = self.resolve(env_id)
            overrides, _ = self._prepare(path, lock, cancel, consumer_context=str(target))
            local = target / ".lake" / "packages"
            local.mkdir(parents=True, exist_ok=True)
            for item in overrides:
                link = local / raw_name(item["name"])
                if link.exists() or link.is_symlink():
                    if link.resolve() == Path(item["dir"]).resolve():
                        continue
                    raise ProjectError("refusing to replace changed project package attachment")
                link_directory(Path(item["dir"]), link)
            self._reference("projects", sha256_id("project_reference", str(target)), env_id)
        return {"environment_id": env_id, "destination": str(target), "apply": True}

    def build_project(self, destination: str | Path, **kwargs: Any) -> ExecutionResult:
        target = Path(destination).expanduser().resolve()
        pin = json.loads((target / PIN).read_text())
        _, _, lock = self.resolve(pin["environment_id"])
        scope = self._scope(lock)
        scope.mutable.append(target)
        token = self._scope_context.set(scope)
        try:
            limit = self.storage_limits.build_bytes
            if limit is None:
                return self._build_project(destination, **kwargs)
            with StorageGuard(
                "build", scope, limit, kwargs.get("cancel"), categories=("dependency",)
            ) as guard:
                kwargs["cancel"] = guard.cancel
                return self._build_project(destination, **kwargs)
        finally:
            self._scope_context.reset(token)

    def _build_project(
        self,
        destination: str | Path,
        *,
        targets: Any = (),
        after_build: Callable[[threading.Event | None], ExecutionResult] | None = None,
        timeout: float = 900,
        policy: ExecutionPolicy | None = None,
        cancel: threading.Event | None = None,
    ) -> ExecutionResult:
        """Build an ordinary generated project under shared package write locks."""
        target = Path(destination).expanduser().resolve()
        pin = json.loads((target / PIN).read_text())
        env_id, path, lock = self.resolve(pin["environment_id"])
        if pin.get("schema") != "lean-runtime-project-environment/1" or pin.get("lock") != lock:
            raise ProjectError("project environment pin changed or is invalid")
        if (target / "lean-toolchain").read_text().strip() != lock["toolchain"]:
            raise ProjectError("project toolchain differs from its exact environment")
        with self._lock("lifecycle", cancel), self._lock(env_id, cancel):
            overrides, workspace = self._prepare(path, lock, cancel, consumer_context=str(target))
            if (target / "lakefile.lean").exists():
                raise ProjectError(
                    "composed project requires its declarative pinned Lake configuration"
                )
            configuration = tomllib.loads((target / "lakefile.toml").read_text())
            requirements = configuration.get("require", [])
            expected_requirements = {
                raw_name(item["name"]): ".lake/packages/" + raw_name(item["name"])
                for item in overrides
            }
            actual_requirements = {item["name"]: item.get("path") for item in requirements}
            if (
                actual_requirements != expected_requirements
                or len(requirements) != len(expected_requirements)
                or any(set(item) != {"name", "path"} for item in requirements)
            ):
                raise ProjectError(
                    "project configuration differs from its pinned environment; "
                    "explicit migration required"
                )
            manifest = json.loads((target / "lake-manifest.json").read_text())
            expected = {
                item["name"]: ".lake/packages/" + raw_name(item["name"]) for item in overrides
            }
            actual = {
                item["name"]: item.get("dir")
                for item in manifest["packages"]
                if item.get("type") == "path"
            }
            if actual != expected or len(manifest["packages"]) != len(expected):
                raise ProjectError(
                    "project manifest differs from its exact environment; "
                    "explicit migration required"
                )
            local = target / ".lake" / "packages"
            local.mkdir(parents=True, exist_ok=True)
            linked: dict[str, str] = {}
            for item in overrides:
                raw = raw_name(item["name"])
                link = local / raw
                if link.exists() or link.is_symlink():
                    if link.resolve() != Path(item["dir"]).resolve() and not _case_folded_alias(
                        lock, linked.get(raw.casefold()), item["name"]
                    ):
                        raise ProjectError("project dependency attachment changed")
                else:
                    link_directory(Path(item["dir"]), link)
                linked.setdefault(raw.casefold(), item["name"])
            extra = tuple(
                Path(item["dir"]).parent.parent.name
                for item in overrides
                if item["name"] in {p["name"] for p in lock["projects"]}
            )
            workspace = SharedProjectWorkspace(
                workspace.workspace_id,
                workspace.root,
                workspace.overrides_file,
                workspace.reused,
                tuple(expected),
                (*workspace.package_ids, *extra),
            )
            self._reference("projects", sha256_id("project_reference", str(target)), env_id)
            command = self.runtime.toolchains.command(lock["toolchain"], "lake", "build", *targets)
            environment = self.runtime.toolchains.environment_for(lock["toolchain"])
            environment.update(
                LAKE_NO_CACHE="true",
                LAKE_ARTIFACT_CACHE="false",
                LAKE_CACHE_DIR=str(self.root / "lake-cache"),
            )
            with self.runtime.shared_projects.build_lock(workspace, cancel=cancel):
                result = self.runtime._raw_result(
                    command,
                    cwd=target,
                    toolchain=lock["toolchain"],
                    source_digest="",
                    policy=policy or ExecutionPolicy(timeout_seconds=timeout),
                    logical_command=("lake", "build", *targets),
                    environment=environment,
                    cancel=cancel,
                )

                return after_build(cancel) if result.ok and after_build is not None else result

    def check_project_source(
        self,
        destination: Path,
        source: str,
        operation: Callable[[threading.Event | None], ExecutionResult],
        *,
        timeout: float,
        policy: ExecutionPolicy | None = None,
        cancel: threading.Event | None = None,
    ) -> ExecutionResult:
        from .project_execution import _source_imports

        imports = _source_imports(source)
        targets = tuple(
            "+" + module + ":leanArts"
            for module in imports
            if module.split(".")[0] not in {"Init", "Lean", "Lake", "Std"}
        )
        # Lake builds just the imported dependency cones, not every environment member.
        # The callback and any local-import recovery remain inside the same write lease.
        return self.build_project(
            destination, targets=targets, after_build=operation, timeout=timeout, cancel=cancel
        )

    def references(
        self, *, alias: str | None = None, project: str | None = None, apply: bool = False
    ) -> dict[str, Any]:
        refs = self._registry()
        if alias is None and project is None:
            return refs
        if alias is not None and not ALIAS.fullmatch(alias):
            raise ProjectError("invalid alias")
        key = (
            alias
            if alias is not None
            else sha256_id("project_reference", str(Path(project or "").expanduser().resolve()))
        )
        kind = "aliases" if alias is not None else "projects"
        result = {"kind": kind, "key": key, "environment_id": refs[kind].get(key), "apply": apply}
        if apply:
            with self._lock("lifecycle"):
                self._reference(kind, key, None)
        return result

    @staticmethod
    def _validate_artifact_references(value: dict[str, Any]) -> None:
        try:
            if (
                value["schema"] != "lean-runtime-composition-artifacts/1"
                or not isinstance(value["buckets"], dict)
                or not isinstance(value["legacy_untracked"], bool)
            ):
                raise ValueError
            for bucket, ids in value["buckets"].items():
                if bucket != "environment" and not re.fullmatch(
                    r"project_reference_[0-9a-f]{64}", bucket
                ):
                    raise ValueError
                if not isinstance(ids, list) or any(
                    not re.fullmatch(r"composition_package_[0-9a-f]{64}", item) for item in ids
                ):
                    raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ProjectError("corrupt composition artifact references; cleanup refused") from exc

    def clean(self, *, apply: bool = False) -> dict[str, Any]:
        def candidates() -> dict[str, Any]:
            refs = self._registry()
            keep = set(refs["aliases"].values()) | set(refs["projects"].values())
            all_envs = {
                p.name for p in (self.root / "environments").glob("composed_*") if p.is_dir()
            }
            for env_id in sorted(all_envs | keep):
                self.resolve(env_id)
            retained = []
            sources: set[str] = set()
            packages: set[str] = set()
            legacy = False
            for env_id in sorted(keep & all_envs):
                _, _, lock = self.resolve(env_id)
                sources.update(p["source"]["source_id"] for p in lock["projects"])
                retained.append(env_id)
                try:
                    artifact_refs = json.loads(
                        (
                            self.root / "environments" / env_id / "artifact-references.json"
                        ).read_text()
                    )
                except FileNotFoundError:
                    legacy = True
                    continue
                except (OSError, ValueError) as exc:
                    raise ProjectError(
                        "composition artifact references unreadable; cleanup refused"
                    ) from exc
                self._validate_artifact_references(artifact_refs)
                legacy |= artifact_refs["legacy_untracked"]
                for bucket, ids in artifact_refs["buckets"].items():
                    if bucket == "environment" or refs["projects"].get(bucket) == env_id:
                        packages.update(ids)
            # Keep precisely referenced views. Missing legacy evidence retains all views;
            # no deletion authority is inferred from a missing consumer path.
            paths = [self.root / "environments" / value for value in sorted(all_envs - keep)]
            paths += [
                p for p in (self.root / "sources").glob("snapshot_*") if p.name not in sources
            ]
            if not legacy:
                paths += [
                    p
                    for p in (self.root / "packages").glob("composition_package_*")
                    if p.name not in packages
                ]
            return {
                "paths": [str(p) for p in paths],
                "bytes": owned_bytes(paths),
                "retained": retained,
                "artifact_retention": "legacy evidence: all views retained"
                if legacy
                else "exact references",
                "apply": apply,
            }

        if not apply:
            return candidates()
        with self._lock("lifecycle"):
            result = candidates()
            for path in result["paths"]:
                remove_tree(Path(path))
            return result
