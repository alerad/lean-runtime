"""Minimal command-line interface around the environment compiler."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Any

from .console import styler_for
from .environments import ExecutionCapture
from .errors import LeanRuntimeError, MaterializationError, ProjectError, ResolutionError
from .events import RuntimeEvent
from .lake import ROOT_MODULE
from .lockfiles import EnvironmentLock
from .matrix import load_matrix
from .models import ExecutionResult, PhaseTiming
from .policies import ExecutionPolicy, format_byte_size, parse_byte_size
from .project_sharing import AdoptionPlan, ProjectInitPlan, ProjectUpdatePlan
from .projects import discover_project, project_publication_workflow
from .runtime import Runtime
from .specs import EnvironmentSpec
from .store import CleanupReport, DownloadCleanupReport, StoreStatus
from .timings import render_timings
from .wire import (
    envelope,
    error,
    serialize_comparison_v1,
    serialize_execution_v1,
    serialize_matrix_v1,
    serialize_profile_v1,
    serialize_verify_v1,
)


def _schema_for(command: str) -> str:
    return {
        "verify": "lean-runtime.verify/v1",
        "compare": "lean-runtime.comparison/v1",
        "profile": "lean-runtime.profile/v1",
        "matrix": "lean-runtime.matrix/v1",
        "clean": "lean-runtime.cleanup/v1",
        "inspect": "lean-runtime.inspect/v1",
    }.get(command, "lean-runtime.execution/v1")


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _relative_age(timestamp: str | None) -> str:
    if not timestamp:
        return "unknown"
    try:
        moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    seconds = max(0.0, time.time() - moment.timestamp())
    for unit_seconds, label in ((31_536_000, "y"), (2_592_000, "mo"), (86_400, "d"), (3_600, "h")):
        if seconds >= unit_seconds:
            return f"{int(seconds // unit_seconds)}{label} ago"
    return "recent"


def _render_storage(status: StoreStatus) -> None:
    style = styler_for(sys.stdout)
    print(f"{style.bold('Store')}  {status.home}")
    print()
    rows = (
        ("Environments", status.environments, status.environments_bytes),
        ("Sources", status.sources, status.sources_bytes),
        ("Download cache", status.oci_blobs, status.oci_blobs_bytes),
        ("Shared module CAS", status.cas_artifacts, status.cas_artifacts_bytes),
        ("Project packages", status.project_packages, status.project_packages_bytes),
        ("Toolchains", None, status.toolchains_bytes),
        ("Executions", status.executions, status.executions_bytes),
    )
    for label, count, bytes_used in rows:
        counted = f"{count:>5}" if count is not None else "     "
        size_column = style.cyan(f"{format_byte_size(bytes_used):>10}")
        print(f"  {label:<15}{counted}  {size_column}")
    total_label = style.bold(f"{'Total':<15}")
    total_size = style.bold(f"{format_byte_size(status.bytes_used):>10}")
    free_note = style.dim(f"({format_byte_size(status.bytes_free)} free on disk)")
    print(f"  {total_label}       {total_size}  {free_note}")
    if status.environment_usage:
        print()
        print(style.bold("Largest environments"))
        for usage in status.environment_usage[:5]:
            name = ", ".join(usage.aliases) if usage.aliases else usage.environment_id[:16] + "…"
            details = " · ".join(
                part
                for part in (usage.toolchain, f"last used {_relative_age(usage.last_used_at)}")
                if part
            )
            size_column = style.cyan(f"{format_byte_size(usage.bytes_used):>10}")
            print(f"  {size_column}  {name}  {style.dim(details)}")
    print()
    print(style.dim("Reclaim space: lean-runtime clean            (preview, keeps recent/named)"))
    print(style.dim("               lean-runtime clean --execute --include-downloads"))
    if status.project_packages:
        print(
            style.dim("Shared project packages are retained for reuse; cleanup is not automatic.")
        )


def _render_cleanup(environments: CleanupReport, downloads: DownloadCleanupReport | None) -> None:
    style = styler_for(sys.stdout)
    retained_note = style.dim(
        f"Retained {len(environments.retained)} environment(s) (named, recent, or in use)."
    )
    if environments.dry_run:
        anything = False
        if environments.candidates:
            anything = True
            print(
                style.bold(
                    f"Would remove {len(environments.candidates)} unused environment(s) · "
                    f"{format_byte_size(environments.candidate_bytes)}"
                )
            )
            for name in environments.candidates:
                print(f"  {style.dim(name)}")
        else:
            print("No unused environments to remove.")
        if downloads is not None:
            if downloads.candidates:
                anything = True
                print(
                    style.bold(
                        f"Would remove {len(downloads.candidates)} cached download(s) · "
                        f"{format_byte_size(downloads.candidate_bytes)}"
                    )
                )
            else:
                print("No unreferenced cached downloads to remove.")
        print(retained_note)
        if anything:
            print()
            print("This was a preview. Re-run with --execute to delete.")
        return
    reclaimed = environments.reclaimed_bytes + (downloads.reclaimed_bytes if downloads else 0)
    removed = len(environments.removed) + (len(downloads.removed) if downloads else 0)
    if removed:
        parts = [f"{len(environments.removed)} environment(s)"]
        if downloads is not None:
            parts.append(f"{len(downloads.removed)} cached download(s)")
        print(
            style.green(f"Removed {' and '.join(parts)} · reclaimed {format_byte_size(reclaimed)}")
        )
    else:
        print("Nothing needed cleaning.")
    print(retained_note)


def _render_adoption_plan(plan: AdoptionPlan) -> None:
    print(
        f"Found {len(plan.projects)} Lake project(s): "
        f"{plan.ready} ready, {plan.blocked} requiring attention"
    )
    for project in plan.projects:
        state = "attached" if project.attached else "ready" if project.ready else "blocked"
        print(
            f"  {state:8} {project.root} · {len(project.packages)} packages · "
            f"{format_byte_size(project.dependency_bytes)}"
        )
        for blocker in project.blockers:
            print(f"           blocker: {blocker}")
        for warning in project.warnings:
            print(f"           note: {warning}")
    print()
    print(f"Checkout bytes removed:    {format_byte_size(plan.checkout_bytes_removed)}")
    print(f"Shared bytes already ready:{format_byte_size(plan.shared_bytes_reused):>10}")
    print(f"New shared bytes needed:   {format_byte_size(plan.new_shared_bytes)}")
    print(
        f"Estimated machine recovery: {format_byte_size(plan.estimated_machine_reclaimable_bytes)}"
    )


def _render_init_plan(plan: ProjectInitPlan) -> None:
    if plan.action == "adopt":
        state = "already attached" if plan.already_attached else "ready to attach"
        print(f"Existing Lake project · {plan.toolchain} · {state}")
        print(f"Exact dependencies: {len(plan.packages)} package(s); versions unchanged")
        return
    context = f"Mathlib {plan.mathlib_version}" if plan.mathlib_version else "core Lean"
    name = plan.project_name or plan.root.name
    print(f"Create {name} in {plan.root} · {context} · {plan.toolchain}")
    if not plan.toolchain_installed:
        print("Full Lake toolchain: download required (size not published by Elan)")
    if plan.seed_root is not None:
        print(f"Reuse exact local graph: {plan.seed_root}")
        print("Download: 0 B")
    elif plan.download_bytes is not None:
        print(f"Download: {format_byte_size(plan.download_bytes)}")
    else:
        print("Download: unknown (no compatible published artifact could be priced)")
    for blocker in plan.blockers:
        print(f"Blocker: {blocker}")


def _render_update_plan(plan: ProjectUpdatePlan) -> None:
    if not plan.changed:
        print(f"Already current: Mathlib {plan.target_version} · {plan.target_toolchain}")
        return
    print(
        f"Mathlib {plan.current_version} → {plan.target_version} · "
        f"{plan.current_toolchain} → {plan.target_toolchain}"
    )
    if not plan.toolchain_installed:
        print("Full Lake toolchain: download required (size not published by Elan)")
    if plan.seed_root is not None:
        print(f"Reuse exact local graph: {plan.seed_root}")
        print("Download: 0 B")
    elif plan.download_bytes is not None:
        print(f"Download: {format_byte_size(plan.download_bytes)}")
    else:
        print("Download: unknown")
    for blocker in plan.blockers:
        print(f"Blocker: {blocker}")


def _progress(event: RuntimeEvent) -> None:
    package = f" [{event.data['package']}]" if "package" in event.data else ""
    print(f"lean-runtime: {event.kind}{package}: {event.message}", file=sys.stderr)


def _print_operation_failure(exc: ResolutionError | MaterializationError, *, verbose: bool) -> None:
    print(f"lean-runtime: {exc}", file=sys.stderr)
    if not verbose:
        return
    print(f"phase: {exc.phase}", file=sys.stderr)
    if exc.command:
        print("command: " + " ".join(exc.command), file=sys.stderr)
    if exc.exit_code is not None:
        print(f"exit code: {exc.exit_code}", file=sys.stderr)
    if exc.output:
        print(exc.output.rstrip(), file=sys.stderr)


def _cli_source_name(path: Path) -> str:
    if path.is_absolute():
        return path.name
    return path.as_posix()


def _display_result_text(text: str, result: ExecutionResult, display_path: str | None) -> str:
    """Rewrite only the staged entrypoint when the CLI has a logical input name."""
    if display_path is None or not result.command:
        return text
    staged = result.command[-1]
    candidates = {staged}
    staged_path = Path(staged)
    if not staged_path.is_absolute():
        candidates.add(str(Path(result.cwd) / staged_path))
    for candidate in sorted(candidates, key=len, reverse=True):
        text = text.replace(candidate, display_path)
    return text


def _emit_result(
    result: ExecutionResult, as_json: bool, *, display_path: str | None = None
) -> None:
    if as_json:
        _json(serialize_execution_v1(result))
        return
    if result.stdout:
        stdout = _display_result_text(result.stdout, result, display_path)
        print(stdout, end="" if stdout.endswith("\n") else "\n")
    if result.stderr:
        stderr = _display_result_text(result.stderr, result, display_path)
        print(stderr, end="" if stderr.endswith("\n") else "\n", file=sys.stderr)
    status = "accepted" if result.ok else "rejected"
    environment = f" environment={result.environment_id}" if result.environment_id else ""
    print(
        f"{status}:{environment} toolchain={result.toolchain} "
        f"exit={result.exit_code} elapsed={result.elapsed_seconds:.3f}s"
    )


def _policy(arguments: argparse.Namespace) -> ExecutionPolicy:
    return ExecutionPolicy(
        timeout_seconds=arguments.timeout,
        max_output_bytes=arguments.max_output,
        memory_mb=arguments.memory,
        cpu_seconds=arguments.cpu,
        network=arguments.network,
    )


def _add_policy(parser: argparse.ArgumentParser, *, timeout: float = 120) -> None:
    parser.add_argument("--timeout", type=float, default=timeout)
    parser.add_argument("--max-output", type=int, default=1_000_000)
    parser.add_argument("--memory", type=int, help="memory limit in MiB")
    parser.add_argument("--cpu", type=int, help="CPU time limit in seconds")
    parser.add_argument("--network", choices=("inherit", "disabled"), default="inherit")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="lean-runtime")
    root.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {distribution_version('lean-runtime')}",
    )
    root.add_argument("--home", help="runtime store root")
    root.add_argument("--quiet", action="store_true", help="suppress progress events")
    root.add_argument("--verbose", action="store_true", help="show detailed decisions and checks")
    root.add_argument("--timings", action="store_true", help="show stable operation phase timings")
    root.add_argument(
        "--library",
        action="append",
        dest="libraries",
        help="environment library; repeatable (for example ghcr.io/owner/environments)",
    )
    root.add_argument("--availability", choices=("auto", "required", "local"), default=None)
    root.add_argument("--publisher-verification", choices=("ignore", "required"), default="ignore")
    root.add_argument("--trusted-publisher")
    root.add_argument("--trusted-issuer")
    root.add_argument("--verification-tool", default="cosign")
    commands = root.add_subparsers(dest="command", required=True)

    resolve = commands.add_parser("prepare", help="prepare an exact environment description")
    resolve.add_argument("spec", type=Path)
    resolve.add_argument("--output", type=Path)
    resolve.add_argument("--timeout", type=float, default=900)

    ensure = commands.add_parser("open", help="open or build an exact environment")
    ensure.add_argument("lock", type=Path)
    ensure.add_argument("--name")

    pull = commands.add_parser("download", help="download an exact environment from a library")
    pull.add_argument("lock", type=Path)
    pull.add_argument("--name")

    push = commands.add_parser(
        "build-and-publish", help="build an exact environment and publish it to a library"
    )
    push.add_argument("lock", type=Path)
    push.add_argument("--publish-to", required=True)
    push.add_argument("--tag", action="append", default=[])
    push.add_argument("--name")
    push.add_argument(
        "--timeout",
        type=float,
        default=1800,
        help="maximum seconds for each artifact hydration and the environment build",
    )
    push.add_argument(
        "--platform-only",
        action="store_true",
        help="publish blobs and platform manifest without updating the lock index",
    )
    push.add_argument(
        "--accelerate",
        action="store_true",
        help="hydrate artifacts from known package caches (e.g. Mathlib's) for locked "
        "packages without an artifact command; the lock identity is unchanged and the "
        "built environment is still probe-verified before publication",
    )
    push.add_argument("--sign", action="store_true", help="sign the published lock index")
    push.add_argument(
        "--attest", action="store_true", help="publish a signed source/probe attestation"
    )

    publish_index = commands.add_parser(
        "finalize-publication", help="combine computer-specific publication results"
    )
    publish_index.add_argument("lock_id")
    publish_index.add_argument("platform_results", nargs="+", type=Path)
    publish_index.add_argument("--library", required=True)
    publish_index.add_argument("--tag", action="append", default=[])
    publish_index.add_argument("--sign", action="store_true")

    toolchain_publish = commands.add_parser(
        "toolchain-publish", help="publish this platform's verified check-only Lean toolchain"
    )
    toolchain_publish.add_argument("toolchain")
    toolchain_publish.add_argument("--library", required=True)

    toolchain_finalize = commands.add_parser(
        "toolchain-finalize-publication",
        help="combine platform check-toolchain manifests",
    )
    toolchain_finalize.add_argument("toolchain")
    toolchain_finalize.add_argument("platform_results", nargs="+", type=Path)
    toolchain_finalize.add_argument("--library", required=True)
    toolchain_finalize.add_argument("--sign", action="store_true")

    export = commands.add_parser("save-copy", help="save a verified portable environment copy")
    export.add_argument("environment")
    export.add_argument("--output", required=True, type=Path)

    import_bundle = commands.add_parser(
        "open-copy", help="verify and open a portable environment copy"
    )
    import_bundle.add_argument("copy", type=Path)
    import_bundle.add_argument("--name")
    import_bundle.add_argument("--no-probe", action="store_true", help="skip the Lean import probe")

    program_create = commands.add_parser(
        "program-create", help="create a verified ready-to-run program"
    )
    program_create.add_argument("payload", type=Path)
    program_create.add_argument("--command", dest="program_command", nargs="+", required=True)
    program_create.add_argument("--source-revision", required=True)
    program_create.add_argument("--source-environment-id")
    program_create.add_argument("--exact-environment-id")
    program_create.add_argument("--toolchain", default="unknown")
    program_create.add_argument("--capability-id")
    program_create.add_argument(
        "--provenance-file",
        type=Path,
        help="JSON object of content-addressed program provenance",
    )

    program_export = commands.add_parser(
        "program-save-copy", help="save a portable copy of a ready-to-run program"
    )
    program_export.add_argument("program_id")
    program_export.add_argument("--output", required=True, type=Path)

    program_import = commands.add_parser(
        "program-open-copy", help="verify and open a portable program copy"
    )
    program_import.add_argument("copy", type=Path)

    program_pull = commands.add_parser(
        "program-download", help="download a compatible ready-to-run program"
    )
    program_pull.add_argument("library")
    program_pull.add_argument("reference")
    program_pull.add_argument("--source-revision")

    program_push = commands.add_parser(
        "program-publish", help="publish this computer's ready-to-run program"
    )
    program_push.add_argument("program_id")
    program_push.add_argument("--library", required=True)
    program_push.add_argument("--tag", action="append", default=[])
    program_push.add_argument("--sign", action="store_true")

    program_index = commands.add_parser(
        "program-finalize-publication", help="combine computer-specific program publications"
    )
    program_index.add_argument("source_revision")
    program_index.add_argument("computer_results", nargs="+", type=Path)
    program_index.add_argument("--library", required=True)
    program_index.add_argument("--tag", action="append", default=[])
    program_index.add_argument("--sign", action="store_true")

    check = commands.add_parser("check", help="check one Lean file or all local libraries")
    check.add_argument(
        "inputs",
        nargs="*",
        help="FILE, legacy ENVIRONMENT FILE, or omit inside a Lake project; FILE may be -",
    )
    check.add_argument(
        "--with",
        dest="package_refs",
        action="append",
        default=[],
        metavar="REFERENCE",
        help="repeatable github:owner/repository@tag-or-commit package reference",
    )
    check.add_argument("--toolchain", help="override the discovered file or package toolchain")
    check.add_argument(
        "--project", type=Path, help="explicit Lake project for FILE or project-wide check"
    )
    check.add_argument(
        "--include", action="append", default=[], type=Path, help="additional Lean source file"
    )
    check.add_argument("--json", action="store_true")
    _add_policy(check)

    inspect = commands.add_parser("inspect", help="inspect a published environment")
    inspect.add_argument("environment")
    inspect.add_argument("--packages", action="store_true", help="include exact package locks")
    inspect.add_argument("--explain", action="store_true", help="explain identity and reuse")

    commands.add_parser("environments", help="list ready environments")
    storage = commands.add_parser("storage", help="show downloaded and built storage usage")
    storage.add_argument("--json", action="store_true")
    commands.add_parser("doctor", help="check local prerequisites and environment storage")

    verify = commands.add_parser("verify", help="verify a lock or published environment")
    verify.add_argument("subject")
    verify.add_argument("--offline", action="store_true")
    verify.add_argument("--rebuild", action="store_true")
    verify.add_argument("--json", action="store_true")

    diff = commands.add_parser("compare", help="compare two exact Lean environments")
    diff.add_argument("left")
    diff.add_argument("right")
    diff.add_argument("--json", action="store_true")

    toolchain_slim = commands.add_parser(
        "toolchain-slim",
        help="materialize a verified slim check-profile copy of a toolchain",
    )
    toolchain_slim.add_argument("toolchain", help="Lean toolchain, e.g. v4.32.2")
    toolchain_slim.add_argument(
        "--prune-original",
        action="store_true",
        help="uninstall the full Elan toolchain after verification; checking keeps "
        "working through the slim copy, but source builds of new environments and "
        "native compilation need the full toolchain again",
    )

    profile = commands.add_parser("profile", help="measure repeated checks in one environment")
    profile.add_argument("environment", help="environment name or exact lock path")
    profile.add_argument("file", type=Path)
    profile.add_argument("--warmup", type=int, default=1)
    profile.add_argument("--repeat", type=int, default=5)
    profile.add_argument("--json", action="store_true")

    matrix = commands.add_parser("matrix", help="check one file across exact contexts")
    matrix.add_argument("configuration", type=Path)
    matrix.add_argument("file", type=Path)
    matrix.add_argument("--concurrency", type=int, default=1)
    matrix.add_argument("--json", action="store_true")

    replay = commands.add_parser("replay", help="replay a canonical execution capture")
    replay.add_argument("capture", type=Path)
    replay.add_argument("--json", action="store_true")

    gc = commands.add_parser("clean", help="clean up old unused environments")
    gc.add_argument("--execute", action="store_true", help="remove candidates; default is dry-run")
    gc.add_argument("--minimum-age-hours", type=float, default=24 * 30)
    gc.add_argument(
        "--include-downloads", action="store_true", help="also clean unused downloaded files"
    )
    gc.add_argument("--json", action="store_true")

    raw = commands.add_parser(
        "check-file", help="check a file, discovering its local Lake project when possible"
    )
    raw.add_argument("file", type=Path, help="Lean source file, or - for stdin")
    raw.add_argument("--toolchain")
    raw.add_argument("--project", type=Path)
    raw.add_argument("--json", action="store_true")
    _add_policy(raw)

    build = commands.add_parser("build", help="build an existing Lake project")
    build.add_argument("project", type=Path, nargs="?", default=Path("."))
    build.add_argument("targets", nargs="*")
    build.add_argument("--toolchain")
    build.add_argument("--timeout", type=float, default=900)
    build_storage = build.add_mutually_exclusive_group()
    build_storage.add_argument(
        "--shared",
        dest="shared",
        action="store_true",
        default=None,
        help="reuse an exact dependency workspace managed by lean-runtime",
    )
    build_storage.add_argument(
        "--local",
        dest="shared",
        action="store_false",
        help="use checkout-local packages (detach an attached project first)",
    )
    build.add_argument("--json", action="store_true")

    init = commands.add_parser(
        "init", help="create a latest-Mathlib project or adopt an existing Lake project"
    )
    init.add_argument("path", type=Path, nargs="?", default=Path("."))
    init.add_argument("--name", help="explicit Lake package and root module name")
    init_context = init.add_mutually_exclusive_group()
    init_context.add_argument(
        "--mathlib",
        nargs="?",
        const="latest",
        default="latest",
        help="use the newest cataloged Mathlib, or select a version such as 4.33.0",
    )
    init_context.add_argument(
        "--core",
        dest="mathlib",
        action="store_const",
        const=None,
        help="create a core-only project",
    )
    init.add_argument("--toolchain")
    init.add_argument("--seed-from", type=Path, help="reuse an exact local project or project tree")
    init.add_argument("--plan", action="store_true", help="show cost and reuse without changes")
    init.add_argument("--offline", action="store_true", help="require exact local dependencies")
    init.add_argument("--max-download", metavar="SIZE", help="fail above SIZE, e.g. 500MiB")
    init.add_argument(
        "--no-agents",
        dest="agents",
        action="store_false",
        help="do not create the default AGENTS.md project guide",
    )
    init.add_argument("--json", action="store_true")

    scan = commands.add_parser(
        "scan", help="register local Lake projects as reusable exact dependency seeds"
    )
    scan.add_argument("path", type=Path, nargs="?", default=Path("."))
    scan.add_argument("--no-recursive", dest="recursive", action="store_false")
    scan.add_argument("--json", action="store_true")

    update = commands.add_parser("update", help="move this project to the latest stable Mathlib")
    update.add_argument("path", type=Path, nargs="?", default=Path("."))
    update.add_argument("--seed-from", type=Path, help="reuse an exact local project or tree")
    update.add_argument("--plan", action="store_true", help="show the update without changes")
    update.add_argument("--yes", action="store_true", help="apply without an interactive prompt")
    update.add_argument("--offline", action="store_true", help="require exact local dependencies")
    update.add_argument("--max-download", metavar="SIZE", help="fail above SIZE, e.g. 500MiB")
    update.add_argument("--json", action="store_true")

    attach = commands.add_parser(
        "attach", help="plan or adopt shared dependencies for existing Lake projects"
    )
    attach.add_argument("path", type=Path, nargs="?", default=Path("."))
    attach.add_argument("--recursive", action="store_true")
    attach.add_argument("--execute", action="store_true", help="apply the displayed plan")
    attach.add_argument("--json", action="store_true")

    detach = commands.add_parser(
        "detach", help="plan or materialize independent dependencies for one project"
    )
    detach.add_argument("path", type=Path, nargs="?", default=Path("."))
    detach.add_argument("--execute", action="store_true", help="copy dependencies locally")
    detach.add_argument("--json", action="store_true")

    install = commands.add_parser("install", help="install a Lean toolchain")
    install.add_argument("toolchain")

    project = commands.add_parser(
        "project", help="inspect, freeze, or export a pinned GitHub Lean project"
    )
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_inspect = project_commands.add_parser(
        "inspect", help="report whether a project is ready for immutable publication"
    )
    project_inspect.add_argument("path", type=Path, nargs="?", default=Path("."))
    project_inspect.add_argument("--module")
    project_inspect.add_argument("--check-remote", action="store_true")
    project_inspect.add_argument("--json", action="store_true")
    project_lock = project_commands.add_parser(
        "lock", help="freeze a clean, pushed project into an exact environment lock"
    )
    project_lock.add_argument("path", type=Path, nargs="?", default=Path("."))
    project_lock.add_argument("--module")
    project_lock.add_argument("--output", type=Path)
    project_lock.add_argument("--timeout", type=float, default=900)
    project_export = project_commands.add_parser(
        "export", help="build and export this computer's immutable project environment"
    )
    project_export.add_argument("path", type=Path, nargs="?", default=Path("."))
    project_export.add_argument("--module")
    project_export.add_argument("--output", type=Path, required=True)
    project_export.add_argument("--timeout", type=float, default=1800)
    project_export.add_argument("--no-accelerate", action="store_true")
    project_init = project_commands.add_parser(
        "init-publish", help="generate a caller for the multi-platform publication workflow"
    )
    project_init.add_argument("path", type=Path, nargs="?", default=Path("."))
    project_init.add_argument("--module", required=True)
    project_init.add_argument("--library", required=True)
    project_init.add_argument(
        "--output",
        type=Path,
    )
    project_init.add_argument("--force", action="store_true")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    operation_started = time.monotonic()
    display_path: str | None = None
    try:
        selected_availability = (
            "local" if args.command in {"init", "update"} and args.offline else args.availability
        )
        selected_download_limit = (
            parse_byte_size(args.max_download)
            if args.command in {"init", "update"} and args.max_download is not None
            else None
        )
        runtime = Runtime(
            home=args.home,
            on_event=None if args.quiet else _progress,
            availability=selected_availability,
            libraries=args.libraries,
            max_download_bytes=selected_download_limit,
            publisher_verification=args.publisher_verification,
            trusted_publisher=args.trusted_publisher,
            trusted_issuer=args.trusted_issuer,
            verification_tool=args.verification_tool,
        )
        if args.command == "init":
            init_plan = runtime.plan_project_init(
                args.path,
                name=args.name,
                mathlib=args.mathlib,
                toolchain=args.toolchain,
                seed_from=args.seed_from,
            )
            if args.plan:
                if args.json:
                    _json(init_plan.to_dict())
                else:
                    _render_init_plan(init_plan)
                return 0 if init_plan.ready else 1
            if not init_plan.ready:
                raise ProjectError(
                    "project cannot be initialized:\n- " + "\n- ".join(init_plan.blockers)
                )
            if not args.json:
                _render_init_plan(init_plan)
            init_result = runtime.init_project(
                args.path,
                name=args.name,
                mathlib=args.mathlib,
                toolchain=args.toolchain,
                agents=args.agents,
                seed_from=args.seed_from,
            )
            if args.json:
                _json(init_result.to_dict())
            else:
                verb = "Ready" if init_plan.action == "create" else "Attached"
                print(f"{verb}: {init_result.root}")
                print(f"Shared packages: {init_result.packages}")
                if args.agents:
                    print(f"Agent guide: {init_result.root / 'AGENTS.md'}")
                project_name = init_plan.project_name or init_result.root.name
                print(
                    f"Next: cd {init_result.root} && lean-runtime check {project_name}/Basic.lean"
                )
            return 0
        if args.command == "scan":
            scan_result = runtime.scan_projects(args.path, recursive=args.recursive)
            if args.json:
                _json(scan_result.to_dict())
            else:
                print(f"Registered {len(scan_result.projects)} Lake project(s)")
                for project_root in scan_result.projects:
                    print(f"  {project_root}")
            return 0
        if args.command == "update":
            update_plan = runtime.plan_project_update(args.path, seed_from=args.seed_from)
            if not args.json:
                _render_update_plan(update_plan)
            if args.plan or not update_plan.changed or not update_plan.ready:
                if args.json:
                    _json({**update_plan.to_dict(), "applied": False})
                return 0 if update_plan.ready else 1
            apply_update = args.yes
            if not apply_update and sys.stdin.isatty() and not args.json:
                answer = input("Apply this update? [Y/n] ").strip().lower()
                apply_update = answer in {"", "y", "yes"}
            if not apply_update:
                if args.json:
                    _json({**update_plan.to_dict(), "applied": False})
                else:
                    print("No changes made. Re-run with --yes to apply noninteractively.")
                return 0
            runtime.update_project(args.path, seed_from=args.seed_from)
            if args.json:
                _json({**update_plan.to_dict(), "applied": True})
            else:
                print(f"Updated and attached: {update_plan.root}")
            return 0
        if args.command == "attach":
            adoption_plan = runtime.plan_project_adoption(args.path, recursive=args.recursive)
            if not args.execute:
                if args.json:
                    _json(adoption_plan.to_dict())
                else:
                    _render_adoption_plan(adoption_plan)
                    print("No changes made. Re-run with --execute to adopt shared packages.")
                return 0 if adoption_plan.blocked == 0 else 1
            adoption_result = runtime.attach_projects(
                args.path,
                recursive=args.recursive,
                plan=adoption_plan,
            )
            if args.json:
                _json(adoption_result.to_dict())
            else:
                _render_adoption_plan(adoption_result.plan)
                for attached in adoption_result.results:
                    print(
                        f"{attached.action}: {attached.root} · "
                        f"{format_byte_size(attached.reclaimed_bytes)} replaced"
                    )
                for root, message in adoption_result.failures:
                    print(f"failed: {root}: {message}", file=sys.stderr)
            return 0 if adoption_result.ok else 1
        if args.command == "detach":
            detachment_plan = runtime.plan_project_detachment(args.path)
            if not args.execute:
                if args.json:
                    _json(detachment_plan.to_dict())
                else:
                    print(
                        f"Would materialize {len(detachment_plan.packages)} independent "
                        f"package copy/copies for {detachment_plan.root}"
                    )
                    print(
                        f"Maximum additional space: "
                        f"{format_byte_size(detachment_plan.materialize_bytes)} · "
                        f"{format_byte_size(detachment_plan.bytes_free)} free"
                    )
                    for blocker in detachment_plan.blockers:
                        print(f"  blocker: {blocker}")
                    print("No changes made. Re-run with --execute to detach.")
                return 0 if detachment_plan.ready else 1
            detach_result = runtime.detach_project(args.path)
            if args.json:
                _json(detach_result.to_dict())
            else:
                print(
                    f"Detached {detach_result.root}; "
                    f"materialized {detach_result.packages} package(s)"
                )
            return 0
        if args.command == "project":
            if args.project_command == "inspect":
                plan = runtime.inspect_project_publication(
                    args.path, module=args.module, check_remote=args.check_remote
                )
                if args.json:
                    _json(plan.to_dict())
                else:
                    print(f"Project: {plan.package}")
                    print(f"Root: {plan.root}")
                    print(f"Toolchain: {plan.toolchain}")
                    print(f"Repository: {plan.repository or 'unavailable'}")
                    print(f"Revision: {plan.revision or 'unavailable'}")
                    print(f"Import roots: {', '.join(plan.modules)}")
                    print(f"Selected: {plan.selected_module or 'none'}")
                    print(f"Ready to publish: {'yes' if plan.ready else 'no'}")
                    for blocker in plan.blockers:
                        print(f"  - {blocker}")
                return 0 if plan.ready else 1
            if args.project_command == "lock":
                lock = runtime.prepare_project(args.path, module=args.module, timeout=args.timeout)
                output = args.output or discover_project(args.path).root / "environment.lock.json"
                lock.write(output)
                _json(
                    {
                        "lock_id": lock.lock_id,
                        "toolchain": lock.toolchain,
                        "output": str(output),
                    }
                )
                return 0
            if args.project_command == "export":
                info = runtime.export_project(
                    args.path,
                    args.output,
                    module=args.module,
                    timeout=args.timeout,
                    accelerate=not args.no_accelerate,
                )
                _json(info.to_dict())
                return 0
            plan = runtime.inspect_project_publication(args.path, module=args.module)
            invalid = [
                blocker for blocker in plan.blockers if not blocker.startswith("checkout is dirty")
            ]
            if invalid:
                raise ValueError("cannot generate publication workflow:\n- " + "\n- ".join(invalid))
            output = args.output or plan.root / ".github/workflows/publish-lean-environment.yml"
            if output.exists() and not args.force:
                raise ValueError(f"workflow already exists: {output}; pass --force to replace")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                project_publication_workflow(library=args.library, module=args.module),
                encoding="utf-8",
            )
            print(f"Created {output}")
            return 0
        if args.command == "install":
            print(runtime.toolchains.ensure_full(args.toolchain))
            return 0
        if args.command == "prepare":
            lock = runtime.prepare(EnvironmentSpec.load(args.spec), timeout=args.timeout)
            if args.output:
                lock.write(args.output)
                print(lock.lock_id)
            else:
                _json(lock.to_dict())
            if args.timings:
                print(
                    render_timings(
                        (
                            PhaseTiming(
                                "resolution", round((time.monotonic() - operation_started) * 1000)
                            ),
                        )
                    ),
                    file=sys.stderr,
                )
            return 0
        if args.command == "open":
            environment = runtime.open_exact(EnvironmentLock.load(args.lock), name=args.name)
            _json(environment.inspect().to_dict())
            if args.timings:
                print(
                    render_timings(
                        (
                            PhaseTiming(
                                "environment_open",
                                round((time.monotonic() - operation_started) * 1000),
                            ),
                        )
                    ),
                    file=sys.stderr,
                )
            return 0
        if args.command == "download":
            runtime.availability = "required"
            environment = runtime.open_exact(
                EnvironmentLock.load(args.lock),
                name=args.name,
                import_roots=(ROOT_MODULE,),
            )
            _json(environment.inspect().to_dict())
            return 0
        if args.command == "build-and-publish":
            environment = runtime.open_exact(
                EnvironmentLock.load(args.lock),
                name=args.name,
                build_timeout=args.timeout,
                accelerate=args.accelerate,
            )
            _json(
                runtime.publish_environment(
                    environment.id,
                    args.publish_to,
                    tags=args.tag,
                    finalize=not args.platform_only,
                    sign=args.sign,
                    attest=args.attest,
                ).to_dict()
            )
            return 0
        if args.command == "finalize-publication":
            descriptors = []
            for path in args.platform_results:
                value = json.loads(path.read_text(encoding="utf-8"))
                descriptor = value.get("computer_record") if isinstance(value, dict) else None
                if not isinstance(descriptor, dict):
                    raise ValueError(f"invalid platform result: {path}")
                descriptors.append(descriptor)
            if args.sign:
                digest = runtime.finalize_publication(
                    args.library, args.lock_id, descriptors, tags=args.tag, sign=True
                )
            else:
                digest = runtime.finalize_publication(
                    args.library, args.lock_id, descriptors, tags=args.tag
                )
            _json({"exact_environment_id": args.lock_id, "publication_id": digest})
            return 0
        if args.command == "toolchain-publish":
            _json(runtime.publish_toolchain(args.toolchain, args.library).to_dict())
            return 0
        if args.command == "toolchain-finalize-publication":
            descriptors = []
            for path in args.platform_results:
                value = json.loads(path.read_text(encoding="utf-8"))
                descriptor = value.get("descriptor") if isinstance(value, dict) else None
                if not isinstance(descriptor, dict):
                    raise ValueError(f"invalid toolchain platform result: {path}")
                descriptors.append(descriptor)
            digest = runtime.finalize_toolchain_publication(
                args.toolchain, args.library, descriptors, sign=args.sign
            )
            _json({"toolchain": args.toolchain, "publication_id": digest})
            return 0
        if args.command == "save-copy":
            _json(runtime.save_portable_copy(args.environment, args.output).to_dict())
            return 0
        if args.command == "open-copy":
            environment = runtime.open_portable_copy(
                args.copy, name=args.name, probe=not args.no_probe
            )
            _json(environment.inspect().to_dict())
            return 0
        if args.command == "program-create":
            provenance = None
            if args.provenance_file is not None:
                value = json.loads(args.provenance_file.read_text(encoding="utf-8"))
                if not isinstance(value, dict) or not all(
                    isinstance(key, str) and isinstance(item, str) for key, item in value.items()
                ):
                    raise ValueError("program provenance file must contain a JSON string object")
                provenance = value
            program = runtime.create_program(
                args.payload,
                command=args.program_command,
                source_revision=args.source_revision,
                source_environment_id=args.source_environment_id,
                exact_environment_id=args.exact_environment_id,
                toolchain=args.toolchain,
                capability_id=args.capability_id,
                provenance=provenance,
            )
            _json(
                {
                    "program_id": program.id,
                    "description": program.description.to_dict(),
                    "location": str(program.root),
                }
            )
            return 0
        if args.command == "program-save-copy":
            _json(runtime.save_program_copy(args.program_id, args.output).to_dict())
            return 0
        if args.command == "program-open-copy":
            program = runtime.open_program_copy(args.copy)
            _json({"program_id": program.id, "description": program.description.to_dict()})
            return 0
        if args.command == "program-download":
            program = runtime.download_program(
                args.library,
                args.reference,
                expected_source_revision=args.source_revision,
            )
            _json({"program_id": program.id, "description": program.description.to_dict()})
            return 0
        if args.command == "program-publish":
            _json(
                runtime.publish_program(
                    args.program_id,
                    args.library,
                    tags=args.tag,
                    sign=args.sign,
                ).to_dict()
            )
            return 0
        if args.command == "program-finalize-publication":
            descriptors = []
            for path in args.computer_results:
                value = json.loads(path.read_text(encoding="utf-8"))
                descriptor = value.get("computer_record") if isinstance(value, dict) else None
                if not isinstance(descriptor, dict):
                    raise ValueError(f"invalid program computer result: {path}")
                descriptors.append(descriptor)
            digest = runtime.finalize_program_publication(
                args.library,
                args.source_revision,
                descriptors,
                tags=args.tag,
                sign=args.sign,
            )
            _json({"source_revision": args.source_revision, "publication_id": digest})
            return 0
        if args.command == "inspect":
            subject_path = Path(args.environment).expanduser()
            if args.explain and subject_path.is_file():
                lock = EnvironmentLock.load(subject_path)
                payload: dict[str, Any] = {
                    "subject": str(subject_path),
                    "subject_kind": "lock",
                    "lock_id": lock.lock_id,
                    "environment": None,
                    "package_locks": [],
                    "decisions": [item.to_dict() for item in runtime.explain(args.environment)],
                }
            else:
                environment = runtime.environment(args.environment)
                payload = {
                    "subject": args.environment,
                    "subject_kind": "environment",
                    "lock_id": environment.lock.lock_id,
                    "environment": environment.inspect().to_dict(),
                    "package_locks": [],
                    "decisions": [],
                }
                if args.packages:
                    payload["package_locks"] = [
                        package.to_dict() for package in environment.lock.packages
                    ]
                if args.explain:
                    payload["decisions"] = [
                        item.to_dict() for item in runtime.explain(args.environment)
                    ]
            _json(envelope("lean-runtime.inspect/v1", ok=True, data=payload))
            return 0
        if args.command == "environments":
            _json(list(runtime.list_environments()))
            return 0
        if args.command == "storage":
            status = runtime.store_status()
            if args.json:
                _json(status.to_dict())
            else:
                _render_storage(status)
            return 0
        if args.command == "toolchain-slim":
            manifest = runtime.toolchains.materialize_slim(args.toolchain)
            if args.prune_original:
                runtime.toolchains.prune_original(args.toolchain)
            _json(
                {
                    **manifest.to_dict(),
                    "path": str(runtime.toolchains.slim_path(args.toolchain)),
                    "pruned_original": args.prune_original,
                }
            )
            return 0
        if args.command == "doctor":
            doctor_report = runtime.doctor()
            _json(doctor_report.to_dict())
            return 0 if doctor_report.ok else 2
        if args.command == "verify":
            report = runtime.verify(args.subject, offline=args.offline, rebuild=args.rebuild)
            if args.json:
                _json(serialize_verify_v1(report))
            elif report.ok:
                print(f"✓ {args.subject} verified")
                if args.verbose:
                    for check in report.checks:
                        marker = "-" if check.skipped else "✓" if check.ok else "!"
                        print(f"{marker} {check.code.replace('_', ' ')}")
            else:
                failure = report.failures[0]
                print(f"✗ {args.subject} failed verification", file=sys.stderr)
                print(
                    str((failure.details or {}).get("message", failure.code)),
                    file=sys.stderr,
                )
            return 0 if report.ok else 1
        if args.command == "compare":
            difference = runtime.compare(args.left, args.right)
            if args.json:
                _json(serialize_comparison_v1(difference))
            elif difference.equal:
                print("Contexts are identical.")
            else:
                print(difference.summary)
                for item in difference.changes:
                    print(f"{item.path}\n  {item.before} -> {item.after}")
            return 0
        if args.command == "profile":
            profile_report = runtime.profile(
                args.environment, args.file, warmup=args.warmup, repeat=args.repeat
            )
            if args.json:
                _json(serialize_profile_v1(profile_report))
            else:
                stats = profile_report.statistics()
                print(f"Profile: {args.file.name}")
                print(f"Samples: {len(profile_report.results)}")
                for name in ("min", "median", "mean", "p95", "max"):
                    value = stats[name]
                    if value is not None:
                        print(f"  {name:<6} {value:g} ms")
            return 0 if profile_report.ok else 1
        if args.command == "matrix":
            contexts = load_matrix(args.configuration)
            matrix_result = runtime.check_matrix(
                args.file.read_text(encoding="utf-8"),
                contexts=contexts,
                filename=args.file.name,
                base=args.configuration.parent,
                concurrency=args.concurrency,
            )
            if args.json:
                _json(serialize_matrix_v1(matrix_result))
            else:
                print("Context\tResult\tTime\tEnvironment")
                for entry in matrix_result.entries:
                    result = entry.result
                    print(
                        f"{entry.context}\t{'accepted' if result.ok else 'rejected'}\t"
                        f"{result.elapsed_seconds:.2f}s\t{result.environment_id or '-'}"
                    )
            return 0 if matrix_result.ok else 1
        if args.command == "replay":
            capture = ExecutionCapture.load(args.capture)
            result = runtime.replay_capture(capture)
            _emit_result(result, args.json)
            if capture.expected_ok is not None and result.ok != capture.expected_ok:
                return 1
            return 0 if result.ok else 1
        if args.command == "clean":
            gc_report = runtime.clean(
                dry_run=not args.execute,
                minimum_age_seconds=args.minimum_age_hours * 3600,
            )
            gc_downloads = (
                runtime.clean_downloads(
                    dry_run=not args.execute,
                    minimum_age_seconds=args.minimum_age_hours * 3600,
                )
                if args.include_downloads
                else None
            )
            if args.json:
                gc_payload: dict[str, Any] = {
                    "environments": gc_report.to_dict(),
                    "downloaded_files": gc_downloads.to_dict() if gc_downloads else None,
                }
                _json(
                    envelope(
                        "lean-runtime.cleanup/v1",
                        ok=True,
                        data=gc_payload,
                    )
                )
            else:
                _render_cleanup(gc_report, gc_downloads)
            return 0
        if args.command == "check":
            if args.project is not None and args.package_refs:
                raise ValueError("check cannot combine --project with --with")
            if not args.inputs:
                if args.package_refs or args.include:
                    raise ValueError("project-wide check does not accept --with or --include")
                result = runtime.check_project(
                    args.project or Path("."), toolchain=args.toolchain, policy=_policy(args)
                )
                source_file = None
            elif args.package_refs:
                if len(args.inputs) != 1:
                    raise ValueError("check with --with expects exactly one FILE")
                environment = runtime.open_references(args.package_refs, toolchain=args.toolchain)
                source_file = Path(args.inputs[0])
            elif len(args.inputs) == 1:
                if args.include:
                    raise ValueError("local project checks do not accept --include")
                source_file = Path(args.inputs[0])
                if str(source_file) == "-":
                    display_path = "<stdin>"
                    result = runtime.check(
                        sys.stdin.read(),
                        toolchain=args.toolchain,
                        project=args.project,
                        policy=_policy(args),
                    )
                else:
                    result = runtime.check_file(
                        source_file,
                        toolchain=args.toolchain,
                        project=args.project,
                        policy=_policy(args),
                    )
                source_file = None
            else:
                if len(args.inputs) != 2:
                    raise ValueError("check expects FILE, ENVIRONMENT FILE, or FILE with --with")
                if args.toolchain:
                    raise ValueError("check --toolchain is only valid with --with")
                if args.project:
                    raise ValueError("check --project is only valid with a single FILE")
                environment = runtime.environment(args.inputs[0])
                source_file = Path(args.inputs[1])
            if source_file is None:
                pass
            elif str(source_file) == "-":
                if args.include:
                    raise ValueError("stdin entrypoints cannot be combined with --include")
                result = environment.check(sys.stdin.read(), policy=_policy(args))
            else:
                source_paths = [source_file, *args.include]
                files = {_cli_source_name(path): path.read_text() for path in source_paths}
                result = environment.check_files(
                    files,
                    entrypoint=_cli_source_name(source_file),
                    policy=_policy(args),
                )
        elif args.command == "check-file":
            if str(args.file) == "-":
                display_path = "<stdin>"
                result = runtime.check(
                    sys.stdin.read(),
                    toolchain=args.toolchain,
                    project=args.project,
                    policy=_policy(args),
                )
            else:
                result = runtime.check_file(
                    args.file,
                    toolchain=args.toolchain,
                    project=args.project,
                    policy=_policy(args),
                )
        else:
            result = runtime.build(
                args.project,
                targets=args.targets,
                toolchain=args.toolchain,
                timeout=args.timeout,
                shared=args.shared,
            )
    except KeyboardInterrupt:
        print("lean-runtime: interrupted", file=sys.stderr)
        return 130
    except (ResolutionError, MaterializationError) as exc:
        details = {
            "phase": exc.phase,
            "command": list(exc.command),
            "exit_code": exc.exit_code,
            "output": exc.output,
        }
        if getattr(args, "json", False):
            _json(
                envelope(
                    _schema_for(args.command),
                    ok=False,
                    data={},
                    errors=[error("operation_failed", str(exc), details=details)],
                )
            )
        else:
            _print_operation_failure(exc, verbose=args.verbose)
        return 2
    except (LeanRuntimeError, OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        if getattr(args, "json", False):
            _json(
                envelope(
                    _schema_for(args.command),
                    ok=False,
                    data={},
                    errors=[error("invocation_failed", str(exc))],
                )
            )
        else:
            print(f"lean-runtime: {exc}", file=sys.stderr)
        return 2
    _emit_result(result, args.json, display_path=display_path)
    if args.timings:
        print(render_timings(result.timings), file=sys.stderr)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
