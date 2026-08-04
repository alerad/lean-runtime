"""Reproducible managed-environment lifecycle case study.

This intentionally accepts an already prepared environment so cold preparation can be
measured separately from warm execution, export/import, and offline verification.
"""

from __future__ import annotations

import argparse
import json
import platform
import tempfile
import time
from pathlib import Path

from lean_runtime import Runtime


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("environment")
    parser.add_argument("--output", type=Path, default=Path("case-study-results.json"))
    arguments = parser.parse_args()

    runtime = Runtime()
    environment = runtime.open(arguments.environment)
    candidates = (
        "example : True := by trivial",
        "example : False := by trivial",
        "example : Nat := by exact 1",
    )
    started = time.monotonic()
    results = environment.check_many(candidates, concurrency=3)
    if any(item.provenance is None for item in results):
        raise RuntimeError("managed case-study execution returned incomplete provenance")
    warm_duration = time.monotonic() - started
    capture = environment.capture(candidates[0], expected_ok=True)

    with tempfile.TemporaryDirectory(prefix="lean-runtime-case-study-") as temporary:
        root = Path(temporary)
        bundle = root / "environment.oci.tar.gz"
        export_started = time.monotonic()
        bundle_info = runtime.export_environment(environment.id, bundle)
        export_duration = time.monotonic() - export_started

        fresh = Runtime(home=root / "fresh", prebuilt="never")
        import_started = time.monotonic()
        imported = fresh.import_environment(bundle, name="case-study")
        import_duration = time.monotonic() - import_started
        verify_started = time.monotonic()
        verification = fresh.verify("case-study", offline=True)
        verify_duration = time.monotonic() - verify_started
        replay = fresh.replay_capture(capture)

        payload = {
            "schema": "lean-runtime.case-study/v1",
            "machine": {
                "platform": platform.platform(),
                "python": platform.python_version(),
            },
            "identities": {
                "environment_id": environment.id,
                "lock_id": environment.lock.lock_id,
                "request_digests": [
                    item.provenance.request_digest
                    for item in results
                    if item.provenance is not None
                ],
                "execution_ids": [item.execution_id for item in results],
                "imported_environment_id": imported.id,
            },
            "warm_batch": {
                "concurrency": 3,
                "wall_ms": round(warm_duration * 1000),
                "accepted": sum(item.ok for item in results),
                "results": [item.to_dict() for item in results],
            },
            "bundle": {
                **bundle_info.to_dict(),
                "bytes": bundle.stat().st_size,
                "export_ms": round(export_duration * 1000),
                "import_ms": round(import_duration * 1000),
            },
            "offline": {
                "verification_ms": round(verify_duration * 1000),
                "verified": verification.ok,
                "replay_ok": replay.ok,
            },
        }
    arguments.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
