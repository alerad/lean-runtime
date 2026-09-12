"""Read-only action planning; execution repeats the shared selection before mutation."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import cache, partial
from pathlib import Path
from typing import Any

from . import project_sharing as sharing
from ._adoption_estimates import (
    AdoptionEstimates,
    CostComponent,
    EstimateIssue,
    PackageAdoptionAction,
    aggregate_issues,
)
from ._adoption_workers import adoption_jobs
from ._project_identity import (
    canonical_git_url,
    compute_package_identity,
    entry_identity,
    package_subdir,
    resolved_path_entries,
)
from ._published_estimates import (
    PublishedPackageSize,
    SizeReferenceRequest,
    SizeReferenceUse,
    select_size_reference,
)
from .events import submit
from .package_ids import package_directory_id
from .project_sharing import (
    AdoptionPlan,
    ProjectAdoption,
    _manifest_packages,
    _packages_directory,
    attachment_matches_workspace,
    discover_shareable_projects,
)
from .projects import discover_project
from .serialization import sha256_id
from .shared_projects import _MANAGED_PROJECT_CONFIG, SharedProjectManager, SourceSelectionInventory
from .store import TreeUsage, tree_usage
from .toolchains import ToolchainBuildIdentity


def source_usage(root: Path) -> TreeUsage:
    """Exactly the source filter used by shared source/package materialization."""

    def include(relative: Path) -> bool:
        if ".lake" in relative.parts or relative == Path(".lean-runtime-package.json"):
            return False
        if relative == Path("lean-runtime.toml"):
            return (root / relative).read_text(encoding="utf-8") != _MANAGED_PROJECT_CONFIG
        return True

    return tree_usage(root, include=include)


def artifact_usage(root: Path) -> TreeUsage:
    return tree_usage(
        root,
        include=lambda p: (
            (not p.parts or p.parts[0] in {"lib", "ir", "bin"})
            and not p.name.endswith((".trace", ".hash"))
        ),
    )


def build_adoption_plan(
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
    workers = adoption_jobs(jobs)
    roots = discover_shareable_projects(path, recursive=recursive)
    events = shared.events if shared is not None else None
    if events is not None:
        events.emit(
            "adopt.workers_selected",
            f"Planning adoption with {workers} worker(s)",
            phase="adopt-plan",
            jobs=workers,
        )
    # Only read-only work runs in this pool. Context propagation preserves the
    # caller's progress emitter. Consume results in discovery order.
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="adopt-plan") as pool:
        futures = [submit(pool, sharing.inspect_adoption, root) for root in roots]
        inspected: list[ProjectAdoption] = []
        for index, (root, future) in enumerate(zip(roots, futures, strict=True), start=1):
            if events is not None:
                events.emit(
                    "adopt.inspect_started",
                    f"Inspecting {root.name} ({index}/{len(roots)})",
                    phase="adopt-plan",
                    project=str(root),
                    name=root.name,
                    current=index,
                    total=len(roots),
                )
            inspected.append(future.result())
        projects = tuple(inspected)
        candidates = [project for project in projects if project.ready]
        contexts = {project.root: discover_project(project.root) for project in candidates}
        manifests = {root: _manifest_packages(context) for root, context in contexts.items()}
        # A root-resolved path may appear under several aliases and in many
        # projects. Hash it once for this plan, never across execution or plans.
        paths = sorted(
            {
                (root / str(entry["dir"])).resolve()
                for root, entries in manifests.items()
                for entry in entries
                if entry["type"] == "path"
            }
        )
        digests = {p: submit(pool, sharing.source_snapshot_digest, p) for p in paths}
        path_digests = {p: future.result() for p, future in digests.items()}
    source_inventory = SourceSelectionInventory(path_digests)
    inventories = cache(tree_usage)
    replacements = cache(partial(tree_usage, missing_ok=True))
    sources = cache(source_usage)
    artifacts = cache(artifact_usage)
    actions: dict[str, PackageAdoptionAction] = {}
    components: dict[str, CostComponent] = {}
    issues: list[EstimateIssue] = []
    references: dict[str, dict[str, Any]] = {}
    planned_sources: dict[str, TreeUsage | None] = {}
    definite_sources: set[str] = set()
    samples: tuple[PublishedPackageSize, ...] | None = None
    toolchains: dict[str, ToolchainBuildIdentity | None] = {}
    inspected_by_root = {p.root: p for p in projects}

    def cost(
        key: str,
        action: str,
        category: str,
        usage: TreeUsage | None,
        project: str,
        package: str,
        *,
        transfer: bool = False,
    ) -> None:
        if key in components:
            old = components[key]
            components[key] = replace(old, action_ids=tuple(sorted(set((*old.action_ids, action)))))
            return
        components[key] = CostComponent(
            key,
            (action,),
            category,
            "payload_bytes" if transfer else "logical_bytes",
            "measured_local" if usage else "unknown",
            usage.logical_bytes if usage else None,
            usage.complete if usage else False,
        )
        if usage is not None and not usage.complete:
            issues.append(
                EstimateIssue(
                    "incomplete_inventory",
                    key,
                    (project,),
                    (package,),
                    "; ".join(usage.errors),
                    "size",
                )
            )

    for index, project in enumerate(candidates, start=1):
        if events is not None:
            events.emit(
                "adopt.identity_started",
                f"Resolving dependency identities for {project.root.name} "
                f"({index}/{len(candidates)})",
                phase="adopt-plan",
                project=str(project.root),
                name=project.root.name,
                current=index,
                total=len(candidates),
            )
        context = contexts[project.root]
        project_label = str(context.root)
        entries = manifests[project.root]
        identity_entries = resolved_path_entries(context, entries, digest=path_digests.__getitem__)
        effective = {
            str(e["name"]): entry_identity(i)
            for e, i in zip(entries, identity_entries, strict=True)
        }
        if context.toolchain not in toolchains:
            toolchains[context.toolchain] = (
                shared.local_toolchain_build_identity(context.toolchain) if shared else None
            )
        compiler = toolchains[context.toolchain]
        project_action = "project:" + project_label
        actions[project_action] = PackageAdoptionAction(
            project_action,
            (project_label,),
            "",
            "validate_attachment" if project.attached else "attach_project",
        )
        if compiler is None:
            issues.append(
                EstimateIssue(
                    "toolchain_identity",
                    context.toolchain,
                    (project_label,),
                    (),
                    "local Lean/Lake build identity unavailable; compatibility unresolved",
                    "compatibility",
                )
            )
            cost(
                "toolchain:" + context.toolchain,
                project_action,
                "toolchains",
                None,
                project_label,
                "",
            )
            cost(
                "toolchain-transfer:" + context.toolchain,
                project_action,
                "toolchain_transfer",
                None,
                project_label,
                "",
                transfer=True,
            )
        healthy = False
        if project.attached and shared is not None:
            workspace = shared.existing_workspace(
                context, toolchain_identity=compiler, identity_packages=identity_entries
            )
            healthy = workspace is not None and attachment_matches_workspace(context, workspace)
            if not healthy:
                inspected_by_root[project.root] = replace(
                    project,
                    attached=False,
                    warnings=(
                        *project.warnings,
                        "attachment is stale or unverified; execution will revalidate",
                    ),
                )
        actions[project_action] = replace(
            actions[project_action], kind="verify_attachment" if healthy else "attach_project"
        )
        if not healthy:
            cost(
                "replace:" + project_label,
                project_action,
                "local_replacement",
                replacements(_packages_directory(context)),
                project_label,
                "",
            )
            cost(
                "metadata:" + project_label,
                project_action,
                "workspace_metadata",
                None,
                project_label,
                "",
            )
            issues.append(
                EstimateIssue(
                    "workspace_metadata",
                    project_label,
                    (project_label,),
                    (),
                    "workspace/attachment metadata and Lake probe output are unpriced",
                    "size",
                )
            )
        decisions = (
            shared.preparation_inputs(
                context,
                entries,
                effective_entries=effective,
                toolchain_identity=compiler,
                inventory=source_inventory,
            )
            if shared
            else {}
        )
        for entry in entries:
            if entry["type"] != "git":
                continue
            name = str(entry["name"])
            decision = decisions.get(name)
            sid = sha256_id(
                "project_source",
                {"url": canonical_git_url(str(entry["url"])), "revision": entry["rev"]},
            )
            # Compatibility variants stay distinct even when their size references coincide.
            aid = sha256_id(
                "adoption_action",
                {
                    "toolchain": context.toolchain,
                    "package": entry_identity(entry),
                    "graph": [effective[n] for n in sorted(effective)],
                },
            )
            reusable = decision.reusable if decision else None
            seed = decision.seed if decision else _packages_directory(context) / name
            kind, source = (
                ("reuse_shared", reusable)
                if reusable is not None
                else shared.source_input(entry, seed, source_inventory)
                if shared
                else ("unresolved", None)
            )
            source_strategy = kind
            subdir = package_subdir(entry)
            build = (seed / subdir if subdir else seed) / ".lake" / "build"
            artifact = (
                "none"
                if not build.is_dir()
                else "preserve"
                if decision and decision.artifact_miss is None
                else "unknown"
                if compiler is None and build.is_dir()
                else "omit"
                if build.is_dir()
                else "none"
            )
            pid = None
            if reusable is not None:
                kind, pid = "reuse_shared", reusable.name
                aid = "package:" + pid
            elif compiler is None:
                kind = "unresolved"
            elif kind in {"import_local", "use_cached_source"} and source is not None:
                package_root = source / subdir if subdir else source
                pid = package_directory_id(
                    compute_package_identity(
                        context=context,
                        entry=entry,
                        source_package=package_root,
                        effective_entries=effective,
                        toolchain_identity=compiler,
                    )
                )
                aid = "package:" + pid
            if (
                kind not in {"unresolved", "reuse_shared", "use_cached_source"}
                and sid in definite_sources
            ):
                kind = "use_planned_source"
            prior = actions.get(aid)
            if prior is not None:
                actions[aid] = replace(
                    prior, projects=tuple(sorted(set((*prior.projects, project_label))))
                )
                continue
            action = PackageAdoptionAction(
                aid,
                (project_label,),
                name,
                kind,
                pid,
                sid,
                str(shared.sources / sid)
                if kind == "use_planned_source" and shared
                else str(source)
                if source
                else None,
                str(shared.packages / pid) if shared and pid else None,
                artifact,
                (decision.artifact_miss,) if decision and decision.artifact_miss else (),
            )
            actions[aid] = action
            if reusable is not None:
                cost(
                    "reuse:" + reusable.name,
                    aid,
                    "shared_reused",
                    inventories(reusable),
                    project_label,
                    name,
                )
                continue
            if kind == "unresolved":
                cost("package:" + aid, aid, "package_source_trees", None, project_label, name)
                # Source creation is also conditional on resolving compatibility.
                if source_strategy != "use_cached_source":
                    planned_sources.setdefault(sid, None)
                    cost("source:" + sid, aid, "source_objects", None, project_label, name)
            else:
                usage = (
                    sources(source)
                    if source is not None and kind in {"import_local", "use_cached_source"}
                    else None
                )
                # All package trees use the one source object selected first by
                # execution. Equal revisions need not have equal .git inventories.
                if kind != "use_cached_source":
                    usage = planned_sources.setdefault(sid, usage)
                cost("package:" + aid, aid, "package_source_trees", usage, project_label, name)
                if kind != "use_cached_source":
                    cost("source:" + sid, aid, "source_objects", usage, project_label, name)
                if usage is None:
                    issues.append(
                        EstimateIssue(
                            "source_inventory",
                            sid,
                            (project_label,),
                            (name,),
                            "exact source materialization has no measured inventory",
                            "size",
                        )
                    )
                if kind == "fetch_source":
                    cost(
                        "fetch:" + sid,
                        aid,
                        "git_transfer",
                        None,
                        project_label,
                        name,
                        transfer=True,
                    )
                    issues.append(
                        EstimateIssue(
                            "git_transfer",
                            sid,
                            (project_label,),
                            (name,),
                            "Git payload transfer is unpriced",
                            "transfer",
                        )
                    )
                if kind != "use_cached_source":
                    definite_sources.add(sid)
            if artifact == "preserve":
                cost(
                    "artifacts:" + aid,
                    aid,
                    "preserved_artifacts",
                    artifacts(build),
                    project_label,
                    name,
                )
            elif artifact == "unknown":
                cost("artifacts:" + aid, aid, "preserved_artifacts", None, project_label, name)
                issues.append(
                    EstimateIssue(
                        "artifact_compatibility",
                        aid,
                        (project_label,),
                        (name,),
                        "artifact preservation awaits validated compatibility",
                        "compatibility",
                    )
                )
    requests_by_action = {}
    for action in actions.values():
        if action.artifact_decision != "unknown":
            continue
        context = contexts[Path(action.projects[0])]
        entry = next(e for e in manifests[context.root] if e["name"] == action.package)
        requests_by_action[action.action_id] = SizeReferenceRequest(entry, context.toolchain)
    if requests_by_action:
        requests = tuple(requests_by_action.values())
        samples = (
            reference_provider(requests)
            if reference_provider
            else size_samples()
            if size_samples
            else ()
        )
        for aid, request in requests_by_action.items():
            sample = select_size_reference(samples, request.entry, request.toolchain)
            if sample is not None:
                action = actions[aid]
                references[aid] = SizeReferenceUse(
                    (aid,), action.projects, request, sample
                ).to_dict()
    # Project coverage is accumulated on actions, including shared source components.
    expanded = []
    for issue in issues:
        related = [
            a
            for a in actions.values()
            if a.action_id == issue.subject_id or a.source_id == issue.subject_id
        ]
        expanded.append(
            replace(
                issue,
                projects=tuple(
                    sorted(set(issue.projects).union(*(set(a.projects) for a in related)))
                ),
            )
        )
    report = AdoptionEstimates(
        tuple(components[k] for k in sorted(components)),
        aggregate_issues(expanded),
        tuple(references[k] for k in sorted(references)),
    )
    retained, removed, network = report.new_retained, report.local_replacement, report.network
    reused = report.summary({"shared_reused"}).known_bytes
    return AdoptionPlan(
        tuple(inspected_by_root[p.root] for p in projects),
        recursive,
        removed.known_bytes,
        retained.known_bytes + reused,
        reused,
        retained.known_bytes,
        storage_estimate_complete=retained.complete and removed.complete,
        unknown_dependencies=tuple(f"{i.subject_id}: {i.detail}" for i in report.uncertainty),
        download_bytes=network.known_bytes if network.complete else None,
        download_estimate_complete=network.complete,
        new_source_bytes=report.summary({"source_objects"}).known_bytes,
        jobs=workers,
        size_references=report.references,
        actions=tuple(actions[k] for k in sorted(actions)),
        estimates=report,
    )
