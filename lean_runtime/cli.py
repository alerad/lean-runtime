"""Minimal command-line interface around the environment compiler."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .environments import ExecutionCapture
from .errors import LeanRuntimeError, MaterializationError, ResolutionError
from .lockfiles import EnvironmentLock
from .models import ExecutionResult
from .policies import ExecutionPolicy
from .runtime import Runtime
from .specs import EnvironmentSpec


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _emit_result(result: ExecutionResult, as_json: bool) -> None:
    if as_json:
        _json(result.to_dict())
        return
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr)
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
    root.add_argument("--home", help="runtime store root")
    commands = root.add_subparsers(dest="command", required=True)

    resolve = commands.add_parser("resolve", help="compile a TOML/JSON spec into a lock")
    resolve.add_argument("spec", type=Path)
    resolve.add_argument("--output", type=Path)
    resolve.add_argument("--timeout", type=float, default=900)

    ensure = commands.add_parser("ensure", help="build or reopen a locked environment")
    ensure.add_argument("lock", type=Path)
    ensure.add_argument("--name")

    check = commands.add_parser("check", help="check a file in a published environment")
    check.add_argument("environment")
    check.add_argument("file", type=Path, help="Lean source file, or - for stdin")
    check.add_argument("--json", action="store_true")
    _add_policy(check)

    inspect = commands.add_parser("inspect", help="inspect a published environment")
    inspect.add_argument("environment")

    replay = commands.add_parser("replay", help="replay a canonical execution capture")
    replay.add_argument("capture", type=Path)
    replay.add_argument("--json", action="store_true")

    gc = commands.add_parser("gc", help="collect old unreferenced environments")
    gc.add_argument("--execute", action="store_true", help="remove candidates; default is dry-run")
    gc.add_argument("--minimum-age-hours", type=float, default=24 * 30)

    raw = commands.add_parser("raw-check", help="check without a managed environment")
    raw.add_argument("file", type=Path, help="Lean source file, or - for stdin")
    raw.add_argument("--toolchain")
    raw.add_argument("--project", type=Path)
    raw.add_argument("--json", action="store_true")
    _add_policy(raw)

    build = commands.add_parser("project-build", help="build an existing Lake project")
    build.add_argument("project", type=Path)
    build.add_argument("targets", nargs="*")
    build.add_argument("--toolchain")
    build.add_argument("--timeout", type=float, default=900)
    build.add_argument("--json", action="store_true")

    install = commands.add_parser("install", help="install a Lean toolchain")
    install.add_argument("toolchain")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    runtime = Runtime(home=args.home)
    try:
        if args.command == "install":
            print(runtime.toolchains.ensure(args.toolchain))
            return 0
        if args.command == "resolve":
            lock = runtime.resolve(EnvironmentSpec.load(args.spec), timeout=args.timeout)
            if args.output:
                lock.write(args.output)
                print(lock.lock_id)
            else:
                _json(lock.to_dict())
            return 0
        if args.command == "ensure":
            environment = runtime.ensure(EnvironmentLock.load(args.lock), name=args.name)
            _json(environment.inspect().to_dict())
            return 0
        if args.command == "inspect":
            _json(runtime.open(args.environment).inspect().to_dict())
            return 0
        if args.command == "replay":
            capture = ExecutionCapture.load(args.capture)
            result = runtime.replay_capture(capture)
            _emit_result(result, args.json)
            if capture.expected_ok is not None and result.ok != capture.expected_ok:
                return 1
            return 0 if result.ok else 1
        if args.command == "gc":
            report = runtime.gc(
                dry_run=not args.execute,
                minimum_age_seconds=args.minimum_age_hours * 3600,
            )
            _json(report.to_dict())
            return 0
        if args.command == "check":
            source = sys.stdin.read() if str(args.file) == "-" else args.file.read_text()
            result = runtime.open(args.environment).check(source, policy=_policy(args))
        elif args.command == "raw-check":
            source = sys.stdin.read() if str(args.file) == "-" else args.file.read_text()
            result = runtime.check(
                source,
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
            )
    except (ResolutionError, MaterializationError) as exc:
        _json(
            {
                "error": str(exc),
                "phase": exc.phase,
                "command": list(exc.command),
                "exit_code": exc.exit_code,
                "output": exc.output,
            }
        )
        return 2
    except (LeanRuntimeError, OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        print(f"lean-runtime: {exc}", file=sys.stderr)
        return 2
    _emit_result(result, args.json)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
