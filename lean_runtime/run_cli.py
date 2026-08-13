"""Zero-configuration command for checking one Lean file."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from .console import ConsoleRenderer
from .discovery import (
    Catalog,
    Discovery,
    DiscoveryError,
    DiscoveryPolicy,
    DiscoveryResult,
    default_catalog,
)
from .errors import LeanRuntimeError, ProjectError, SpecificationError
from .events import RuntimeEvent
from .frontmatter import LeanFrontmatter, parse_frontmatter
from .lockfiles import EnvironmentLock
from .models import ExecutionResult, PhaseTiming
from .policies import ExecutionPolicy, format_byte_size, parse_byte_size
from .projects import discover_project
from .runtime import Runtime
from .timings import render_timings
from .wire import envelope, error, serialize_execution_v1


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="lean-run", description="Check one Lean file")
    root.add_argument("file", type=Path)
    root.add_argument("--with", dest="requires", action="append", default=[])
    root.add_argument("--lock", type=Path, help="use an exact environment lock")
    root.add_argument("--lock-out", type=Path, help="write the resolved exact lock")
    root.add_argument("--toolchain", help="Lean version for a core-only file")
    root.add_argument("--catalog", type=Path, help="override the bundled discovery catalog")
    root.add_argument(
        "--no-discover",
        action="store_true",
        help="require explicit context or a pinned Lake project",
    )
    root.add_argument(
        "--offline",
        action="store_true",
        help="use retained local environments only",
    )
    root.add_argument(
        "--no-source-build",
        action="store_true",
        help="allow verified downloads but do not build missing environments",
    )
    root.add_argument("--max-candidates", type=int, default=3)
    root.add_argument(
        "--search-timeout",
        "--discovery-timeout",
        dest="search_timeout",
        type=float,
        default=90.0,
        help="budget for ranking and compiler probes (--discovery-timeout is a deprecated alias)",
    )
    root.add_argument(
        "--acquire-timeout",
        type=float,
        default=1800.0,
        help="budget for downloading, installing, or building one candidate environment",
    )
    root.add_argument("--home", help="runtime store root")
    root.add_argument("--json", action="store_true")
    root.add_argument(
        "--json-events",
        action="store_true",
        help="stream runtime lifecycle events to stderr as JSON lines",
    )
    root.add_argument("--quiet", action="store_true")
    root.add_argument(
        "--verbose", action="store_true", help="show every runtime event while preparing"
    )
    root.add_argument(
        "--max-download",
        type=parse_byte_size,
        metavar="SIZE",
        help="fail instead of downloading more than SIZE (e.g. 500MiB)",
    )
    root.add_argument(
        "--plan",
        action="store_true",
        help="report the acquisition cost without downloading or checking",
    )
    root.add_argument(
        "--explain", action="store_true", help="explain context selection without running"
    )
    root.add_argument(
        "--timings", action="store_true", help="show preparation and execution timings"
    )
    root.add_argument(
        "--check-timeout",
        "--timeout",
        dest="check_timeout",
        type=float,
        default=300,
        help="budget for one Lean invocation (--timeout is a deprecated alias)",
    )
    return root


def _catalog(path: Path | None) -> Catalog:
    return Catalog.from_file(path.expanduser().resolve()) if path is not None else default_catalog()


def _discovery_policy(arguments: argparse.Namespace) -> DiscoveryPolicy:
    return DiscoveryPolicy(
        max_candidates=arguments.max_candidates,
        max_total_seconds=arguments.search_timeout,
        allow_download=not arguments.offline,
        allow_source_build=not arguments.offline and not arguments.no_source_build,
        candidate_timeout_seconds=arguments.check_timeout,
        acquisition_timeout_seconds=arguments.acquire_timeout,
    )


def _discovery_failure(result: DiscoveryResult) -> str:
    details = [item.detail for item in result.diagnostics]
    if not details:
        details = [item.diagnostics[0].detail for item in result.attempts if item.diagnostics]
    suffix = f" ({len(result.attempts)} candidate(s) tested)" if result.attempts else ""
    return (details[0] if details else "no compatible environment was found") + suffix


def _explain_discovery(arguments: argparse.Namespace, source: str) -> dict[str, object]:
    plan = Discovery(
        catalog=_catalog(arguments.catalog),
        policy=_discovery_policy(arguments),
    ).plan(source)
    return {
        "decision": "automatic_discovery",
        "context": "catalog candidates",
        "subject": [candidate.entry.id for candidate in plan.candidates],
        "plan": plan.to_dict(),
    }


def _combine(arguments: argparse.Namespace, metadata: LeanFrontmatter | None) -> LeanFrontmatter:
    embedded = metadata or LeanFrontmatter()
    if arguments.requires and embedded.requires:
        raise SpecificationError("cannot combine --with and frontmatter 'requires'")
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
    symbol = "✓" if result.ok else "✗"
    status = "accepted" if result.ok else "rejected"
    print(f"{symbol} {shown} {status} in {result.elapsed_seconds:.2f}s")
    if show_timings:
        print(render_timings(result.timings))


def _plan_lock(
    arguments: argparse.Namespace,
    context: LeanFrontmatter,
    source: str,
    source_path: Path,
) -> tuple[EnvironmentLock, str | None]:
    """Select the exact lock whose acquisition cost --plan should report."""
    if context.requires or context.toolchain is not None:
        raise SpecificationError(
            "--plan supports an exact lock or automatic discovery; explicit dependencies "
            "need Lake resolution first (run once with --lock-out, then plan the lock)"
        )
    if context.lock is not None:
        path = _lock_path(context.lock, source_path, embedded=arguments.lock is None)
        return EnvironmentLock.load(path), None
    try:
        project = discover_project(source_path)
    except ProjectError:
        plan = Discovery(
            catalog=_catalog(arguments.catalog),
            policy=_discovery_policy(arguments),
        ).plan(source)
        if not plan.candidates:
            raise SpecificationError(
                "no plausible catalog candidate for this file; nothing to plan"
            ) from None
        candidate = plan.candidates[0]
        return candidate.entry.lock, candidate.entry.id
    raise SpecificationError(f"--plan is not supported inside a Lake project: {project.root}")


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
    if report["environment_ready"]:
        print("Download required: none")
    elif isinstance(download, int):
        selected = next(item for item in libraries if item.get("available"))
        cached = selected.get("cached_bytes", 0)
        suffix = (
            f" ({format_byte_size(cached)} already cached)"
            if isinstance(cached, int) and cached > 0
            else ""
        )
        print(f"Download required: {format_byte_size(download)}{suffix}")
        print(f"Library: {selected['library']}")
    else:
        detail = ""
        if isinstance(libraries, list) and libraries:
            detail = f" ({libraries[0].get('error', 'no library responded')})"
        print(f"Download required: unknown{detail}")
    limit = report.get("max_download_bytes")
    if isinstance(limit, int):
        allowed = not isinstance(download, int) or download <= limit
        print(f"Download limit: {format_byte_size(limit)} ({'ok' if allowed else 'exceeded'})")


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    source_path = arguments.file.expanduser().resolve()
    renderer = ConsoleRenderer(
        mode="quiet" if arguments.quiet or arguments.json else None,
        verbose=arguments.verbose,
    )
    try:
        source = source_path.read_text(encoding="utf-8")
        embedded = parse_frontmatter(source)
        context = _combine(arguments, embedded)
        if arguments.explain:
            if context.lock is not None:
                selected = "exact lock"
                detail = context.lock
                explanation: dict[str, object] = {
                    "decision": "context_selected",
                    "context": selected,
                    "subject": detail,
                }
            elif context.requires:
                selected = "standalone dependencies"
                detail = ", ".join(context.requires)
                explanation = {
                    "decision": "context_selected",
                    "context": selected,
                    "subject": detail,
                }
            elif context.toolchain is not None:
                selected = "standalone toolchain"
                detail = context.toolchain
                explanation = {
                    "decision": "context_selected",
                    "context": selected,
                    "subject": detail,
                }
            else:
                try:
                    project = discover_project(source_path)
                except ProjectError:
                    if arguments.no_discover:
                        raise SpecificationError(
                            "the file has no explicit context or pinned Lake project"
                        ) from None
                    explanation = _explain_discovery(arguments, source)
                    selected = "automatic discovery"
                    candidates = explanation["subject"]
                    if isinstance(candidates, list):
                        detail = ", ".join(candidates)
                    else:
                        detail = str(candidates)
                else:
                    selected = "local project"
                    detail = str(project.root)
                    explanation = {
                        "decision": "context_selected",
                        "context": selected,
                        "subject": detail,
                    }
            if arguments.json:
                print(
                    json.dumps(
                        {
                            "schema": "lean-runtime.inspect/v1",
                            "ok": True,
                            "data": explanation,
                            "warnings": [],
                            "errors": [],
                        },
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                print(f"Context: {selected}\nSelected: {detail}")
            return 0
        if arguments.plan:
            lock, candidate_id = _plan_lock(arguments, context, source, source_path)
            runtime = Runtime(
                home=arguments.home,
                max_download_bytes=arguments.max_download,
                libraries=() if arguments.offline else None,
            )
            report = runtime.plan_exact(lock)
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

        availability = (
            "local" if arguments.offline else ("required" if arguments.no_source_build else "auto")
        )
        runtime = Runtime(
            home=arguments.home,
            on_event=observe,
            availability=availability,
            libraries=() if arguments.offline else None,
            max_download_bytes=arguments.max_download,
        )
        policy = ExecutionPolicy(timeout_seconds=arguments.check_timeout)
        if context.lock is not None:
            if arguments.lock_out is not None:
                raise SpecificationError("--lock-out cannot be combined with an exact lock")
            lock_path = _lock_path(
                context.lock,
                source_path,
                embedded=arguments.lock is None,
            )
            environment = runtime.open_exact(EnvironmentLock.load(lock_path))
            preparation = PhaseTiming(
                "environment_open", round((time.monotonic() - preparation_started) * 1000)
            )
            result = environment.check(source, filename=source_path.name, policy=policy)
        elif context.requires:
            resolution_started = time.monotonic()
            lock = runtime.prepare_references(context.requires, toolchain=context.toolchain)
            resolution = PhaseTiming(
                "resolution", round((time.monotonic() - resolution_started) * 1000)
            )
            if arguments.lock_out is not None:
                lock.write(arguments.lock_out)
            environment = runtime.open_exact(lock)
            preparation = PhaseTiming(
                "environment_open",
                round((time.monotonic() - resolution_started) * 1000) - resolution.duration_ms,
            )
            result = environment.check(source, filename=source_path.name, policy=policy)
            result = replace(result, timings=(resolution, preparation, *result.timings))
        elif context.toolchain is not None:
            preparation = PhaseTiming(
                "toolchain", round((time.monotonic() - preparation_started) * 1000)
            )
            result = runtime.check_file(source_path, toolchain=context.toolchain, policy=policy)
        else:
            try:
                preparation = PhaseTiming(
                    "environment_open", round((time.monotonic() - preparation_started) * 1000)
                )
                result = runtime.check_file(source_path, policy=policy)
                if arguments.lock_out is not None:
                    raise SpecificationError(
                        "--lock-out is only available for explicit dependencies or discovery"
                    )
            except ProjectError:
                if arguments.no_discover:
                    raise SpecificationError(
                        "the file has no execution context; add frontmatter, pass --with or "
                        "--toolchain, provide --lock, or place it in a pinned Lake project"
                    ) from None
                renderer.note("Discovering an exact environment")
                discovery_started = time.monotonic()
                discovered = Discovery(
                    catalog=_catalog(arguments.catalog),
                    policy=_discovery_policy(arguments),
                    runtime=runtime,
                    runtime_events=runtime_events,
                ).discover_and_check(source)
                if discovered.status != "found" or discovered.execution_result is None:
                    rejection = discovered.rejection_attempt
                    if rejection is None or rejection.execution_result is None:
                        raise SpecificationError(_discovery_failure(discovered)) from None
                    result = rejection.execution_result
                else:
                    if arguments.lock_out is not None:
                        assert discovered.lock is not None
                        discovered.lock.write(arguments.lock_out)
                    result = discovered.execution_result
                preparation = PhaseTiming(
                    "discovery", round((time.monotonic() - discovery_started) * 1000)
                )
        if context.lock is not None or context.toolchain is not None or not context.requires:
            result = replace(result, timings=(preparation, *result.timings))
        renderer.close()
        _emit(
            result,
            as_json=arguments.json,
            filename=source_path.name,
            display_path=str(arguments.file),
            show_timings=arguments.timings,
        )
        return 0 if result.ok else 1
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
            print(f"lean-run: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
