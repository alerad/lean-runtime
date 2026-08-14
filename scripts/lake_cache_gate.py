"""Cross-platform correctness and performance gate for root-only Lake caching."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def run(home: Path, *arguments: str) -> float:
    started = time.monotonic()
    process = subprocess.run(
        [sys.executable, "-m", "lean_runtime", "--home", str(home), *arguments],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    elapsed = time.monotonic() - started
    if process.returncode:
        raise SystemExit(process.stdout)
    return elapsed


def main() -> int:
    first_budget = float(os.environ.get("LEAN_RUNTIME_CACHE_FIRST_BUDGET", "10"))
    warm_budget = float(os.environ.get("LEAN_RUNTIME_CACHE_WARM_BUDGET", "3"))
    home = Path(
        os.environ.get("LEAN_RUNTIME_CACHE_GATE_HOME", Path.home() / ".cache" / "lean-runtime")
    ).expanduser()
    with tempfile.TemporaryDirectory(prefix="lean-runtime-cache-gate-") as raw:
        project = Path(raw) / "project"
        run(
            home,
            "init",
            str(project),
            "--name",
            "CacheGate",
            "--core",
            "--toolchain",
            "4.33.0",
            "--no-agents",
        )
        first = run(home, "check", "--project", str(project))
        warm = run(home, "check", "--project", str(project))
        build = project / ".lake" / "build"
        shutil.rmtree(build)
        restored = run(home, "check", "--project", str(project))
        cache_files = sum(1 for path in (home / "lake-artifacts").rglob("*") if path.is_file())
        restored_files = sum(1 for path in build.rglob("*") if path.is_file())

    report = {
        "first_seconds": round(first, 3),
        "warm_seconds": round(warm, 3),
        "restored_seconds": round(restored, 3),
        "cache_files": cache_files,
        "restored_files": restored_files,
    }
    print(json.dumps(report, sort_keys=True))
    if cache_files == 0 or restored_files == 0:
        raise SystemExit("Lake did not persist and restore root artifacts")
    if first > first_budget:
        raise SystemExit(f"first root check exceeded {first_budget:g}s: {first:.3f}s")
    if warm > warm_budget or restored > warm_budget:
        raise SystemExit(
            f"warm/restore check exceeded {warm_budget:g}s: {warm:.3f}s/{restored:.3f}s"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
