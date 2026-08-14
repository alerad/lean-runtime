"""Resolve, build, and import-check a Lean Runtime compatibility profile."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from lean_runtime import EnvironmentError, EnvironmentSpec, Runtime, RuntimeEvent


def progress(event: RuntimeEvent) -> None:
    print(f"[{event.kind}] {event.message}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("profile", type=Path)
    parser.add_argument("--home")
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args()

    profile_path = args.profile.resolve()
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    spec = EnvironmentSpec.load(profile_path.with_name(str(profile["spec"])))
    runtime = Runtime(home=args.home, on_event=progress)
    try:
        environment = runtime.environment(str(profile["name"]))
        lock = environment.lock
        print(f"[environment.cache_hit] Reusing {environment.id}", flush=True)
    except EnvironmentError:
        lock = runtime.prepare(spec)
        environment = runtime.open_exact(lock, name=str(profile["name"]))
    modules = tuple(str(module) for module in profile["imports"])
    sources = tuple(f"import {module}\nexample : True := by trivial\n" for module in modules)
    results = environment.check_many(sources, concurrency=args.concurrency)
    summary = {
        "environment_id": environment.id,
        "lock_id": lock.lock_id,
        "packages": len(lock.packages),
        "imports": [
            {"module": module, "ok": result.ok, "diagnostics": len(result.diagnostics)}
            for module, result in zip(modules, results, strict=True)
        ],
        "ok": all(result.ok for result in results),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
