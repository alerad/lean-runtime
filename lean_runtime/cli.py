"""Minimal command-line interface around the environment compiler."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .environments import ExecutionCapture
from .errors import LeanRuntimeError, MaterializationError, ResolutionError
from .events import RuntimeEvent
from .lockfiles import EnvironmentLock
from .matrix import load_matrix
from .models import ExecutionResult, PhaseTiming
from .policies import ExecutionPolicy
from .runtime import Runtime
from .specs import EnvironmentSpec
from .timings import render_timings
from .wire import (
    envelope,
    error,
    serialize_diff_v1,
    serialize_execution_v1,
    serialize_matrix_v1,
    serialize_profile_v1,
    serialize_verify_v1,
)


def _schema_for(command: str) -> str:
    return {
        "verify": "lean-runtime.verify/v1",
        "diff": "lean-runtime.diff/v1",
        "profile": "lean-runtime.profile/v1",
        "matrix": "lean-runtime.matrix/v1",
        "gc": "lean-runtime.gc/v1",
        "inspect": "lean-runtime.inspect/v1",
    }.get(command, "lean-runtime.execution/v1")


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _progress(event: RuntimeEvent) -> None:
    package = f" [{event.data['package']}]" if "package" in event.data else ""
    print(f"lean-runtime: {event.kind}{package}: {event.message}", file=sys.stderr)


def _cli_source_name(path: Path) -> str:
    if path.is_absolute():
        return path.name
    return path.as_posix()


def _emit_result(result: ExecutionResult, as_json: bool) -> None:
    if as_json:
        _json(serialize_execution_v1(result))
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
    root.add_argument("--quiet", action="store_true", help="suppress progress events")
    root.add_argument("--verbose", action="store_true", help="show detailed decisions and checks")
    root.add_argument("--timings", action="store_true", help="show stable operation phase timings")
    root.add_argument(
        "--cache",
        action="append",
        dest="caches",
        help="OCI cache repository; repeatable (oci://registry/owner/repository)",
    )
    root.add_argument("--prebuilt", choices=("auto", "require", "never"), default=None)
    root.add_argument("--signatures", choices=("ignore", "require"), default="ignore")
    root.add_argument("--trusted-identity")
    root.add_argument("--trusted-issuer")
    root.add_argument("--cosign", default="cosign")
    commands = root.add_subparsers(dest="command", required=True)

    resolve = commands.add_parser("resolve", help="compile a TOML/JSON spec into a lock")
    resolve.add_argument("spec", type=Path)
    resolve.add_argument("--output", type=Path)
    resolve.add_argument("--timeout", type=float, default=900)

    ensure = commands.add_parser("ensure", help="build or reopen a locked environment")
    ensure.add_argument("lock", type=Path)
    ensure.add_argument("--name")

    pull = commands.add_parser("pull", help="require and import a prebuilt locked environment")
    pull.add_argument("lock", type=Path)
    pull.add_argument("--name")

    push = commands.add_parser(
        "build-and-push", help="ensure a lock and publish its prebuilt environment"
    )
    push.add_argument("lock", type=Path)
    push.add_argument("--push-to", required=True)
    push.add_argument("--tag", action="append", default=[])
    push.add_argument("--name")
    push.add_argument(
        "--platform-only",
        action="store_true",
        help="publish blobs and platform manifest without updating the lock index",
    )
    push.add_argument("--sign", action="store_true", help="sign the published lock index")
    push.add_argument(
        "--attest", action="store_true", help="publish a signed source/probe attestation"
    )

    publish_index = commands.add_parser(
        "publish-index", help="finalize a lock index from platform-result JSON files"
    )
    publish_index.add_argument("lock_id")
    publish_index.add_argument("platform_results", nargs="+", type=Path)
    publish_index.add_argument("--repository", required=True)
    publish_index.add_argument("--tag", action="append", default=[])

    export = commands.add_parser("export", help="export a deterministic OCI environment bundle")
    export.add_argument("environment")
    export.add_argument("--output", required=True, type=Path)

    import_bundle = commands.add_parser(
        "import", help="verify and import an OCI environment bundle"
    )
    import_bundle.add_argument("bundle", type=Path)
    import_bundle.add_argument("--name")
    import_bundle.add_argument("--no-probe", action="store_true", help="skip the Lean import probe")

    capsule_create = commands.add_parser(
        "capsule-create", help="create a thin content-addressed executable capsule"
    )
    capsule_create.add_argument("payload", type=Path)
    capsule_create.add_argument("--command", dest="capsule_command", nargs="+", required=True)
    capsule_create.add_argument("--source-revision", required=True)
    capsule_create.add_argument("--source-environment-id")
    capsule_create.add_argument("--source-lock-id")
    capsule_create.add_argument("--toolchain", default="unknown")
    capsule_create.add_argument("--capability-digest")

    capsule_export = commands.add_parser(
        "capsule-export", help="export a thin capsule as an OCI archive"
    )
    capsule_export.add_argument("capsule_id")
    capsule_export.add_argument("--output", required=True, type=Path)

    capsule_import = commands.add_parser(
        "capsule-import", help="verify and import a local capsule OCI archive"
    )
    capsule_import.add_argument("bundle", type=Path)

    capsule_pull = commands.add_parser(
        "capsule-pull", help="pull a compatible thin capsule from OCI"
    )
    capsule_pull.add_argument("repository")
    capsule_pull.add_argument("reference")
    capsule_pull.add_argument("--source-revision")

    capsule_push = commands.add_parser("capsule-push", help="publish a platform capsule to OCI")
    capsule_push.add_argument("capsule_id")
    capsule_push.add_argument("--repository", required=True)
    capsule_push.add_argument("--tag", action="append", default=[])
    capsule_push.add_argument("--sign", action="store_true")

    capsule_index = commands.add_parser(
        "capsule-publish-index", help="finalize a multi-platform capsule OCI index"
    )
    capsule_index.add_argument("source_revision")
    capsule_index.add_argument("platform_results", nargs="+", type=Path)
    capsule_index.add_argument("--repository", required=True)
    capsule_index.add_argument("--tag", action="append", default=[])

    check = commands.add_parser(
        "check", help="check with --with packages or in a published environment"
    )
    check.add_argument(
        "inputs",
        nargs="+",
        help="FILE with --with, otherwise ENVIRONMENT FILE; FILE may be - for stdin",
    )
    check.add_argument(
        "--with",
        dest="package_refs",
        action="append",
        default=[],
        metavar="REFERENCE",
        help="repeatable github:owner/repository@tag-or-commit package reference",
    )
    check.add_argument("--toolchain", help="override the toolchain discovered from --with packages")
    check.add_argument(
        "--include", action="append", default=[], type=Path, help="additional Lean source file"
    )
    check.add_argument("--json", action="store_true")
    _add_policy(check)

    inspect = commands.add_parser("inspect", help="inspect a published environment")
    inspect.add_argument("environment")
    inspect.add_argument("--packages", action="store_true", help="include exact package locks")
    inspect.add_argument("--explain", action="store_true", help="explain identity and reuse")

    commands.add_parser("env-list", help="list published environments")
    commands.add_parser("cache-status", help="show cache counts and disk usage")
    commands.add_parser("doctor", help="check local prerequisites and cache health")

    verify = commands.add_parser("verify", help="verify a lock or published environment")
    verify.add_argument("subject")
    verify.add_argument("--offline", action="store_true")
    verify.add_argument("--rebuild", action="store_true")
    verify.add_argument("--json", action="store_true")

    diff = commands.add_parser("diff", help="compare two exact Lean contexts")
    diff.add_argument("left")
    diff.add_argument("right")
    diff.add_argument("--json", action="store_true")

    profile = commands.add_parser("profile", help="measure repeated checks in one environment")
    profile.add_argument("environment")
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

    gc = commands.add_parser("gc", help="collect old unreferenced environments")
    gc.add_argument("--execute", action="store_true", help="remove candidates; default is dry-run")
    gc.add_argument("--minimum-age-hours", type=float, default=24 * 30)
    gc.add_argument(
        "--include-blobs", action="store_true", help="also collect unreferenced OCI blobs"
    )

    raw = commands.add_parser(
        "raw-check", help="check a file, discovering its local Lake project when possible"
    )
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
    operation_started = time.monotonic()
    runtime = Runtime(
        home=args.home,
        on_event=None if args.quiet else _progress,
        prebuilt=args.prebuilt,
        caches=args.caches,
        signatures=args.signatures,
        trusted_identity=args.trusted_identity,
        trusted_issuer=args.trusted_issuer,
        cosign=args.cosign,
    )
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
        if args.command == "ensure":
            environment = runtime.ensure(EnvironmentLock.load(args.lock), name=args.name)
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
        if args.command == "pull":
            runtime.prebuilt = "require"
            environment = runtime.ensure(EnvironmentLock.load(args.lock), name=args.name)
            _json(environment.inspect().to_dict())
            return 0
        if args.command == "build-and-push":
            environment = runtime.ensure(EnvironmentLock.load(args.lock), name=args.name)
            _json(
                runtime.publish_environment(
                    environment.id,
                    args.push_to,
                    tags=args.tag,
                    finalize=not args.platform_only,
                    sign=args.sign,
                    attest=args.attest,
                ).to_dict()
            )
            return 0
        if args.command == "publish-index":
            descriptors = []
            for path in args.platform_results:
                value = json.loads(path.read_text(encoding="utf-8"))
                descriptor = value.get("platform_descriptor") if isinstance(value, dict) else None
                if not isinstance(descriptor, dict):
                    raise ValueError(f"invalid platform result: {path}")
                descriptors.append(descriptor)
            digest = runtime.publish_environment_index(
                args.repository, args.lock_id, descriptors, tags=args.tag
            )
            _json({"lock_id": args.lock_id, "index_digest": digest})
            return 0
        if args.command == "export":
            _json(runtime.export_environment(args.environment, args.output).to_dict())
            return 0
        if args.command == "import":
            environment = runtime.import_environment(
                args.bundle, name=args.name, probe=not args.no_probe
            )
            _json(environment.inspect().to_dict())
            return 0
        if args.command == "capsule-create":
            capsule = runtime.create_capsule(
                args.payload,
                command=args.capsule_command,
                source_revision=args.source_revision,
                source_environment_id=args.source_environment_id,
                source_lock_id=args.source_lock_id,
                toolchain=args.toolchain,
                capability_digest=args.capability_digest,
            )
            _json(
                {
                    "capsule_id": capsule.id,
                    "manifest": capsule.manifest.to_dict(),
                    "path": str(capsule.root),
                }
            )
            return 0
        if args.command == "capsule-export":
            _json(runtime.export_capsule(args.capsule_id, args.output).to_dict())
            return 0
        if args.command == "capsule-import":
            capsule = runtime.import_capsule(args.bundle)
            _json({"capsule_id": capsule.id, "manifest": capsule.manifest.to_dict()})
            return 0
        if args.command == "capsule-pull":
            capsule = runtime.pull_capsule(
                args.repository,
                args.reference,
                expected_source_revision=args.source_revision,
            )
            _json({"capsule_id": capsule.id, "manifest": capsule.manifest.to_dict()})
            return 0
        if args.command == "capsule-push":
            _json(
                runtime.publish_capsule(
                    args.capsule_id,
                    args.repository,
                    tags=args.tag,
                    sign=args.sign,
                ).to_dict()
            )
            return 0
        if args.command == "capsule-publish-index":
            descriptors = []
            for path in args.platform_results:
                value = json.loads(path.read_text(encoding="utf-8"))
                descriptor = value.get("platform_descriptor") if isinstance(value, dict) else None
                if not isinstance(descriptor, dict):
                    raise ValueError(f"invalid capsule platform result: {path}")
                descriptors.append(descriptor)
            digest = runtime.publish_capsule_index(
                args.repository,
                args.source_revision,
                descriptors,
                tags=args.tag,
            )
            _json({"source_revision": args.source_revision, "index_digest": digest})
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
                environment = runtime.open(args.environment)
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
        if args.command == "env-list":
            _json(list(runtime.list_environments()))
            return 0
        if args.command == "cache-status":
            _json(runtime.store_status().to_dict())
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
        if args.command == "diff":
            difference = runtime.diff(args.left, args.right)
            if args.json:
                _json(serialize_diff_v1(difference))
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
        if args.command == "gc":
            gc_report = runtime.gc(
                dry_run=not args.execute,
                minimum_age_seconds=args.minimum_age_hours * 3600,
            )
            gc_payload: dict[str, Any] = {
                "environments": gc_report.to_dict(),
                "oci_blobs": None,
            }
            if args.include_blobs:
                gc_payload["oci_blobs"] = runtime.gc_oci_blobs(
                    dry_run=not args.execute,
                    minimum_age_seconds=args.minimum_age_hours * 3600,
                ).to_dict()
            _json(
                envelope(
                    "lean-runtime.gc/v1",
                    ok=True,
                    data=gc_payload,
                )
            )
            return 0
        if args.command == "check":
            if args.package_refs:
                if len(args.inputs) != 1:
                    raise ValueError("check with --with expects exactly one FILE")
                environment = runtime.ensure_references(args.package_refs, toolchain=args.toolchain)
                source_file = Path(args.inputs[0])
            else:
                if len(args.inputs) != 2:
                    raise ValueError("check expects ENVIRONMENT FILE, or FILE with --with")
                if args.toolchain:
                    raise ValueError("check --toolchain is only valid with --with")
                environment = runtime.open(args.inputs[0])
                source_file = Path(args.inputs[1])
            if str(source_file) == "-":
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
        elif args.command == "raw-check":
            if str(args.file) == "-":
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
            )
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
            print(f"lean-runtime: {exc}", file=sys.stderr)
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
    _emit_result(result, args.json)
    if args.timings:
        print(render_timings(result.timings), file=sys.stderr)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
