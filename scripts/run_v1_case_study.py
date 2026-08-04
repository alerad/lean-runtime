"""Reproducible release case study over one already-prepared environment."""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any

from lean_runtime import Runtime

DEFAULT_CANDIDATES = Path(__file__).parents[1] / "benchmarks" / "proof_batch" / "candidates.json"


def _percentile(values: list[int], percentile: float) -> int | None:
    if len(values) < 5:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(percentile * len(ordered)))]


def _summary(durations: list[int]) -> dict[str, int | float | None]:
    return {
        "min_ms": min(durations),
        "median_ms": statistics.median(durations),
        "mean_ms": statistics.fmean(durations),
        "p95_ms": _percentile(durations, 0.95),
        "max_ms": max(durations),
    }


def _load_candidates(path: Path) -> tuple[str, ...]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ValueError("candidate fixture must be a non-empty JSON array of Lean source strings")
    return tuple(value)


def _cache_state(runtime: Runtime) -> dict[str, Any]:
    return runtime.store_status().to_dict()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("environment")
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--concurrency", type=int, action="append", default=[])
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--output", type=Path, default=Path("case-study-results.json"))
    arguments = parser.parse_args()
    concurrency_levels = tuple(arguments.concurrency or (1, 4, 8, 20))
    if arguments.repeat < 1 or arguments.repeat > 100:
        parser.error("--repeat must be between 1 and 100")
    if any(value < 1 or value > 32 for value in concurrency_levels):
        parser.error("--concurrency must be between 1 and 32")

    candidates = _load_candidates(arguments.candidates)
    runtime = Runtime()
    cache_before = _cache_state(runtime)
    environment = runtime.open(arguments.environment)
    batches: list[dict[str, Any]] = []
    all_results = []
    for concurrency in concurrency_levels:
        samples: list[int] = []
        accepted: list[int] = []
        for _ in range(arguments.repeat):
            started = time.monotonic()
            results = environment.check_many(candidates, concurrency=concurrency)
            samples.append(round((time.monotonic() - started) * 1000))
            accepted.append(sum(item.ok for item in results))
            all_results.extend(results)
        batches.append(
            {
                "concurrency": concurrency,
                "repeat": arguments.repeat,
                "wall_samples_ms": samples,
                "statistics": _summary(samples),
                "accepted_per_sample": accepted,
            }
        )
    if any(item.provenance is None for item in all_results):
        raise RuntimeError("managed case-study execution returned incomplete provenance")

    repeated = environment.check(candidates[0])
    repeated_again = environment.check(candidates[0])
    capture = environment.capture(candidates[0], expected_ok=True)
    with tempfile.TemporaryDirectory(prefix="lean-runtime-case-study-") as temporary:
        root = Path(temporary)
        bundle = root / "environment.oci.tar.gz"
        export_started = time.monotonic()
        bundle_info = runtime.export_environment(environment.id, bundle)
        export_ms = round((time.monotonic() - export_started) * 1000)

        fresh = Runtime(home=root / "fresh", prebuilt="never")
        import_started = time.monotonic()
        imported = fresh.import_environment(bundle, name="case-study")
        import_ms = round((time.monotonic() - import_started) * 1000)
        verify_started = time.monotonic()
        verification = fresh.verify("case-study", offline=True)
        verification_ms = round((time.monotonic() - verify_started) * 1000)
        replay = fresh.replay_capture(capture)

        payload = {
            "schema": "lean-runtime.case-study/v1",
            "runtime_version": version("lean-runtime"),
            "command": [sys.executable, *sys.argv],
            "machine": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "processor": platform.processor(),
                "filesystem_root": os.statvfs(arguments.output.parent.resolve()).f_fsid,
            },
            "fixture": {
                "path": str(arguments.candidates),
                "candidates": len(candidates),
            },
            "cache_state": {"before": cache_before, "after": _cache_state(runtime)},
            "identities": {
                "environment_id": environment.id,
                "lock_id": environment.lock.lock_id,
                "imported_environment_id": imported.id,
                "repeated_request_digest_equal": (
                    repeated.provenance is not None
                    and repeated_again.provenance is not None
                    and repeated.provenance.request_digest
                    == repeated_again.provenance.request_digest
                ),
                "repeated_execution_ids_unique": (
                    repeated.execution_id != repeated_again.execution_id
                ),
            },
            "warm_batches": batches,
            "bundle": {
                **bundle_info.to_dict(),
                "bytes": bundle.stat().st_size,
                "export_ms": export_ms,
                "import_ms": import_ms,
            },
            "offline": {
                "verification_ms": verification_ms,
                "verified": verification.ok,
                "replay_ok": replay.ok,
            },
        }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
