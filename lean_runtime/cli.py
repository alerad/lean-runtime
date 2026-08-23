"""Minimal command-line interface around the environment compiler."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised by Python 3.10 CI
    import tomli as tomllib

from .console import ConsoleRenderer, styler_for
from .declaration_index import DeclarationIndex, DeclarationIndexSet, DeclarationShard
from .declaration_index_build import (
    build_declaration_index,
    load_declaration_index_build,
)
from .declaration_index_oci import OCIDeclarationIndexPublisher
from .discovery.api import Discovery
from .discovery.catalog_build import build_catalog_file
from .discovery.defaults import default_catalog
from .discovery.errors import DiscoveryError
from .environments import ExecutionCapture
from .errors import (
    LeanRuntimeError,
    MaterializationError,
    ProjectError,
    ProjectNotFoundError,
    PublicationError,
    ResolutionError,
)
from .events import RuntimeEvent
from .header_cache import ENABLE_VARIABLE as _HEADER_SNAPSHOTS_VARIABLE
from .health import DoctorReport
from .lake import ROOT_MODULE
from .lockfiles import EnvironmentLock
from .matrix import load_matrix
from .models import ExecutionResult, PhaseTiming
from .oci import OCIRepository
from .policies import ExecutionPolicy, format_byte_size, parse_byte_size
from .profiling import ProfileReport
from .project_sharing import AdoptionPlan, ProjectInitPlan, ProjectUpdatePlan
from .projects import discover_project, project_publication_workflow
from .publisher_verification import CosignVerifier
from .run_cli import run as _run_front_door
from .runtime import Runtime
from .specs import EnvironmentSpec
from .store import CleanupReport, DownloadCleanupReport, StoreStatus
from .timings import render_timings
from .wire import (
    envelope,
    error,
    serialize_check_batch_v1,
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
        "publish-environment": "lean-runtime.publication/v1",
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
        (
            "Declaration indexes",
            status.declaration_indexes,
            status.declaration_indexes_bytes,
        ),
        ("Project packages", status.project_packages, status.project_packages_bytes),
        ("Toolchains", None, status.toolchains_bytes),
        ("Executions", status.executions, status.executions_bytes),
        ("Scratch", status.scratch_workspaces, status.scratch_bytes),
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
    print(style.dim("Reclaim space: lean-runtime clean --dry-run  (keeps recent/named)"))
    print(style.dim("               lean-runtime clean --all"))
    if status.project_packages:
        print(
            style.dim("Shared project packages are retained for reuse; cleanup is not automatic.")
        )


def _render_environments(records: tuple[dict[str, object], ...]) -> None:
    if not records:
        print("No ready environments.")
        return
    for record in records:
        names = record.get("names")
        label = (
            ", ".join(str(item) for item in names)
            if isinstance(names, list) and names
            else str(record["environment_id"])
        )
        print(f"{label}  {record.get('toolchain', 'unknown')}  {record.get('status', 'unknown')}")


def _render_doctor(report: DoctorReport) -> None:
    symbols = {"pass": "✓", "warning": "!", "fail": "✗"}
    for check in report.checks:
        print(f"{symbols[check.status]} {check.name}: {check.message}")


def _render_cleanup(
    environments: CleanupReport,
    downloads: DownloadCleanupReport | None,
    scratch: CleanupReport | None = None,
) -> None:
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
        if scratch is not None:
            if scratch.candidates:
                anything = True
                print(
                    style.bold(
                        f"Would remove {len(scratch.candidates)} abandoned workspace(s) · "
                        f"{format_byte_size(scratch.candidate_bytes)}"
                    )
                )
            else:
                print("No abandoned workspaces to remove.")
        print(retained_note)
        if anything:
            print()
            print("This was a preview.")
        return
    reclaimed = environments.reclaimed_bytes + (downloads.reclaimed_bytes if downloads else 0)
    if scratch is not None:
        reclaimed += scratch.reclaimed_bytes
    removed = (
        len(environments.removed)
        + (len(downloads.removed) if downloads else 0)
        + (len(scratch.removed) if scratch else 0)
    )
    if removed:
        parts = [f"{len(environments.removed)} environment(s)"]
        if downloads is not None:
            parts.append(f"{len(downloads.removed)} cached download(s)")
        if scratch is not None:
            parts.append(f"{len(scratch.removed)} abandoned workspace(s)")
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
        if not plan.packages:
            print(
                "Already current: no cataloged Mathlib dependency to update · "
                f"{plan.target_toolchain}"
            )
            return
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
    counters: list[str] = []
    frame_current = event.data.get("frame_current")
    frame_total = event.data.get("frame_total")
    if isinstance(frame_current, int) and isinstance(frame_total, int):
        counters.append(f"frames {frame_current}/{frame_total}")
    if event.current_bytes is not None and event.total_bytes is not None:
        counters.append(
            f"{format_byte_size(event.current_bytes)}/{format_byte_size(event.total_bytes)}"
        )
    progress = f" · {', '.join(counters)}" if counters else ""
    print(f"lean-runtime: {event.kind}{package}: {event.message}{progress}", file=sys.stderr)


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


def _print_publication_failure(exc: PublicationError) -> None:
    status = f" (HTTP {exc.status_code})" if exc.status_code is not None else ""
    print(
        f"✗ Publication failed at {exc.registry} during {exc.phase}{status}",
        file=sys.stderr,
    )
    print(f"  {exc}", file=sys.stderr)
    if exc.attempted_provider is not None:
        print("  Authentication: unavailable (source: none)", file=sys.stderr)
        print(f"  Attempted provider: {exc.attempted_provider}", file=sys.stderr)
    else:
        identity = exc.username or "anonymous"
        print(
            f"  Authentication: {identity} (source: {exc.credential_source})",
            file=sys.stderr,
        )
    if exc.partial:
        print(
            "  No verified final release was produced; immutable platform content may exist.",
            file=sys.stderr,
        )
        print("  Safe to retry the publication; verified content will be reused.", file=sys.stderr)
    else:
        print("  Nothing was published: no manifest or index was finalized.", file=sys.stderr)
        print("  Safe to retry; completed blobs will be reused.", file=sys.stderr)
    if exc.hint:
        print(f"  Fix: {exc.hint}", file=sys.stderr)


def _cli_source_name(path: Path) -> str:
    if path.is_absolute():
        return path.name
    return path.as_posix()


def _expand_check_inputs(inputs: list[str]) -> list[Path]:
    """Expand FILE and DIRECTORY inputs into an ordered, deduplicated file list."""
    files: list[Path] = []
    for raw in inputs:
        path = Path(raw)
        if path.is_dir():
            found: list[Path] = []
            for current, directories, filenames in os.walk(path):
                directories[:] = sorted(name for name in directories if not name.startswith("."))
                found.extend(
                    Path(current) / name for name in sorted(filenames) if name.endswith(".lean")
                )
            if not found:
                raise ValueError(f"no Lean files found under directory: {path}")
            files.extend(found)
        elif path.is_file():
            files.append(path)
        else:
            raise ValueError(f"check input does not exist: {path}")
    return list(dict.fromkeys(files))


def _run_check_batch(runtime: Runtime, files: list[Path], args: argparse.Namespace) -> int:
    """Check each file independently, optionally in parallel, and report per file."""
    if args.concurrency < 1 or args.concurrency > 32:
        raise ValueError("check concurrency must be between 1 and 32")
    environment = runtime.environment(args.environment) if args.environment else None

    def execute(path: Path) -> ExecutionResult:
        if environment is not None:
            name = _cli_source_name(path)
            return environment.check_files(
                {name: path.read_text(encoding="utf-8")}, entrypoint=name, policy=_policy(args)
            )
        return runtime.check_file(
            path, toolchain=args.toolchain, project=args.project, policy=_policy(args)
        )

    started = time.monotonic()
    results: dict[Path, ExecutionResult] = {}
    if args.concurrency == 1:
        for path in files:
            results[path] = execute(path)
    else:
        with ThreadPoolExecutor(
            max_workers=args.concurrency, thread_name_prefix="lean-check"
        ) as pool:
            futures = {pool.submit(execute, path): path for path in files}
            try:
                for future in as_completed(futures):
                    results[futures[future]] = future.result()
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
    entries = [(str(path), results[path]) for path in files]
    runtime.events.emit(
        "check.completed",
        "Lean check completed",
        ok=all(result.ok for _, result in entries),
    )
    if args.json:
        _json(serialize_check_batch_v1(entries, time.monotonic() - started))
    else:
        for display, result in entries:
            status = "accepted" if result.ok else "timed out" if result.timed_out else "rejected"
            print(f"{display}\t{status}\t{result.elapsed_seconds:.2f}s")
            if not result.ok:
                output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
                if output:
                    print(_display_result_text(output, result, display), file=sys.stderr)
        accepted = sum(1 for _, result in entries if result.ok)
        print(f"{accepted}/{len(entries)} accepted")
    if all(result.ok for _, result in entries):
        return 0
    return 2 if any(result.timed_out for _, result in entries) else 1


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
    for hint in result.hints:
        print(f"hint: {hint}", file=sys.stderr)
    status = "accepted" if result.ok else "timed out" if result.timed_out else "rejected"
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


def _mathlib_version(value: str) -> str:
    if value == "latest" or re.fullmatch(r"\d+\.\d+\.\d+", value):
        return value
    raise argparse.ArgumentTypeError("expected a version such as 4.33.0")


# The complete v4 public vocabulary used by shell completion.
PUBLIC_COMMANDS = (
    "new",
    "adopt",
    "check",
    "watch",
    "build",
    "update",
    "publish",
    "status",
    "verify",
    "doctor",
    "clean",
    "env",
    "project",
    "program",
    "toolchain",
    "storage",
    "catalog",
    "replay",
    "completion",
)


def _completion_script(shell: str) -> str:
    command_names = sorted(PUBLIC_COMMANDS)
    words = " ".join(command_names)
    if shell == "bash":
        return f"complete -W '{words}' lean-runtime\n"
    if shell == "zsh":
        return f"#compdef lean-runtime\n_arguments '1:command:({words})' '*::arg:->args'\n"
    return (
        "\n".join(
            f"complete -c lean-runtime -n '__fish_use_subcommand' -a {name}"
            for name in command_names
        )
        + "\n"
    )


def _add_policy(
    parser: argparse.ArgumentParser, *, timeout: float = 120, hidden: bool = False
) -> None:
    option_help = argparse.SUPPRESS if hidden else None
    parser.add_argument("--timeout", type=float, default=timeout, help=option_help)
    parser.add_argument("--max-output", type=int, default=1_000_000, help=option_help)
    parser.add_argument("--memory", type=int, help=option_help or "memory limit in MiB")
    parser.add_argument("--cpu", type=int, help=option_help or "CPU time limit in seconds")
    parser.add_argument(
        "--network", choices=("inherit", "disabled"), default="inherit", help=option_help
    )


def _add_common_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--home", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument(
        "--library",
        action="append",
        dest="libraries",
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--availability",
        choices=("auto", "required", "local"),
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--timings", action="store_true", default=argparse.SUPPRESS)


def _add_check_v4(parser: argparse.ArgumentParser, *, watch: bool = False) -> None:
    parser.add_argument("inputs", nargs="*", metavar="PATH")
    parser.add_argument(
        "--using",
        metavar="CONTEXT",
        help="override context inference with a project, lock, environment, toolchain, or package",
    )
    parser.add_argument("--include", action="append", default=[], type=Path)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--plan", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--lock-out", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--no-source-build", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--allow-source-build",
        action="store_true",
        help="allow standalone discovery to build an environment from source",
    )
    parser.add_argument("--max-download", type=parse_byte_size, help=argparse.SUPPRESS)
    _add_common_output(parser)
    parser.add_argument("--repeat", type=int)
    parser.add_argument("--warmup", type=int, default=1, help=argparse.SUPPRESS)
    parser.add_argument("--matrix", nargs="?", const=Path("lean-runtime.matrix.toml"), type=Path)
    parser.add_argument(
        "--concurrency",
        type=int,
        default=max(1, os.cpu_count() or 1),
        help=argparse.SUPPRESS,
    )
    _add_policy(parser, hidden=True)
    parser.set_defaults(
        command="check",
        watch=watch,
        watch_interval=0.2,
        across=None,
        package_refs=[],
        environment=None,
        project=None,
        toolchain=None,
    )


def _configuration_defaults() -> dict[str, Any]:
    """Load global then nearest-project v4 configuration when present."""
    configured = os.environ.get("LEAN_RUNTIME_CONFIG")
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    paths = [Path(configured).expanduser()] if configured else [xdg / "lean-runtime/config.toml"]
    current = Path.cwd().resolve()
    project_config = next(
        (
            root / "lean-runtime.toml"
            for root in (current, *current.parents)
            if (root / "lean-runtime.toml").is_file()
        ),
        None,
    )
    if project_config is not None:
        paths.append(project_config)
    defaults: dict[str, Any] = {}
    for path in paths:
        if not path.is_file():
            continue
        try:
            with path.open("rb") as stream:
                document = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(f"could not read Lean Runtime configuration {path}: {exc}") from exc
        runtime = document.get("runtime", {})
        trust = document.get("trust", {})
        if not isinstance(runtime, dict) or not isinstance(trust, dict):
            raise ValueError(f"invalid Lean Runtime configuration sections in {path}")
        if "home" in runtime:
            if not isinstance(runtime["home"], str):
                raise ValueError(f"runtime.home must be a string in {path}")
            defaults["home"] = runtime["home"]
        if "libraries" in runtime:
            libraries = runtime["libraries"]
            if not isinstance(libraries, list) or not all(
                isinstance(item, str) for item in libraries
            ):
                raise ValueError(f"runtime.libraries must be a string array in {path}")
            defaults["libraries"] = libraries
        if "availability" in runtime:
            if runtime["availability"] not in {"auto", "required", "local"}:
                raise ValueError(f"invalid runtime.availability in {path}")
            defaults["availability"] = runtime["availability"]
        for key in (
            "publisher_verification",
            "trusted_publisher",
            "trusted_issuer",
            "verification_tool",
        ):
            if key in trust:
                if not isinstance(trust[key], str):
                    raise ValueError(f"trust.{key} must be a string in {path}")
                defaults[key] = trust[key]
        if defaults.get("publisher_verification") not in {None, "ignore", "required"}:
            raise ValueError(f"invalid trust.publisher_verification in {path}")
    return defaults


def parser() -> argparse.ArgumentParser:
    """Build the intentionally small, cwd-first v4 command surface."""
    configured = _configuration_defaults()
    root = argparse.ArgumentParser(
        prog="lean-runtime",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Lean projects and standalone proofs, without environment busywork.",
        epilog="""Start here:
  new NAME       Create a Lean project
  adopt [PATH]   Share dependencies from existing Lake project(s)
  check [PATH…]  Check a project or Lean source (context is inferred)
  watch FILE     Re-check a file when it changes
  build [TARGET] Build the current project
  update         Update the current project safely
  publish        Configure publication for the current project

