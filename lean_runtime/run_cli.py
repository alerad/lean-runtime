"""Zero-configuration command for checking one Lean file."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import LeanRuntimeError, ProjectError, SpecificationError
from .events import RuntimeEvent
from .frontmatter import LeanFrontmatter, parse_frontmatter
from .lockfiles import EnvironmentLock
from .models import ExecutionResult
from .policies import ExecutionPolicy
from .runtime import Runtime


def _progress(event: RuntimeEvent) -> None:
    messages = {
        "package_reference.started": "Resolving dependency",
        "prebuilt.lookup": "Looking for a cached environment",
        "prebuilt.layer_download_started": "Downloading cached environment",
        "environment.build_started": "Building environment",
        "environment.cache_hit": "Using local environment",
    }
    message = messages.get(event.kind)
    if message:
        package = f" {event.data['reference']}" if "reference" in event.data else ""
        print(f"lean-run: {message}{package}", file=sys.stderr)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="lean-run", description="Check one Lean file")
    root.add_argument("file", type=Path)
    root.add_argument("--with", dest="requires", action="append", default=[])
    root.add_argument("--lock", type=Path, help="use an exact environment lock")
    root.add_argument("--lock-out", type=Path, help="write the resolved exact lock")
    root.add_argument("--toolchain", help="Lean version for a core-only file")
    root.add_argument("--home", help="runtime store root")
    root.add_argument("--json", action="store_true")
    root.add_argument("--quiet", action="store_true")
    root.add_argument("--timeout", type=float, default=120)
    return root


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


def _emit(result: ExecutionResult, *, as_json: bool, filename: str) -> None:
    if as_json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr)
    symbol = "✓" if result.ok else "✗"
    status = "accepted" if result.ok else "rejected"
    print(f"{symbol} {filename} {status} in {result.elapsed_seconds:.2f}s")


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    source_path = arguments.file.expanduser().resolve()
    try:
        source = source_path.read_text(encoding="utf-8")
        embedded = parse_frontmatter(source)
        context = _combine(arguments, embedded)
        runtime = Runtime(
            home=arguments.home,
            on_event=None if arguments.quiet or arguments.json else _progress,
        )
        policy = ExecutionPolicy(timeout_seconds=arguments.timeout)
        if context.lock is not None:
            if arguments.lock_out is not None:
                raise SpecificationError("--lock-out cannot be combined with an exact lock")
            lock_path = _lock_path(
                context.lock,
                source_path,
                embedded=arguments.lock is None,
            )
            environment = runtime.ensure(EnvironmentLock.load(lock_path))
            result = environment.check(source, filename=source_path.name, policy=policy)
        elif context.requires:
            lock = runtime.resolve_references(context.requires, toolchain=context.toolchain)
            if arguments.lock_out is not None:
                lock.write(arguments.lock_out)
            environment = runtime.ensure(lock)
            result = environment.check(source, filename=source_path.name, policy=policy)
        elif arguments.lock_out is not None:
            raise SpecificationError(
                "--lock-out requires dependencies declared with --with or frontmatter"
            )
        elif context.toolchain is not None:
            result = runtime.check_file(source_path, toolchain=context.toolchain, policy=policy)
        else:
            try:
                result = runtime.check_file(source_path, policy=policy)
            except ProjectError as exc:
                raise SpecificationError(
                    "the file has no execution context; add frontmatter, pass --with or "
                    "--toolchain, provide --lock, or place it in a pinned Lake project"
                ) from exc
        _emit(result, as_json=arguments.json, filename=source_path.name)
        return 0 if result.ok else 1
    except (LeanRuntimeError, OSError, UnicodeError, ValueError) as exc:
        if arguments.json:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"lean-run: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
