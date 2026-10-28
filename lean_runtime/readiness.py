"""Read-only multi-project readiness, not a compilation verdict or adoption."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._git import git_head
from ._project_identity import canonical_git_url, package_subdir
from ._relpath import packages_directory
from .errors import LeanRuntimeError, ProjectError
from .project_sharing import discover_shareable_projects
from .projects import discover_project
from .shared_projects import _git_clean_project_seed

if TYPE_CHECKING:
    from .runtime import Runtime


def project_readiness(runtime: Runtime, roots: list[Path], *, recursive: bool) -> dict[str, Any]:
    """Inspect exact pins without executing Lean, accessing network, or writing caches.

    Source readiness does not assert prebuilt artifact compatibility. Different
    independent graphs are reported as pool conflicts, not rejected projects.
    """
    projects: list[dict[str, Any]] = []
    versions: dict[str, dict[str, list[str]]] = {}
    seen: set[Path] = set()
    for root in roots:
        try:
            selected = discover_shareable_projects(root, recursive=recursive)
        except (LeanRuntimeError, OSError, ValueError) as exc:
            projects.append(
                {
                    "root": str(root),
                    "inputs_ready": False,
                    "blockers": [{"code": "invalid_project", "message": str(exc)}],
                }
            )
            continue
        for path in selected:
            if path in seen:
                continue
            seen.add(path)
            result: dict[str, Any] = {"root": str(path), "blockers": [], "packages": []}
            projects.append(result)
            blockers = result["blockers"]
            try:
                context = discover_project(path)
                capabilities = runtime.toolchains.local_capabilities(context.toolchain)
                result["toolchain"] = capabilities
                if not capabilities["build_ready"]:
                    blockers.append(
                        {"code": "missing_build_toolchain", "toolchain": context.toolchain}
                    )
                manifest_path = context.current_manifest()
                if manifest_path is None:
                    raise ProjectError("missing lake-manifest.json; exact dependencies unknown")
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                local_packages = path.joinpath(*packages_directory(manifest).parts)
                entries = manifest.get("packages")
                if not isinstance(entries, list):
                    raise ProjectError("manifest packages must be an array")
                names: set[str] = set()
                for entry in entries:
                    if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                        raise ProjectError("malformed package entry")
                    name = entry["name"]
                    if not name or name in names or Path(name).name != name or name in {".", ".."}:
                        raise ProjectError(f"unsafe or duplicate package name: {name!r}")
                    names.add(name)
                seeds = runtime.shared_projects.graph_seeds(context.toolchain, entries)
                registered, _ = runtime.shared_projects.registered_graph_seeds(
                    context.toolchain, entries, exclude_root=path
                )
                seeds = {**registered, **seeds}
                for entry in entries:
                    name = entry["name"]
                    package: dict[str, Any] = {
                        "name": name,
                        "type": entry.get("type"),
                        "artifacts": "not_verified",
                    }
                    result["packages"].append(package)
                    if entry.get("type") == "git":
                        rev, url = entry.get("rev"), entry.get("url")
                        if not isinstance(rev, str) or not isinstance(url, str):
                            raise ProjectError(f"{name}: missing exact revision or URL")
                        package.update(revision=rev, url=url, subdir=entry.get("subDir"))
                        identity = json.dumps([canonical_git_url(url), rev, entry.get("subDir")])
                        versions.setdefault(name, {}).setdefault(identity, []).append(str(path))
                        candidate = None
                        for source in (
                            local_packages / name,
                            seeds.get(name),
                            runtime.shared_projects.cached_source(entry),
                        ):
                            if (
                                source is not None
                                and git_head(source) == rev
                                and _git_clean_project_seed(source)
                            ):
                                subdir = package_subdir(entry)
                                source = source / subdir if subdir else source
                                if source.is_dir():
                                    candidate = source
                                    break
                        package["source_local"] = candidate is not None
                        package["path"] = str(candidate) if candidate is not None else None
                        if candidate is None:
                            blockers.append(
                                {
                                    "code": "missing_exact_dependency",
                                    "package": name,
                                    "revision": rev,
                                    "url": url,
                                }
                            )
                    elif entry.get("type") == "path":
                        directory = entry.get("dir")
                        if not isinstance(directory, str):
                            raise ProjectError(f"{name}: missing path directory")
                        dependency = (path / directory).resolve()
                        package.update(path=str(dependency), source_local=dependency.is_dir())
                        if not dependency.is_dir():
                            blockers.append(
                                {
                                    "code": "missing_path_dependency",
                                    "package": name,
                                    "path": str(dependency),
                                }
                            )
                        elif (dependency / "lean-toolchain").is_file():
                            other = discover_project(dependency)
                            nested_manifest = other.current_manifest()
                            if nested_manifest is not None:
                                nested = json.loads(nested_manifest.read_text(encoding="utf-8"))
                                outer = {item["name"]: item for item in entries}
                                nested_entries = nested.get("packages", [])
                                if not isinstance(nested_entries, list):
                                    raise ProjectError(
                                        "path dependency manifest packages must be an array"
                                    )
                                for nested_entry in nested_entries:
                                    if not isinstance(nested_entry, dict):
                                        raise ProjectError(
                                            "malformed path dependency manifest entry"
                                        )
                                    current = outer.get(nested_entry.get("name"))
                                    if (
                                        current is not None
                                        and current.get("type") == "git"
                                        and nested_entry.get("type") == "git"
                                        and (
                                            current.get("rev") != nested_entry.get("rev")
                                            or canonical_git_url(str(current.get("url", "")))
                                            != canonical_git_url(str(nested_entry.get("url", "")))
                                        )
                                    ):
                                        blockers.append(
                                            {
                                                "code": "path_graph_conflict",
                                                "package": name,
                                                "dependency": nested_entry["name"],
                                            }
                                        )
                            if other.toolchain != context.toolchain:
                                blockers.append(
                                    {
                                        "code": "path_toolchain_conflict",
                                        "package": name,
                                        "toolchain": other.toolchain,
                                    }
                                )
                    else:
                        raise ProjectError(f"unsupported source type for {name}")
            except (LeanRuntimeError, OSError, ValueError, TypeError, KeyError) as exc:
                blockers.append({"code": "invalid_project", "message": str(exc)})
            result["inputs_ready"] = not blockers
    from ._adoption_planning import build_adoption_plan

    preview: dict[str, Any] | None = None
    preview_error: str | None = None
    preview_roots = tuple(
        Path(item["root"])
        for item in projects
        if not any(blocker["code"] == "invalid_project" for blocker in item["blockers"])
    )
    if preview_roots:
        try:
            preview = build_adoption_plan(
                preview_roots[0],
                recursive=False,
                shared=runtime.shared_projects,
                jobs=1,
                project_roots=tuple(sorted(preview_roots)),
            ).to_dict()
        except (LeanRuntimeError, OSError, ValueError, TypeError, KeyError) as exc:
            preview_error = str(exc)
    conflicts = [
        {
            "package": name,
            "variants": [
                {"identity": json.loads(identity), "projects": paths}
                for identity, paths in choices.items()
            ],
        }
        for name, choices in sorted(versions.items())
        if len(choices) > 1
    ]
    return {
        "schema": "lean-runtime-readiness/1",
        "projects": projects,
        "inputs_ready": bool(projects) and all(item["inputs_ready"] for item in projects),
        "pool_conflicts": conflicts,
        "artifact_compatibility": "not_verified",
        "verdict": None,
        "storage_limits": runtime.storage_limits.to_dict(),
        "shared_adoption_preview": preview,
        "storage_preview_error": preview_error,
        "build_output_bytes": None,
    }