Inspect and fix:
  status · verify · doctor · clean

Advanced namespaces:
  env · project · program · toolchain · storage · catalog
""",
    )
    root.add_argument(
        "--version", action="version", version=f"%(prog)s {distribution_version('lean-runtime')}"
    )
    # Store and trust policy are intentionally environment/config shaped. Keep
    # these suppressed overrides for hermetic automation and the test suite.
    root.add_argument("--home", default=configured.get("home"), help=argparse.SUPPRESS)
    root.add_argument("--quiet", action="store_true", help=argparse.SUPPRESS)
    root.add_argument("--verbose", action="store_true", help=argparse.SUPPRESS)
    root.add_argument("--timings", action="store_true", help=argparse.SUPPRESS)
    root.add_argument(
        "--library",
        action="append",
        dest="libraries",
        default=configured.get("libraries"),
        help=argparse.SUPPRESS,
    )
    root.add_argument(
        "--availability",
        choices=("auto", "required", "local"),
        default=configured.get("availability"),
        help=argparse.SUPPRESS,
    )
    root.add_argument(
        "--publisher-verification",
        choices=("ignore", "required"),
        default=configured.get("publisher_verification", "ignore"),
        help=argparse.SUPPRESS,
    )
    root.add_argument(
        "--trusted-publisher",
        default=configured.get("trusted_publisher"),
        help=argparse.SUPPRESS,
    )
    root.add_argument(
        "--trusted-issuer",
        default=configured.get("trusted_issuer"),
        help=argparse.SUPPRESS,
    )
    root.add_argument(
        "--verification-tool",
        default=configured.get("verification_tool", "cosign"),
        help=argparse.SUPPRESS,
    )
    commands = root.add_subparsers(dest="surface_command", required=True, metavar="COMMAND")

    new = commands.add_parser("new", help="create a new Lean project")
    new.add_argument("path", type=Path)
    new.add_argument("--core", action="store_true")
    new.add_argument("--offline", action="store_true")
    new.add_argument("--yes", action="store_true")
    new.add_argument("--no-agents", dest="agents", action="store_false", default=True)
    new.add_argument("--ci", action="store_true")
    _add_common_output(new)
    new.set_defaults(
        command="init",
        name=None,
        mathlib_version="latest",
        toolchain=None,
        seed_from=None,
        plan=False,
        max_download=None,
    )

    adopt = commands.add_parser("adopt", help="adopt existing Lake project(s)")
    adopt.add_argument("path", type=Path, nargs="?", default=Path.cwd())
    adopt.add_argument("--yes", action="store_true")
    adopt.add_argument("--dry-run", action="store_true")
    _add_common_output(adopt)
    adopt.set_defaults(command="attach", recursive=None, execute=False)

    check = commands.add_parser("check", help="check Lean code; context is inferred")
    _add_check_v4(check)
    watch = commands.add_parser("watch", help="re-check one Lean file when it changes")
    _add_check_v4(watch, watch=True)

    build = commands.add_parser("build", help="build the current Lake project")
    build.add_argument("targets", nargs="*")
    build.add_argument(
        "--no-cache",
        dest="artifact_cache",
        action="store_false",
        help="skip dependency artifact restoration and build directly with Lake",
    )
    build.add_argument("--json", action="store_true")
    build.set_defaults(
        command="build",
        project=Path.cwd(),
        toolchain=None,
        timeout=900,
        shared=None,
        artifact_cache=True,
    )

    update = commands.add_parser("update", help="update the current project safely")
    update.add_argument("path", type=Path, nargs="?", default=Path.cwd())
    update.add_argument("--yes", action="store_true")
    update.add_argument("--dry-run", action="store_true")
    update.add_argument("--offline", action="store_true")
    _add_common_output(update)
    update.set_defaults(command="update", seed_from=None, plan=False, max_download=None)

    publish = commands.add_parser("publish", help="configure publication for the current project")
    publish.add_argument("path", type=Path, nargs="?", default=Path.cwd())
    publish.add_argument("--yes", action="store_true")
    publish.add_argument("--json", action="store_true")
    publish.set_defaults(command="publish-project")

    status = commands.add_parser("status", help="explain the current project or context")
    status.add_argument("subject", nargs="?", default=str(Path.cwd()))
    status.add_argument("--json", action="store_true")
    status.set_defaults(command="status")

    verify = commands.add_parser("verify", help="verify a lock, environment, or artifact")
    verify.add_argument("subject", nargs="?", default=str(Path.cwd()))
    verify.add_argument("--offline", action="store_true")
    verify.add_argument("--rebuild", action="store_true")
    _add_common_output(verify)
    verify.set_defaults(command="verify")
    doctor = commands.add_parser("doctor", help="diagnose and offer safe repairs")
    doctor.add_argument("--yes", action="store_true")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(command="doctor", fix=False)
    clean = commands.add_parser("clean", help="preview and reclaim unused storage")
    clean.add_argument("--yes", action="store_true")
    clean.add_argument("--dry-run", action="store_true")
    clean.add_argument("--all", dest="include_downloads", action="store_true")
    clean.add_argument("--minimum-age-hours", type=float, default=24 * 30)
    clean.add_argument(
        "--keep-last", type=int, default=int(os.environ.get("LEAN_RUNTIME_CLEAN_KEEP_LAST", "0"))
    )
    _add_common_output(clean)
    clean.set_defaults(command="clean", execute=False)

    replay = commands.add_parser("replay", help="replay an execution capture")
    replay.add_argument("capture", type=Path)
    _add_common_output(replay)
    replay.set_defaults(command="replay")
    completion = commands.add_parser("completion", help="generate shell completion")
    completion.add_argument("shell", choices=("bash", "zsh", "fish"))
    completion.set_defaults(command="completion")

    env = commands.add_parser("env", help="exact immutable environments")
    env_commands = env.add_subparsers(dest="env_command", required=True)
    env_list = env_commands.add_parser("list", help="list ready environments")
    _add_common_output(env_list)
    env_list.set_defaults(command="environments")
    env_info = env_commands.add_parser("info", help="inspect an environment")
    env_info.add_argument("environment")
    env_info.add_argument("--packages", action="store_true")
    env_info.add_argument("--explain", action="store_true")
    env_info.set_defaults(command="inspect")
    env_lock = env_commands.add_parser("lock", help="resolve a specification to an exact lock")
    env_lock.add_argument("spec", type=Path)
    env_lock.add_argument("--output", type=Path)
    env_lock.add_argument("--timeout", type=float, default=900)
    env_lock.set_defaults(command="prepare")
    env_acquire = env_commands.add_parser("acquire", help="make an exact environment available")
    env_acquire.add_argument("lock", type=Path)
    env_acquire.add_argument("--name")
    env_acquire.add_argument("--download-only", action="store_true")
    env_acquire.add_argument("--timeout", type=float, default=1800)
    env_acquire.set_defaults(command="acquire")
    env_diff = env_commands.add_parser("diff", help="compare exact environments")
    env_diff.add_argument("left")
    env_diff.add_argument("right")
    _add_common_output(env_diff)
    env_diff.set_defaults(command="compare")
    env_export = env_commands.add_parser("export", help="export a portable environment")
    env_export.add_argument("environment")
    env_export.add_argument("--output", required=True, type=Path)
    env_export.set_defaults(command="copy-save")
    env_import = env_commands.add_parser("import", help="import a portable environment")
    env_import.add_argument("copy", type=Path)
    env_import.add_argument("--name")
    env_import.add_argument("--no-probe", action="store_true")
    env_import.set_defaults(command="copy-open")
    env_publish = env_commands.add_parser("publish", help="publish one platform environment")
    env_publish.add_argument("lock", nargs="?", type=Path)
    env_publish.add_argument("--to", dest="publish_to", required=True)
    env_publish.add_argument("--tag", action="append", default=[])
    env_publish.add_argument("--name")
    env_publish.add_argument("--timeout", type=float)
    env_publish.add_argument("--platform-only", action="store_true")
    env_publish.add_argument("--accelerate", action="store_true")
    env_publish.add_argument("--sign", action="store_true")
    env_publish.add_argument("--attest", action="store_true")
    env_publish.add_argument("--check-access", action="store_true")
    _add_common_output(env_publish)
    env_publish.set_defaults(command="publish-environment")
    env_finalize = env_commands.add_parser("finalize", help="finalize a platform matrix")
    env_finalize.add_argument("lock_id")
    env_finalize.add_argument("platform_results", nargs="+", type=Path)
    env_finalize.add_argument("--library", required=True)
    env_finalize.add_argument("--tag", action="append", default=[])
    env_finalize.add_argument("--sign", action="store_true")
    env_finalize.set_defaults(command="finalize-environment")

    project = commands.add_parser("project", help="advanced mutable-project operations")
    pc = project.add_subparsers(dest="project_command", required=True)
    pi = pc.add_parser("info")
    pi.add_argument("path", type=Path, nargs="?", default=Path.cwd())
    pi.add_argument("--module")
    pi.add_argument("--check-remote", action="store_true")
    _add_common_output(pi)
    pi.set_defaults(command="project", project_command="inspect")
    ps = pc.add_parser("scan")
    ps.add_argument("path", type=Path, nargs="?", default=Path.cwd())
    _add_common_output(ps)
    ps.set_defaults(command="scan", recursive=True)
    pshare = pc.add_parser("share")
    pshare.add_argument("path", type=Path, nargs="?", default=Path.cwd())
    pshare.add_argument("--yes", action="store_true")
    pshare.add_argument("--dry-run", action="store_true")
    _add_common_output(pshare)
    pshare.set_defaults(command="attach", recursive=None, execute=False)
    punshare = pc.add_parser("unshare")
    punshare.add_argument("path", type=Path, nargs="?", default=Path.cwd())
    punshare.add_argument("--yes", action="store_true")
    punshare.add_argument("--dry-run", action="store_true")
    _add_common_output(punshare)
    punshare.set_defaults(command="detach", execute=False)
    pl = pc.add_parser("lock")
    pl.add_argument("path", type=Path, nargs="?", default=Path.cwd())
    pl.add_argument("--module")
    pl.add_argument("--output", type=Path)
    pl.add_argument("--timeout", type=float, default=900)
    pl.set_defaults(command="project")
    pe = pc.add_parser("export")
    pe.add_argument("path", type=Path, nargs="?", default=Path.cwd())
    pe.add_argument("--module")
    pe.add_argument("--output", type=Path, required=True)
    pe.add_argument("--timeout", type=float, default=1800)
    pe.add_argument("--no-accelerate", action="store_true")
    pe.set_defaults(command="project")

    toolchain = commands.add_parser("toolchain", help="advanced toolchain management")
    tc = toolchain.add_subparsers(dest="toolchain_operation", required=True)
    tl = tc.add_parser("list")
    _add_common_output(tl)
    tl.set_defaults(command="toolchain-list")
    tinfo = tc.add_parser("info")
    tinfo.add_argument("toolchain")
    _add_common_output(tinfo)
    tinfo.set_defaults(command="toolchain-info")
    ti = tc.add_parser("install")
    ti.add_argument("toolchain")
    ti.set_defaults(command="toolchain-install")
    to = tc.add_parser("optimize")
    to.add_argument("toolchain")
    to.add_argument("--prune-original", action="store_true")
    to.set_defaults(command="toolchain-slim")
    tp = tc.add_parser("publish")
    tp.add_argument("toolchain")
    tp.add_argument("--library", required=True)
    tp.set_defaults(command="publish-toolchain")
    tf = tc.add_parser("finalize")
    tf.add_argument("toolchain")
    tf.add_argument("platform_results", nargs="+", type=Path)
    tf.add_argument("--library", required=True)
    tf.add_argument("--sign", action="store_true")
    tf.set_defaults(command="finalize-toolchain")

    storage = commands.add_parser("storage", help="storage inspection and maintenance")
    sc = storage.add_subparsers(dest="storage_command", required=True)
    su = sc.add_parser("usage")
    _add_common_output(su)
    su.set_defaults(command="storage", verify=False)
    sv = sc.add_parser("verify")
    _add_common_output(sv)
    sv.set_defaults(command="storage", verify=True)

    # Program and low-level publication retain operator-shaped flags, but live
    # under their object namespaces instead of polluting the daily surface.
    program = commands.add_parser("program", help="ready-to-run programs")
    pr = program.add_subparsers(dest="program_operation", required=True)
    pcreate = pr.add_parser("create")
    pcreate.add_argument("payload", type=Path)
    pcreate.add_argument("--command", dest="program_command", nargs="+", required=True)
    pcreate.add_argument("--source-revision", required=True)
    pcreate.add_argument("--source-environment-id")
    pcreate.add_argument("--source-lock-id")
    pcreate.add_argument("--toolchain", default="unknown")
    pcreate.add_argument("--capability-id")
    pcreate.add_argument("--provenance-file", type=Path)
    pcreate.set_defaults(command="program-create")
    pinfo = pr.add_parser("info")
    pinfo.add_argument("program_id")
    pinfo.set_defaults(command="program-info")
    prun = pr.add_parser("run")
    prun.add_argument("program_id")
    prun.add_argument("arguments", nargs="*")
    prun.set_defaults(command="program-run")
    pexport = pr.add_parser("export")
    pexport.add_argument("program_id")
    pexport.add_argument("--output", required=True, type=Path)
    pexport.set_defaults(command="program-save")
    pimport = pr.add_parser("import")
    pimport.add_argument("copy", type=Path)
    pimport.set_defaults(command="program-open")
    pacquire = pr.add_parser("acquire")
    pacquire.add_argument("library")
    pacquire.add_argument("reference")
    pacquire.add_argument("--source-revision")
    pacquire.set_defaults(command="program-download")
    ppub = pr.add_parser("publish")
    ppub.add_argument("program_id")
    ppub.add_argument("--library", required=True)
    ppub.add_argument("--tag", action="append", default=[])
    ppub.add_argument("--sign", action="store_true")
    ppub.set_defaults(command="publish-program")
    pfinal = pr.add_parser("finalize")
    pfinal.add_argument("source_revision")
    pfinal.add_argument("computer_results", nargs="+", type=Path)
    pfinal.add_argument("--library", required=True)
    pfinal.add_argument("--tag", action="append", default=[])
    pfinal.add_argument("--sign", action="store_true")
    pfinal.set_defaults(command="finalize-program")
    declaration_index = commands.add_parser(
        "declaration-index", help="build and publish composable declaration shards"
    )
    di = declaration_index.add_subparsers(dest="declaration_index_operation", required=True)
    dib = di.add_parser("build")
    dib.add_argument("lock", type=Path)
    dib.add_argument("--output", required=True, type=Path)
    dib.add_argument("--weights", type=Path)
    dib.set_defaults(command="declaration-index-build")
    dip = di.add_parser("publish")
    dip.add_argument("lock", type=Path)
    dip.add_argument("build", type=Path)
    dip.add_argument("--library", required=True)
    dip.add_argument("--sign", action="store_true")
    dip.set_defaults(command="declaration-index-publish")
    dii = di.add_parser("inspect")
    dii.add_argument("build", type=Path)
    dii.add_argument("--resolve")
    dii.set_defaults(command="declaration-index-inspect")
    catalog = commands.add_parser("catalog", help="catalog maintenance")
    cc = catalog.add_subparsers(dest="catalog_operation", required=True)
    cb = cc.add_parser("build")
    cb.add_argument("manifest", type=Path)
    cb.add_argument("--output", required=True, type=Path)
    cb.add_argument(
        "--previous",
        type=Path,
        help="prior catalog whose unchanged entries skip module re-inventory",
    )
    cb.set_defaults(command="catalog-build")
    return root


def _confirm(prompt: str, *, yes: bool, json_mode: bool = False) -> bool:
    if yes:
        return True
    if json_mode or not sys.stdin.isatty():
        return False
    return input(f"{prompt} [Y/n] ").strip().lower() in {"", "y", "yes"}


def _declined_exit(*, json_mode: bool = False) -> int:
    """An interactive decline succeeds; a prompt that could not be asked fails."""
    if json_mode or not sys.stdin.isatty():
        return 2
    return 0


def _path_is_project_root(path: Path) -> bool:
    path = path.expanduser().resolve()
    return any((path / name).is_file() for name in ("lakefile.toml", "lakefile.lean"))


def _apply_using(args: argparse.Namespace) -> None:
    """Classify the single v4 context override into the exact internal model."""
    value = getattr(args, "using", None)
    if not value:
        return
    kind: str | None = None
    payload = value
    for prefix in ("package", "lock", "env", "toolchain", "lean", "project"):
        marker = prefix + ":"
        if value.startswith(marker):
            kind, payload = prefix, value[len(marker) :]
            break
    if kind == "toolchain" and payload.startswith("lean:"):
        # The explicit prefix accepts the same `lean:vX.Y.Z` shorthand as the
        # bare spelling instead of passing the raw string to Elan.
        payload = payload[len("lean:") :]
    candidate = Path(payload).expanduser()
    if kind is None:
        if candidate.is_dir():
            kind = "project"
        elif candidate.is_file():
            kind = "lock"
        elif "@" in payload or payload.startswith("github:"):
            kind = "package"
        elif re.fullmatch(r"(?:leanprover/lean4:)?v?\d+\.\d+\.\d+", payload):
            kind = "toolchain"
        else:
            kind = "env"
    if not payload:
        raise ValueError("--using requires a non-empty context")
    if kind == "project":
        args.project = candidate
    elif kind == "lock":
        args._using_lock = candidate
    elif kind == "package":
        args.package_refs = [payload]
    elif kind in {"toolchain", "lean"}:
        args.toolchain = payload
    else:
        args.environment = payload


def _standalone_check_args(args: argparse.Namespace, source: Path) -> argparse.Namespace:
    """Translate the small v4 check vocabulary to the discovery engine contract."""
    lock = getattr(args, "_using_lock", None)
    return argparse.Namespace(
        file=source,
        requires=list(args.package_refs),
        lock=lock,
        lock_out=args.lock_out,
        toolchain=args.toolchain,
        catalog=None,
        no_discover=False,
        offline=args.offline,
        no_source_build=args.no_source_build,
        allow_source_build=args.allow_source_build,
        max_candidates=3,
        max_remote_acquisitions=2,
        search_timeout=90.0,
        wall_timeout=1800.0,
        acquire_timeout=1800.0,
        home=args.home,
        json=args.json,
        json_events=False,
        quiet=args.quiet,
        verbose=args.verbose,
        max_download=args.max_download,
        plan=args.plan,
        explain=False,
        timings=args.timings,
        check_timeout=args.timeout,
        availability=args.availability,
        libraries=args.libraries,
        publisher_verification=args.publisher_verification,
        trusted_publisher=args.trusted_publisher,
        trusted_issuer=args.trusted_issuer,
        verification_tool=args.verification_tool,
    )


def main(argv: list[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:]) if argv is None else list(argv)
    first_positional = next((token for token in raw_arguments if not token.startswith("-")), None)
    if first_positional is not None and first_positional.endswith(".lean"):
        print(
            f"lean-runtime: {first_positional!r} is a Lean file, not a command.\n"
            f"Try: lean-runtime check {first_positional}",
            file=sys.stderr,
        )
        return 2
    try:
        args = parser().parse_args(raw_arguments)
    except ValueError as exc:
        print(f"lean-runtime: {exc}", file=sys.stderr)
        return 2
    if args.command == "update":
        args.plan = args.dry_run
    if args.command == "check":
        _apply_using(args)
        if args.matrix is not None:
            args.across = args.matrix
        if (
            not args.watch
            and args.repeat is None
            and args.across is None
            and not args.include
            and len(args.inputs) == 1
            and args.inputs[0] != "-"
        ):
            source = Path(args.inputs[0]).expanduser()
            project_available = False
            if args.project is not None:
                project_available = True
            elif source.is_file():
                try:
                    discover_project(source)
                    project_available = True
                except ProjectNotFoundError:
                    pass
            explicit_discovery = bool(
                args.package_refs or args.toolchain or getattr(args, "_using_lock", None)
            )
            if (
                source.is_file()
                and args.environment is None
                and (explicit_discovery or not project_available)
            ):
                return _run_front_door(
                    _standalone_check_args(args, source), command_name="lean-runtime check"
                )
    if args.command == "publish":
        args.command = f"publish-{args.publish_kind}"
    if args.command == "finalize":
        args.command = f"finalize-{args.finalize_kind}"
    if args.command == "copy":
        args.command = f"copy-{args.copy_operation}"
    if args.command == "program":
        args.command = f"program-{args.program_operation}"
    if args.command == "toolchain":
        args.command = f"toolchain-{args.toolchain_operation}"
    if args.command == "init":
        args.mathlib = None if args.core else args.mathlib_version
    operation_started = time.monotonic()
    display_path: str | None = None
    renderer = ConsoleRenderer(
        mode="quiet" if args.quiet or getattr(args, "json", False) else None,
        verbose=args.verbose,
    )
    try:
        if args.command == "completion":
            print(_completion_script(args.shell), end="")
            return 0
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
            on_event=renderer,
            availability=selected_availability,
            libraries=args.libraries,
            max_download_bytes=selected_download_limit,
            publisher_verification=args.publisher_verification,
            trusted_publisher=args.trusted_publisher,
            trusted_issuer=args.trusted_issuer,
            verification_tool=args.verification_tool,
        )
        if args.command == "declaration-index-build":
            lock = EnvironmentLock.load(args.lock)
            built = build_declaration_index(
                runtime,
                lock,
                args.output,
                weights_path=args.weights,
            )
            _json(built.to_dict())
            return 0
        if args.command == "declaration-index-publish":
            lock = EnvironmentLock.load(args.lock)
            built = load_declaration_index_build(
                args.build, expected_lock_id=lock.lock_id
            )
            di_repository = OCIRepository.parse(args.library)
            di_publication = OCIDeclarationIndexPublisher(di_repository).publish(
                tuple(di_item.source for di_item in built.shards), lock_id=lock.lock_id
            )
            if args.sign:
                CosignVerifier(executable=runtime.verification_executable).sign(
                    di_repository, di_publication.manifest_digest
                )
            _json(di_publication.to_dict())
            return 0
        if args.command == "declaration-index-inspect":
            built = load_declaration_index_build(args.build)
            indexes = []
            for di_item in built.shards:
                di_source = di_item.source
                shard = DeclarationShard(
                    di_source.shard_id,
                    di_source.package,
                    di_source.source_id,
                    di_source.toolchain,
                    di_source.subdir,
                    di_source.module_roots,
                    di_source.namespace_roots,
                    "sha256:" + "0" * 64,
                    di_source.path.stat().st_size,
                    "sha256:" + "0" * 64,
                    1,
                )
                indexes.append(
                    (
                        shard,
                        DeclarationIndex(
                            di_source.path, expected_shard_id=di_source.shard_id
                        ),
                    )
                )
            index_set = DeclarationIndexSet(built.lock_id, tuple(indexes))
            di_match = index_set.resolve(args.resolve) if args.resolve else None
            _json(
                {
                    "lock_id": built.lock_id,
                    "shards": len(built.shards),
                    "declarations": index_set.declaration_count,
                    "match": (
                        {
                            "name": di_match.name,
                            "module": di_match.module,
                            "kind": di_match.kind,
                            "weight": di_match.weight,
                        }
                        if di_match is not None
                        else None
                    ),
                }
            )
            return 0
        if args.command == "catalog-build":
            os.environ.setdefault("MATHLIB_NO_CACHE_ON_UPDATE", "1")
            catalog_result = build_catalog_file(
                args.manifest,
                args.output,
                runtime=Runtime(home=args.home, libraries=()),
                previous_path=args.previous,
            )
            print(
                f"wrote {args.output}: {len(catalog_result.entries)} environments, "
                f"{sum(len(entry.modules) for entry in catalog_result.entries)} "
                f"module records, {catalog_result.digest}"
            )
            return 0
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
            if init_plan.action != "create":
                raise ProjectError(
                    f"{init_plan.root} is already a Lake project; use `lean-runtime adopt`"
                )
            if not args.json:
                _render_init_plan(init_plan)
            if not _confirm("Create this project?", yes=args.yes, json_mode=args.json):
                if args.json:
                    _json({**init_plan.to_dict(), "created": False})
                else:
                    print("No changes made. Use --yes for non-interactive creation.")
                return _declined_exit(json_mode=args.json)
            init_result = runtime.init_project(
                args.path,
                name=args.name,
                mathlib=args.mathlib,
                toolchain=args.toolchain,
                agents=args.agents,
                ci=args.ci,
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
                return _declined_exit(json_mode=args.json)
            runtime.update_project(args.path, seed_from=args.seed_from)
            if args.json:
                _json({**update_plan.to_dict(), "applied": True})
            else:
                print(f"Updated and attached: {update_plan.root}")
            return 0
        if args.command == "publish-project":
            plan = runtime.inspect_project_publication(args.path, check_remote=True)
            if not plan.ready:
                if args.json:
                    _json({**plan.to_dict(), "configured": False})
                else:
                    print(f"Project: {plan.root}")
                    for blocker in plan.blockers:
                        print(f"  blocker: {blocker}")
                return 1
            if len(plan.modules) != 1:
                raise ProjectError(
                    "publication requires exactly one library root; use `project export` "
                    "for explicit multi-root publication"
                )
            repository = plan.repository or ""
            match = re.search(r"github\.com[/:]([^/]+)/([^/.]+)(?:\.git)?$", repository)
            if match is None:
                raise ProjectError("publish currently requires a GitHub origin")
            owner, repository_name = match.groups()
            module = plan.modules[0]
            library = f"ghcr.io/{owner.lower()}/{repository_name.lower()}-lean"
            output = plan.root / ".github/workflows/publish-lean-environment.yml"
            if args.json:
                preview = {
                    "project": str(plan.root),
                    "module": module,
                    "library": library,
                    "workflow": str(output),
                }
            else:
                print(f"Project:  {plan.root}")
                print(f"Module:   {module}")
                print(f"Publish:  {library}")
                print(f"Workflow: {output}")
            if not _confirm("Configure publication?", yes=args.yes, json_mode=args.json):
                if args.json:
                    _json({**preview, "configured": False})
                else:
                    print("No changes made. Use --yes for non-interactive configuration.")
                return _declined_exit(json_mode=args.json)
            if output.exists():
                raise ProjectError(f"publication workflow already exists: {output}")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                project_publication_workflow(library=library, module=module), encoding="utf-8"
            )
            if args.json:
                _json({**preview, "configured": True})
            else:
                print(f"Created {output}")
            return 0
        if args.command == "attach":
            if args.recursive is None:
                args.recursive = not _path_is_project_root(args.path)
            adoption_plan = runtime.plan_project_adoption(args.path, recursive=args.recursive)
            if args.dry_run:
                if args.json:
                    _json(adoption_plan.to_dict())
                else:
                    _render_adoption_plan(adoption_plan)
                return 0 if adoption_plan.blocked == 0 else 1
            if not args.json:
                _render_adoption_plan(adoption_plan)
            if not adoption_plan.ready:
                return 1
            if not _confirm("Adopt these projects?", yes=args.yes, json_mode=args.json):
                if args.json:
                    _json({**adoption_plan.to_dict(), "applied": False})
                else:
                    print("No changes made. Use --yes for non-interactive adoption.")
                return _declined_exit(json_mode=args.json)
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
            if args.dry_run:
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
                return 0 if detachment_plan.ready else 1
            if not args.json:
                print(
                    f"Materialize {len(detachment_plan.packages)} independent package "
                    f"copy/copies for {detachment_plan.root}"
                )
            if not detachment_plan.ready:
                return 1
            if not _confirm("Stop sharing dependencies?", yes=args.yes, json_mode=args.json):
                if args.json:
                    _json({**detachment_plan.to_dict(), "applied": False})
                else:
                    print("No changes made. Use --yes for non-interactive operation.")
                return _declined_exit(json_mode=args.json)
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
                # `project info` is an inspection command. Publication readiness is
                # reported as data; it is not a condition for successful inspection.
                return 0
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
        if args.command == "toolchain-install":
            print(runtime.toolchains.ensure_full(args.toolchain))
            return 0
        if args.command in {"toolchain-list", "toolchain-info"}:
            toolchains = runtime.toolchains.available_toolchains()
            if args.command == "toolchain-info":
                selected = runtime.toolchains.ensure(args.toolchain)
                toolchains = (selected,)
            toolchain_payload = [
                {
                    "toolchain": name,
                    "full": (
                        runtime.toolchains._full_toolchain_dir(name) / "bin" / "lake"
                    ).is_file(),
                    "slim": runtime.toolchains.has_slim(name),
                }
                for name in toolchains
            ]
            if args.json:
                _json(toolchain_payload)
            elif toolchain_payload:
                for toolchain_record in toolchain_payload:
                    capabilities = "full" if toolchain_record["full"] else "check-only"
                    print(f"{toolchain_record['toolchain']}  {capabilities}")
            else:
                print("No Lean toolchains available.")
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
        if args.command in {"open", "acquire"}:
            if args.command == "acquire" and args.download_only:
                runtime.availability = "required"
            environment = runtime.open_exact(
                EnvironmentLock.load(args.lock),
                name=args.name,
                build_timeout=args.timeout,
            )
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
        if args.command == "publish-environment":
            timeout_override = args.timeout
            publisher = runtime.begin_publication(
                args.publish_to,
                auth_timeout=timeout_override if timeout_override is not None else 10,
                registry_timeout=timeout_override if timeout_override is not None else 30,
            )
            access = publisher.check_access()
            if args.check_access:
                _json(
                    envelope(_schema_for(args.command), ok=True, data=access.to_dict())
                    if args.json
                    else access.to_dict()
                )
                return 0
            if args.lock is None:
                raise ValueError("publish environment requires LOCK unless --check-access is used")
            environment = runtime.open_exact(
                EnvironmentLock.load(args.lock),
                name=args.name,
                build_timeout=timeout_override if timeout_override is not None else 1800,
                accelerate=args.accelerate,
            )
            publication = runtime.publish_environment(
                environment.id,
                args.publish_to,
                tags=args.tag,
                finalize=not args.platform_only,
                sign=args.sign,
                attest=args.attest,
                publisher=publisher,
            ).to_dict()
            publication["consumer_command"] = (
                f"LEAN_RUNTIME_LIBRARIES={args.publish_to} lean-runtime env acquire "
                f"{args.lock} --download-only"
            )
            _json(
                envelope(_schema_for(args.command), ok=True, data=publication)
                if args.json
                else publication
            )
            return 0
        if args.command == "finalize-environment":
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
        if args.command == "publish-toolchain":
            _json(runtime.publish_toolchain(args.toolchain, args.library).to_dict())
            return 0
        if args.command == "finalize-toolchain":
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
        if args.command == "copy-save":
            _json(runtime.save_portable_copy(args.environment, args.output).to_dict())
            return 0
        if args.command == "copy-open":
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
                source_lock_id=args.source_lock_id,
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
        if args.command == "program-info":
            ready_program = runtime.program(args.program_id)
            _json(
                {
                    "program_id": ready_program.id,
                    "description": ready_program.description.to_dict(),
                    "location": str(ready_program.root),
                }
            )
            return 0
        if args.command == "program-run":
            ready_program = runtime.program(args.program_id)
            command = (*ready_program.description.command, *args.arguments)
            with ready_program.spawn_interactive(command) as session:
                for line in sys.stdin:
                    print(session.request_line(line.rstrip("\n")))
            program_result = session.close()
            return 0 if program_result.ok else 1
        if args.command == "program-save":
            _json(runtime.save_program_copy(args.program_id, args.output).to_dict())
            return 0
        if args.command == "program-open":
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
        if args.command == "publish-program":
            _json(
                runtime.publish_program(
                    args.program_id,
                    args.library,
                    tags=args.tag,
                    sign=args.sign,
                ).to_dict()
            )
            return 0
        if args.command == "finalize-program":
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
        if args.command == "status":
            subject_path = Path(args.subject).expanduser()
            status_payload: dict[str, Any]
            try:
                project_status = discover_project(subject_path)
            except ProjectNotFoundError:
                if subject_path.is_file() and subject_path.suffix == ".lean":
                    catalog = default_catalog()
                    source_text = subject_path.read_text(encoding="utf-8")
                    discovery = Discovery(catalog=catalog, runtime=runtime)
                    evidence = discovery.analyze(source_text)
                    static_plan = discovery.plan(source_text, evidence=evidence)
                    discovery_plan, _ = discovery.order(source_text, evidence, static_plan)
                    status_payload = {
                        "kind": "standalone",
                        "subject": str(subject_path.resolve()),
                        "imports": list(discovery_plan.evidence.imports),
                        "candidates": [
                            candidate.entry.id for candidate in discovery_plan.candidates
                        ],
                        "planned_first": (
                            discovery_plan.candidates[0].entry.id
                            if discovery_plan.candidates
                            else None
                        ),
                        "availability": {
                            candidate.entry.id: {
                                "local": runtime.exact_ready_locally(
                                    candidate.entry.lock,
                                    import_roots=evidence.imports,
                                ),
                                "remote": "not_probed",
                            }
                            for candidate in discovery_plan.candidates
                        },
                    }
                elif subject_path.is_file() and subject_path.suffix == ".json":
                    lock = EnvironmentLock.load(subject_path)
                    status_payload = {
                        "kind": "lock",
                        "subject": str(subject_path.resolve()),
                        "lock_id": lock.lock_id,
                        "toolchain": lock.toolchain,
                        "packages": len(lock.packages),
                    }
                else:
                    try:
                        environment = runtime.environment(args.subject)
                    except (LeanRuntimeError, ValueError):
                        status_payload = {
                            "kind": "unconfigured",
                            "subject": str(subject_path.resolve()),
                            "message": "no pinned Lake project, lock, or named environment found",
                        }
                    else:
                        status_payload = {
                            "kind": "environment",
                            **environment.inspect().to_dict(),
                        }
            else:
                status_payload = {
                    "kind": "project",
                    "root": str(project_status.root),
                    "toolchain": project_status.toolchain,
                    "manifest": str(project_status.manifest) if project_status.manifest else None,
                    "attached": (project_status.root / "lean-runtime.toml").is_file(),
                }
            if args.json:
                _json(status_payload)
            else:
                print(f"{status_payload['kind'].title()}")
                for key, value in status_payload.items():
                    if key == "kind":
                        continue
                    label = key.replace("_", " ").title()
                    if isinstance(value, dict):
                        print(f"  {label}")
                        for name, detail in value.items():
                            if isinstance(detail, dict):
                                detail = " ".join(f"{k}={v}" for k, v in detail.items())
                            print(f"    {name:<20} {detail}")
                    elif isinstance(value, (list, tuple)):
                        print(f"  {label:<14} {', '.join(str(item) for item in value) or '-'}")
                    else:
                        print(f"  {label:<14} {value}")
            return 0
        if args.command == "environments":
            records = runtime.list_environments()
            if args.json:
                _json(list(records))
            else:
                _render_environments(records)
            return 0
        if args.command == "storage":
            if args.verify and not args.json:
                print("Verifying storage ledger…", file=sys.stderr)
            status = runtime.store_status(verify=args.verify)
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
            if args.yes or (
                not doctor_report.ok
                and _confirm("Apply safe repairs?", yes=False, json_mode=args.json)
            ):
                doctor_report = runtime.doctor_fix()
            if args.json:
                _json(doctor_report.to_dict())
            else:
                _render_doctor(doctor_report)
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
            execute_cleanup = False
            cleanup_preview = runtime.clean(
                dry_run=True,
                minimum_age_seconds=args.minimum_age_hours * 3600,
                keep_last=args.keep_last,
            )
            preview_downloads = (
                runtime.clean_downloads(
                    dry_run=True,
                    minimum_age_seconds=args.minimum_age_hours * 3600,
                )
                if args.include_downloads
                else None
            )
            preview_scratch = runtime.clean_scratch(
                dry_run=True,
                minimum_age_seconds=min(args.minimum_age_hours * 3600, 3600),
            )
            if not args.json:
                _render_cleanup(cleanup_preview, preview_downloads, preview_scratch)
            has_candidates = bool(
                cleanup_preview.candidates
                or (preview_downloads and preview_downloads.candidates)
                or preview_scratch.candidates
            )
            if has_candidates and not args.dry_run:
                execute_cleanup = _confirm("Remove these files?", yes=args.yes, json_mode=args.json)
            gc_report = runtime.clean(
                dry_run=not execute_cleanup,
                minimum_age_seconds=args.minimum_age_hours * 3600,
                keep_last=args.keep_last,
            )
            gc_downloads = (
                runtime.clean_downloads(
                    dry_run=not execute_cleanup,
                    minimum_age_seconds=args.minimum_age_hours * 3600,
                )
                if args.include_downloads
                else None
            )
            gc_scratch = runtime.clean_scratch(
                dry_run=not execute_cleanup,
                minimum_age_seconds=min(args.minimum_age_hours * 3600, 3600),
            )
            if args.json:
                gc_payload: dict[str, Any] = {
                    "environments": gc_report.to_dict(),
                    "downloaded_files": gc_downloads.to_dict() if gc_downloads else None,
                    "scratch": gc_scratch.to_dict(),
                }
                _json(
                    envelope(
                        "lean-runtime.cleanup/v1",
                        ok=True,
                        data=gc_payload,
                    )
                )
            elif execute_cleanup:
                _render_cleanup(gc_report, gc_downloads, gc_scratch)
            return 0
        if args.command == "check":
            if args.across is not None:
                if (
                    len(args.inputs) != 1
                    or args.package_refs
                    or args.include
                    or args.project
                    or args.environment
                ):
                    raise ValueError("check --across requires exactly one FILE")
                across_file = Path(args.inputs[0])
                contexts = load_matrix(args.across)
                matrix_result = runtime.check_matrix(
                    across_file.read_text(encoding="utf-8"),
                    contexts=contexts,
                    filename=across_file.name,
                    base=args.across.parent,
                    concurrency=args.concurrency,
                )
                if args.json:
                    _json(serialize_matrix_v1(matrix_result))
                else:
                    print("Context\tResult\tTime\tEnvironment")
                    for entry in matrix_result.entries:
                        print(
                            f"{entry.context}\t{'accepted' if entry.result.ok else 'rejected'}\t"
                            f"{entry.result.elapsed_seconds:.2f}s\t"
                            f"{entry.result.environment_id or '-'}"
                        )
                return 0 if matrix_result.ok else 1
            if args.repeat is not None:
                if (
                    len(args.inputs) != 1
                    or args.inputs[0] == "-"
                    or args.package_refs
                    or args.include
                ):
                    raise ValueError("check --repeat requires exactly one FILE")
                if args.repeat < 1 or args.warmup < 0:
                    raise ValueError(
                        "check --repeat requires positive samples and nonnegative warmups"
                    )
                repeated_file = Path(args.inputs[0])
                if args.environment is not None:
                    environment_report = runtime.profile(
                        args.environment,
                        repeated_file,
                        warmup=args.warmup,
                        repeat=args.repeat,
                    )
                    if args.json:
                        _json(serialize_profile_v1(environment_report))
                    else:
                        print(f"Profile: {repeated_file.name}")
                        print(f"Samples: {len(environment_report.results)}")
                        for name, value in environment_report.statistics().items():
                            if value is not None:
                                print(f"  {name:<6} {value:g} ms")
                    return 0 if environment_report.ok else 1
                if os.environ.get(_HEADER_SNAPSHOTS_VARIABLE) is None:
                    runtime.header_cache.enabled = True
                for _ in range(args.warmup):
                    warm = runtime.check_file(
                        repeated_file, project=args.project, policy=_policy(args)
                    )
                    if not warm.ok:
                        _emit_result(warm, args.json)
                        return 2 if warm.timed_out else 1
                repeated_started = time.monotonic()
                samples = tuple(
                    runtime.check_file(repeated_file, project=args.project, policy=_policy(args))
                    for _ in range(args.repeat)
                )
                profile_report = ProfileReport(
                    str(repeated_file), args.warmup, samples, time.monotonic() - repeated_started
                )
                if args.json:
                    _json(serialize_profile_v1(profile_report))
                else:
                    print(f"Profile: {repeated_file.name}")
                    for name, value in profile_report.statistics().items():
                        if value is not None:
                            print(f"  {name:<6} {value:g} ms")
                return 0 if profile_report.ok else 1
            if args.watch:
                if args.json:
                    raise ValueError("check --watch does not support --json; use one-shot check")
                if (
                    args.package_refs
                    or args.include
                    or len(args.inputs) != 1
                    or args.inputs[0] == "-"
                ):
                    raise ValueError("check --watch requires exactly one project FILE")
                watched = Path(args.inputs[0]).expanduser().resolve()
                if not watched.is_file():
                    raise ValueError(f"watched Lean file does not exist: {watched}")
                print(f"Watching {watched} · Ctrl-C to stop")
                if os.environ.get(_HEADER_SNAPSHOTS_VARIABLE) is None:
                    runtime.header_cache.enabled = True
                previous: tuple[int, int] | None = None
                while True:
                    stat = watched.stat()
                    signature = (stat.st_mtime_ns, stat.st_size)
                    if signature != previous:
                        previous = signature
                        watched_result = runtime.check_file(
                            watched,
                            toolchain=args.toolchain,
                            project=args.project,
                            policy=_policy(args),
                        )
                        _emit_result(watched_result, False)
                    time.sleep(args.watch_interval)
            check_subject = (
                Path(args.inputs[-1]).name
                if args.inputs and args.inputs[-1] != "-"
                else "stdin"
                if args.inputs
                else Path(args.project or ".").resolve().name
            )
            runtime.events.emit("check.started", "Checking Lean input", subject=check_subject)
            if args.project is not None and args.package_refs:
                raise ValueError("check cannot combine project and package contexts")
            if args.environment is not None and (
                args.package_refs or args.project or args.toolchain
            ):
                raise ValueError(
                    "an environment context cannot be combined with another --using context"
                )
            if not args.inputs:
                if args.package_refs or args.include:
                    raise ValueError(
                        "project-wide check does not accept package context or --include"
                    )
                if args.environment is not None:
                    raise ValueError("an environment context requires at least one FILE")
                try:
                    result = runtime.check_project(
                        args.project or Path("."), toolchain=args.toolchain, policy=_policy(args)
                    )
                except ProjectNotFoundError as exc:
                    if not sys.stdin.isatty():
                        raise ProjectNotFoundError(
                            f"{exc}\nTo check Lean source from stdin, pass '-': "
                            "lean-runtime check - --using CONTEXT"
                        ) from exc
                    raise
                source_file = None
            elif args.package_refs:
                if len(args.inputs) != 1:
                    raise ValueError("a package context expects exactly one FILE")
                environment = runtime.open_references(args.package_refs, toolchain=args.toolchain)
                source_file = Path(args.inputs[0])
            elif "-" in args.inputs:
                if len(args.inputs) != 1:
                    raise ValueError("stdin (-) must be the only check input")
                if args.environment is not None:
                    environment = runtime.environment(args.environment)
                    source_file = Path("-")
                else:
                    if args.include:
                        raise ValueError("local project checks do not accept --include")
                    display_path = "<stdin>"
                    result = runtime.check(
                        sys.stdin.read(),
                        toolchain=args.toolchain,
                        project=args.project,
                        policy=_policy(args),
                    )
                    source_file = None
            else:
                selected_files = _expand_check_inputs(args.inputs)
                if len(selected_files) > 1:
                    if args.include:
                        raise ValueError("multi-file checks do not accept --include")
                    return _run_check_batch(runtime, selected_files, args)
                if args.environment is not None:
                    environment = runtime.environment(args.environment)
                    source_file = selected_files[0]
                else:
                    if args.include:
                        raise ValueError("local project checks do not accept --include")
                    result = runtime.check_file(
                        selected_files[0],
                        toolchain=args.toolchain,
                        project=args.project,
                        policy=_policy(args),
                    )
                    source_file = None
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
        else:
            result = runtime.build(
                args.project,
                targets=args.targets,
                toolchain=args.toolchain,
                timeout=args.timeout,
                shared=args.shared,
                artifact_cache=args.artifact_cache,
            )
    except KeyboardInterrupt:
        renderer.close()
        print("lean-runtime: interrupted", file=sys.stderr)
        return 130
    except (ResolutionError, MaterializationError) as exc:
        renderer.close()
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
    except PublicationError as exc:
        renderer.close()
        if getattr(args, "json", False):
            _json(
                envelope(
                    _schema_for(args.command),
                    ok=False,
                    data=exc.to_dict(),
                    errors=[error("publication_failed", str(exc), details=exc.to_dict())],
                )
            )
        else:
            _print_publication_failure(exc)
        return exc.exit_code
    except (
        LeanRuntimeError,
        DiscoveryError,
        OSError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        renderer.close()
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
    if args.command == "check":
        runtime.events.emit("check.completed", "Lean check completed", ok=result.ok)
        total_ms = round((time.monotonic() - operation_started) * 1000)
        accounted_ms = sum(timing.duration_ms for timing in result.timings)
        result = replace(
            result,
            timings=(
                PhaseTiming("command_preparation", max(0, total_ms - accounted_ms)),
                *result.timings,
            ),
        )
    renderer.close()
    _emit_result(result, args.json, display_path=display_path)
    if args.timings:
        print(render_timings(result.timings), file=sys.stderr)
    if result.ok:
        return 0
    # A hit resource limit is an execution-policy outcome, not a verdict.
    return 2 if result.timed_out else 1


if __name__ == "__main__":
    raise SystemExit(main())
