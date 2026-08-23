"""Internal zero-configuration discovery engine used by the v4 check command."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from .console import ConsoleRenderer, styler_for
from .context_resolution import FileContextResolution, resolve_file_context
from .discovery import (
    Catalog,
    Discovery,
    DiscoveryError,
    DiscoveryPolicy,
    DiscoveryResult,
    default_catalog,
)
from .discovery.analyzer import SourceEvidence, analyze_source
from .errors import LeanRuntimeError, SpecificationError
from .events import RuntimeEvent
from .frontmatter import LeanFrontmatter, parse_frontmatter
from .lockfiles import EnvironmentLock
from .models import ExecutionResult, PhaseTiming
from .policies import ExecutionPolicy, format_byte_size, parse_byte_size
from .runtime import Runtime
from .timings import render_timings
from .wire import envelope, error, serialize_execution_v1


def add_run_arguments(parser: argparse.ArgumentParser, *, standalone: bool = True) -> None:
    """Register the internal standalone-discovery argument contract.

    With ``standalone=False`` the options that also exist as `lean-runtime`
    global options use ``argparse.SUPPRESS`` defaults, so a value parsed by the
    root parser survives unless it is repeated after the subcommand.
    """
    shared: dict[str, Any] = {} if standalone else {"default": argparse.SUPPRESS}
    parser.add_argument("file", type=Path)
    parser.add_argument("--with", dest="requires", action="append", default=[])
    parser.add_argument("--lock", type=Path, help="use an exact environment lock")
    parser.add_argument("--lock-out", type=Path, help="write the resolved exact lock")
    parser.add_argument("--toolchain", help="Lean version for a core-only file")
    parser.add_argument("--catalog", type=Path, help="override the bundled discovery catalog")
    parser.add_argument(
        "--no-discover",
        action="store_true",
        help="require explicit context or a pinned Lake project",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="use retained local environments only",
    )
    parser.add_argument(
        "--no-source-build",
        action="store_true",
        help="deprecated compatibility spelling; source builds are disabled by default",
    )
    parser.add_argument(
        "--allow-source-build",
        action="store_true",
        help="allow automatic discovery to build missing environments from source",
    )
    parser.add_argument("--max-candidates", type=int, default=3)
    parser.add_argument("--max-remote-acquisitions", type=int, default=2)
    parser.add_argument(
        "--search-timeout",
        dest="search_timeout",
        type=float,
        default=90.0,
        help="aggregate budget for compiler probes",
    )
    parser.add_argument(
        "--wall-timeout",
        type=float,
        default=1800.0,
        help="wall-clock budget including downloads and compiler probes",
    )
    parser.add_argument(
        "--acquire-timeout",
        type=float,
        default=1800.0,
        help="budget for downloading, installing, or building one candidate environment",
    )
    parser.add_argument("--home", help="runtime store root", **shared)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--json-events",
        action="store_true",
        help="stream runtime lifecycle events to stderr as JSON lines",
    )
    parser.add_argument("--quiet", action="store_true", **shared)
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show every runtime event while preparing",
        **shared,
    )
    parser.add_argument(
        "--max-download",
        type=parse_byte_size,
        metavar="SIZE",
        help="fail instead of downloading more than SIZE (e.g. 500MiB)",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="report the acquisition cost without downloading or checking",
    )
    parser.add_argument(
        "--explain", action="store_true", help="explain context selection without running"
    )
    parser.add_argument(
        "--timings",
        action="store_true",
        help="show preparation and execution timings",
        **shared,
    )
    parser.add_argument(
        "--check-timeout",
        dest="check_timeout",
        type=float,
        default=300,
        help="budget for one Lean invocation",
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="lean-runtime-internal-discovery",
        description=(
            "Discover or select an exact Lean context and check one file. "
            "Used internally by `lean-runtime check`."
        ),
        epilog="Public spelling: lean-runtime check FILE",
    )
    add_run_arguments(root, standalone=True)
    return root


def _selected_policy(arguments: argparse.Namespace) -> tuple[str, tuple[str, ...] | None]:
    """Resolve availability and libraries from run flags and any global options."""
    global_availability = getattr(arguments, "availability", None)
    global_libraries = getattr(arguments, "libraries", None)
    if arguments.offline and global_availability in {"auto", "required"}:
        raise SpecificationError(
            "--offline cannot be combined with --availability auto or required"
        )
    allow_source_build = getattr(arguments, "allow_source_build", False)
    if allow_source_build and arguments.no_source_build:
        raise SpecificationError("--allow-source-build cannot be combined with --no-source-build")
    if allow_source_build and arguments.offline:
        raise SpecificationError("--allow-source-build cannot be combined with --offline")
    if arguments.offline:
        return "local", ()
    availability = "auto" if allow_source_build else (global_availability or "required")
    libraries = tuple(global_libraries) if global_libraries else None
    return availability, libraries


def _catalog(path: Path | None) -> Catalog:
    return Catalog.from_file(path.expanduser().resolve()) if path is not None else default_catalog()


def _discovery_policy(arguments: argparse.Namespace) -> DiscoveryPolicy:
    allow_source_build = getattr(arguments, "allow_source_build", False)
    if allow_source_build and arguments.catalog is not None:
        raise SpecificationError(
            "automatic source builds are not allowed from a custom discovery catalog"
        )
    return DiscoveryPolicy(
        max_candidates=arguments.max_candidates,
        max_remote_acquisitions=arguments.max_remote_acquisitions,
        max_total_seconds=arguments.search_timeout,
        max_wall_seconds=arguments.wall_timeout,
        allow_download=not arguments.offline,
        allow_source_build=allow_source_build,
        candidate_timeout_seconds=arguments.check_timeout,
        acquisition_timeout_seconds=arguments.acquire_timeout,
    )


def _discovery_failure(result: DiscoveryResult) -> str:
    details = [item.detail for item in result.diagnostics]
    if not details:
        details = [item.diagnostics[0].detail for item in result.attempts if item.diagnostics]
    tested = len(result.attempts)
    planned = len(getattr(result.plan, "planned_candidates", result.plan.candidates))
    untried = max(0, planned - tested)
    suffix = f" ({tested} candidate(s) tested"
    if untried:
        suffix += f", {untried} untried"
    suffix += ")"
    return (details[0] if details else "no compatible environment was found") + suffix


def _discovery_summary(result: DiscoveryResult) -> str:
    tested = len(result.attempts)
    planned = len(getattr(result.plan, "planned_candidates", result.plan.candidates))
    untried = max(0, planned - tested)
    completion = result.completion
    reason = {
        "time_limit": "wall-clock time limit reached",
        "candidate_limit": "candidate limit reached",
        "acquisition_limit": "remote acquisition limit reached",
        "complete": "plausible candidates exhausted",
    }.get(completion, "discovery did not find a compatible environment")
    suffix = f"; {untried} planned candidate(s) untried" if untried else ""
    return (
        f"Discovery: {reason} after {tested}/{planned} candidate(s) "
        f"in {result.duration_seconds:.2f}s{suffix}"
    )


def _explain_discovery(
    arguments: argparse.Namespace,
    source: str,
    evidence: SourceEvidence,
) -> dict[str, object]:
    plan = Discovery(
        catalog=_catalog(arguments.catalog),
        policy=_discovery_policy(arguments),
    ).plan(source, evidence=evidence)
    return {
        "decision": "automatic_discovery",
        "context": "catalog candidates",
        "subject": [candidate.entry.id for candidate in plan.candidates],
        "plan": plan.to_dict(),
    }


def _combine(arguments: argparse.Namespace, metadata: LeanFrontmatter | None) -> LeanFrontmatter:
    embedded = metadata or LeanFrontmatter()
    if arguments.requires and embedded.requires:
        raise SpecificationError("cannot combine --using and frontmatter 'requires'")
    if arguments.lock is not None and embedded.lock is not None:
        raise SpecificationError("cannot combine --lock and frontmatter 'lock'")
    if arguments.toolchain is not None and embedded.toolchain is not None:
        raise SpecificationError("cannot combine --toolchain and frontmatter 'toolchain'")
    requires = tuple(arguments.requires) or embedded.requires
    lock = str(arguments.lock) if arguments.lock is not None else embedded.lock
    toolchain = arguments.toolchain or embedded.toolchain
    if lock is not None and requires:
        raise SpecificationError("a Lean file cannot combine an exact lock with dependencies")
    if lock is not None and toolchain is not None:
        raise SpecificationError("an exact lock cannot be combined with a toolchain override")
    return LeanFrontmatter(requires, toolchain, lock)


def _lock_path(value: str, source: Path, *, embedded: bool) -> Path:
    path = Path(value).expanduser()
    if embedded and not path.is_absolute():
        path = source.parent / path
    return path.resolve()


def _display_text(text: str, result: ExecutionResult, filename: str, display_path: str) -> str:
    """Rewrite the exact staged entrypoint path to the path the user passed.

    Only the known staged source path is rewritten; dependency paths and path
    text embedded inside compiler messages are left untouched.
    """
    staged_entry = str(Path(result.cwd) / filename)
    return text.replace(staged_entry, display_path)


def _emit(
    result: ExecutionResult,
    *,
    as_json: bool,
    filename: str,
    display_path: str | None = None,
    show_timings: bool = False,
    summary_elapsed_seconds: float | None = None,
    discovery_summary: str | None = None,
) -> None:
    if as_json:
        print(
            json.dumps(serialize_execution_v1(result), ensure_ascii=False, indent=2, sort_keys=True)
        )
        return
    shown = display_path or filename
    if result.stdout:
        stdout = _display_text(result.stdout, result, filename, shown)
        print(stdout, end="" if stdout.endswith("\n") else "\n")
    if result.stderr:
        stderr = _display_text(result.stderr, result, filename, shown)
        print(stderr, end="" if stderr.endswith("\n") else "\n", file=sys.stderr)
    for hint in result.hints:
        if discovery_summary is not None and hint == discovery_summary:
            continue
        print(f"Hint: {hint}", file=sys.stderr)
    style = styler_for(sys.stdout)
    symbol = style.green("✓") if result.ok else style.red("✗")
    if result.ok:
        status = style.green("accepted")
    elif result.timed_out:
        status = style.red("timed out")
    else:
        status = style.red("rejected")
    elapsed = result.elapsed_seconds if summary_elapsed_seconds is None else summary_elapsed_seconds
    timing = style.dim(f"in {elapsed:.2f}s")
    print(f"{symbol} {shown} {status} {timing}")
    if discovery_summary is not None:
        print(discovery_summary)
    if show_timings:
        print(render_timings(result.timings))


def _plan_lock(
    arguments: argparse.Namespace,
    context: LeanFrontmatter,
    source: str,
    source_path: Path,
    resolution: FileContextResolution,
    evidence: SourceEvidence,
) -> tuple[EnvironmentLock, str | None]:
    """Select the exact lock whose acquisition cost --plan should report."""
    if context.requires or context.toolchain is not None:
        raise SpecificationError(
            "--plan supports an exact lock or automatic discovery; explicit dependencies "
            "need Lake resolution first (run once with --write-lock, then plan the lock)"
        )
    if context.lock is not None:
        path = _lock_path(context.lock, source_path, embedded=arguments.lock is None)
        return EnvironmentLock.load(path), None
    if resolution.kind == "discovery":
        plan = Discovery(
            catalog=_catalog(arguments.catalog),
            policy=_discovery_policy(arguments),
        ).plan(source, evidence=evidence)
        if not plan.candidates:
            raise SpecificationError(
                "no plausible catalog candidate for this file; nothing to plan"
            ) from None
        candidate = plan.candidates[0]
        return candidate.entry.lock, candidate.entry.id
    assert resolution.project is not None
    raise SpecificationError(
        f"--plan is not supported inside a Lake project: {resolution.project.root}"
    )


def _render_plan(report: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                envelope("lean-runtime.plan/v1", ok=True, data=dict(report)),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return
    subject = report.get("candidate") or report.get("lock_id")
    print(f"Environment: {subject} · {report['toolchain']}")
    print(f"Environment ready locally: {'yes' if report['environment_ready'] else 'no'}")
    print(f"Toolchain installed: {'yes' if report['toolchain_installed'] else 'no'}")
    download = report["download_bytes"]
    libraries = report.get("libraries") or []
    environment_download = report.get("environment_download_bytes")
    toolchain_download = report.get("toolchain_download_bytes")
    if isinstance(environment_download, int):
        print(f"Environment closure: {format_byte_size(environment_download)}")
    else:
        print("Environment closure: unknown")
    if isinstance(toolchain_download, int):
        print(f"Lean check runtime: {format_byte_size(toolchain_download)}")
    else:
        print("Lean check runtime: unknown (no published slim runtime found)")
    if download == 0 and report.get("download_bytes_complete", True):
        print("Download required: none")
    elif isinstance(download, int):
        selected: dict[str, Any] = next((item for item in libraries if item.get("available")), {})
        cached = selected.get("cached_bytes", 0) if isinstance(selected, dict) else 0
        suffix = (
            f" ({format_byte_size(cached)} already cached)"
            if isinstance(cached, int) and cached > 0
            else ""
        )
        qualifier = "at least " if not report.get("download_bytes_complete", True) else ""
        print(f"Download required: {qualifier}{format_byte_size(download)}{suffix}")
        if selected:
            print(f"Library: {selected['library']}")
    else:
        detail = ""
        if isinstance(libraries, list) and libraries:
            detail = f" ({libraries[0].get('error', 'no library responded')})"
        print(f"Download required: unknown{detail}")
    limit = report.get("max_download_bytes")
    if isinstance(limit, int):
        allowed = (
            report.get("download_bytes_complete", True)
            and isinstance(download, int)
            and download <= limit
        )
        print(f"Download limit: {format_byte_size(limit)} ({'ok' if allowed else 'exceeded'})")


def run(
    arguments: argparse.Namespace,
    *,
    command_name: str = "lean-runtime-internal-discovery",
) -> int:
    """Execute standalone context selection for the public `check` command."""
    source_path = arguments.file.expanduser().resolve()
    renderer = ConsoleRenderer(
        mode="quiet" if arguments.quiet or arguments.json else None,
        verbose=arguments.verbose,
    )
    try:
        source = source_path.read_text(encoding="utf-8")
        evidence = analyze_source(source)
        embedded = parse_frontmatter(source)
        context = _combine(arguments, embedded)
        context_resolution = resolve_file_context(
            source_path,
            context,
            discover=not arguments.no_discover,
        )
        if arguments.explain:
            if context_resolution.kind == "lock":
                selected = "exact lock"
                detail = context.lock
                explanation: dict[str, object] = {
                    "decision": "context_selected",
                    "context": selected,
                    "subject": detail,
                }
            elif context_resolution.kind == "references":
                selected = "standalone dependencies"
                detail = ", ".join(context.requires)
                explanation = {
                    "decision": "context_selected",
                    "context": selected,
                    "subject": detail,
                }
            elif context_resolution.kind == "toolchain":
                selected = "standalone toolchain"
                detail = context.toolchain
                explanation = {
                    "decision": "context_selected",
                    "context": selected,
                    "subject": detail,
                }
            elif context_resolution.kind == "project":
                assert context_resolution.project is not None
                selected = "local project"
                detail = str(context_resolution.project.root)
                explanation = {
                    "decision": "context_selected",
                    "context": selected,
                    "subject": detail,
                    "reasons": list(context_resolution.reasons),
                }
            else:
                explanation = _explain_discovery(arguments, source, evidence)
                selected = "automatic discovery"
                candidates = explanation["subject"]
                detail = ", ".join(candidates) if isinstance(candidates, list) else str(candidates)
            if arguments.json:
                print(
                    json.dumps(
                        envelope("lean-runtime.inspect/v1", ok=True, data=explanation),
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                print(f"Context: {selected}\nSelected: {detail}")
            return 0
        if arguments.plan:
            lock, candidate_id = _plan_lock(
                arguments,
                context,
                source,
                source_path,
                context_resolution,
                evidence,
            )
            _, plan_libraries = _selected_policy(arguments)
            runtime = Runtime(
                home=arguments.home,
                max_download_bytes=arguments.max_download,
                libraries=plan_libraries,
            )
            report = runtime.plan_exact(lock, import_roots=evidence.imports)
            if candidate_id is not None:
                report["candidate"] = candidate_id
            _render_plan(report, as_json=arguments.json)
            return 0
        preparation_started = time.monotonic()
        runtime_events: list[RuntimeEvent] = []

        def observe(event: RuntimeEvent) -> None:
            runtime_events.append(event)
            if arguments.json_events:
                print(
                    json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True), file=sys.stderr
                )
            renderer(event)

        availability, libraries = _selected_policy(arguments)
        runtime = Runtime(
            home=arguments.home,
            on_event=observe,
            availability=availability,
            libraries=libraries,
            max_download_bytes=arguments.max_download,
            allow_source_build=getattr(arguments, "allow_source_build", False),
        )
        policy = ExecutionPolicy(timeout_seconds=arguments.check_timeout)
        rejected_discovery: DiscoveryResult | None = None
        if context_resolution.kind == "lock":
            if arguments.lock_out is not None:
                raise SpecificationError("--write-lock cannot be combined with an exact lock")
            assert context.lock is not None
            lock_path = _lock_path(
                context.lock,
                source_path,
                embedded=arguments.lock is None,
            )
            environment = runtime.open_exact(
                EnvironmentLock.load(lock_path), import_roots=evidence.imports
            )
            preparation = PhaseTiming(
                "environment_open", round((time.monotonic() - preparation_started) * 1000)
            )
            result = environment.check(source, filename=source_path.name, policy=policy)
        elif context_resolution.kind == "references":
            resolution_started = time.monotonic()
            lock = runtime.prepare_references(context.requires, toolchain=context.toolchain)
            resolution = PhaseTiming(
                "resolution", round((time.monotonic() - resolution_started) * 1000)
            )
            if arguments.lock_out is not None:
                lock.write(arguments.lock_out)
            environment = runtime.open_exact(lock, import_roots=evidence.imports)
            preparation = PhaseTiming(
                "environment_open",
                round((time.monotonic() - resolution_started) * 1000) - resolution.duration_ms,
            )
            result = environment.check(source, filename=source_path.name, policy=policy)
            result = replace(result, timings=(resolution, preparation, *result.timings))
        elif context_resolution.kind == "toolchain":
            preparation = PhaseTiming(
                "toolchain", round((time.monotonic() - preparation_started) * 1000)
            )
            result = runtime.check_file(source_path, toolchain=context.toolchain, policy=policy)
        elif context_resolution.kind == "project":
            if arguments.lock_out is not None:
                raise SpecificationError(
                    "--write-lock is only available for explicit dependencies or discovery"
                )
            preparation = PhaseTiming(
                "environment_open", round((time.monotonic() - preparation_started) * 1000)
            )
            result = runtime.check_file(source_path, policy=policy)
        else:
            discovery_started = time.monotonic()
            discovery_catalog = _catalog(arguments.catalog)
            discovery_policy = _discovery_policy(arguments)
            discovery = Discovery(
                catalog=discovery_catalog,
                policy=discovery_policy,
                runtime=runtime,
                runtime_events=runtime_events,
                filename=source_path.name,
            )
            if not discovery.has_local_history_hint(source, evidence=evidence):
                renderer.note("Discovering an exact environment")
            discovered = discovery.discover_and_check(source, evidence=evidence)
            if discovered.status != "found" or discovered.execution_result is None:
                rejection = discovered.best_rejection
                if rejection is None or rejection.execution_result is None:
                    raise SpecificationError(_discovery_failure(discovered)) from None
                result = rejection.execution_result
                rejected_discovery = discovered
            else:
                if arguments.lock_out is not None:
                    assert discovered.lock is not None
                    discovered.lock.write(arguments.lock_out)
                result = discovered.execution_result
            preparation = PhaseTiming(
                "discovery", round((time.monotonic() - discovery_started) * 1000)
            )
        if context_resolution.kind in {"lock", "toolchain", "project", "discovery"}:
            result = replace(result, timings=(preparation, *result.timings))
        if rejected_discovery is not None:
            result = replace(
                result,
                hints=(*result.hints, _discovery_summary(rejected_discovery)),
            )
        renderer.close()
        _emit(
            result,
            as_json=arguments.json,
            filename=source_path.name,
            display_path=str(arguments.file),
            show_timings=arguments.timings,
            summary_elapsed_seconds=(
                rejected_discovery.duration_seconds if rejected_discovery is not None else None
            ),
            discovery_summary=(
                _discovery_summary(rejected_discovery) if rejected_discovery is not None else None
            ),
        )
        if result.ok:
            return 0
        # A hit resource limit is an execution-policy outcome, not a verdict.
        return 2 if result.timed_out else 1
    except KeyboardInterrupt:
        renderer.close()
        print(f"{command_name}: interrupted", file=sys.stderr)
        return 130
    except (DiscoveryError, LeanRuntimeError, OSError, UnicodeError, ValueError) as exc:
        renderer.close()
        if arguments.json:
            print(
                json.dumps(
                    envelope(
                        "lean-runtime.execution/v1",
                        ok=False,
                        data={},
                        errors=[error("invocation_failed", str(exc))],
                    ),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(f"{command_name}: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    return run(parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
