"""Human-readable operation costs; publication samples are never additive."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import TYPE_CHECKING

from .policies import format_byte_size

if TYPE_CHECKING:
    from .project_sharing import AdoptionPlan


def render_adoption_plan(plan: AdoptionPlan, *, verbose: bool = False) -> None:
    print(f"Planning workers: {plan.jobs}")
    print(
        f"Found {len(plan.projects)} Lake project(s): "
        f"{plan.ready} ready, {plan.blocked} requiring attention"
    )
    for project in plan.projects:
        state = "attached" if project.attached else "ready" if project.ready else "blocked"
        print(f"  {state:8} {project.root} · {len(project.packages)} packages")
        for blocker in project.blockers:
            print(f"           blocker: {blocker}")
        if verbose:
            for warning in project.warnings:
                print(f"           note: {warning}")
    print("\nAdoption actions")
    for kind, count in sorted(Counter(a.kind for a in plan.actions).items()):
        print(f"  {kind.replace('_', ' ')}: {count}")
    report = plan.estimates
    print("\nStorage — logical content")
    print(
        f"  Local directories replaced: {format_byte_size(plan.current_dependency_bytes)} logical"
    )
    print(f"  Shared content already ready: {format_byte_size(plan.shared_bytes_reused)}")
    print(f"  Known new retained content: {format_byte_size(plan.new_shared_bytes)}")
    if report is not None:
        for category in (
            "source_objects",
            "package_source_trees",
            "preserved_artifacts",
            "restored_artifacts",
            "toolchains",
            "workspace_metadata",
        ):
            summary = report.summary({category})
            print(
                f"    {category.replace('_', ' ')}: {format_byte_size(summary.known_bytes)}"
                + ("" if summary.complete else " known; incomplete")
            )
        names = {action.action_id: action.package for action in plan.actions}
        contributors: Counter[str] = Counter()
        for component in report.components:
            if (
                component.category
                in {
                    "source_objects",
                    "package_source_trees",
                    "preserved_artifacts",
                    "restored_artifacts",
                }
                and component.bytes is not None
            ):
                name = names.get(component.action_ids[0], component.category)
                contributors[name] += component.bytes
        if contributors:
            print("  Largest known retained-content contributors:")
            for name, amount in contributors.most_common(5):
                print(f"    {name}: {format_byte_size(amount)} logical")
        change = report.net_logical_change
        print(
            "  Net logical change: "
            + (
                "incomplete"
                if change is None
                else ("+" if change >= 0 else "-") + format_byte_size(abs(change))
            )
        )
        print("\nNetwork")
        print(
            f"  Known payload transfer: {format_byte_size(report.network.known_bytes)}"
            + ("" if report.network.complete else "; incomplete")
        )
        print("\nMissing evidence")
        groups = defaultdict(list)
        for issue in report.uncertainty:
            groups[(issue.code, issue.impact)].append(issue)
        for (code, impact), issues in sorted(groups.items()):
            projects = {p for issue in issues for p in issue.projects}
            print(
                f"  {len(issues)} {code.replace('_', ' ')} ({impact}), "
                f"affecting {len(projects)} projects"
            )
            if verbose:
                for issue in issues:
                    print(f"    {issue.subject_id}: {issue.detail}")
                    print("      Projects: " + ", ".join(issue.projects))
    print("\nDisk capacity")
    print("  Physical disk recovery: not reliably predictable before adoption")
    print("  Additional working space: unknown (staging, downloads, and allocation strategy)")
    if plan.size_references:
        print(
            "\nRelated published check-artifact sizes are available "
            "for unpriced artifact decisions."
        )
        print(
            "References are excluded from storage and transfer totals. "
            "Actual content overlap is unknown."
        )
        if verbose:
            grouped = defaultdict(list)
            for row in plan.size_references:
                grouped[row["requested_package"]].append(row)
            for name, rows in sorted(grouped.items()):
                sizes = [r["sample"]["artifact_bytes"] for r in rows]
                print(
                    f"  {name}: {len(rows)} variants; references {format_byte_size(min(sizes))}"
                    f"–{format_byte_size(max(sizes))} each"
                )
                for row in rows:
                    sample = row["sample"]
                    print(
                        f"    Requested {row['requested_revision']} "
                        f"({row['requested_toolchain']}); "
                        f"reference {sample['revision']} ({sample['toolchain']}), "
                        f"{sample['registry']} @ {sample['config_digest']}"
                    )
    if not verbose:
        print("Use --verbose for affected projects, donor decisions, and reference provenance.")
    elif plan.actions:
        for action in plan.actions:
            print(f"  {action.package}: {action.kind}; artifacts: {action.artifact_decision}")
            for reason in action.reasons:
                print(f"    {reason}")
