#!/usr/bin/env python3
"""Fail-closed announcement gate: the advertised user journeys, from clean state.

Runs the exact journeys a new user follows after ``pip install lean-runtime``
against an empty runtime home, with hard wall-clock timeouts, and writes one
JSON report. The critical property is fail-closed distribution: the first cold
check runs with ``--no-source-build``, so a broken or private registry fails
in seconds instead of degrading into a silent source build.

Steps:
1. cold check of acceptance/Main.lean with --no-source-build and --lock-out
2. warm re-check (must be fast)
3. acceptance/Fail.lean must be rejected with a diagnostic naming Fail.lean
4. re-check via the emitted lock (reproducibility)
5. the documented Python batch API against the same lock

Run it on a machine WITHOUT registry credentials; the point is proving the
anonymous path. The runtime home is created fresh; pass --keep-home to retain
it for inspection.

Usage:
    python scripts/announcement_gate.py --report gate-report.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ACCEPTANCE = Path(__file__).resolve().parent.parent / "acceptance"

COLD_TIMEOUT = 2700  # toolchain download + environment download, no builds
WARM_TIMEOUT = 600
WARM_LIMIT = 400  # hang detector, not a perf bar: GitHub macOS runners take ~4x
# longer than real hardware for the warm Lake trace scan (238s observed vs 38s
# on an M-series laptop)


def run(
    command: list[str],
    *,
    timeout: float,
    env: dict[str, str],
) -> tuple[int, str, float]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
        output = completed.stdout + completed.stderr
        return completed.returncode, output, time.monotonic() - started
    except subprocess.TimeoutExpired as error:
        output = (error.stdout or "") + (error.stderr or "")
        if isinstance(output, bytes):  # pragma: no cover - platform dependent
            output = output.decode(errors="replace")
        return -1, f"TIMEOUT after {timeout}s\n{output}", time.monotonic() - started


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", default=None, help="runtime home (default: fresh temp dir)")
    parser.add_argument("--report", default="announcement-gate-report.json")
    parser.add_argument("--keep-home", action="store_true")
    parser.add_argument("--cold-timeout", type=float, default=COLD_TIMEOUT)
    arguments = parser.parse_args()

    import os
    import tempfile

    home = Path(arguments.home) if arguments.home else Path(tempfile.mkdtemp(prefix="gate-home-"))
    home.mkdir(parents=True, exist_ok=True)
    if any(home.iterdir()):
        print(f"refusing to run: home is not empty: {home}")
        return 2
    env = {**os.environ, "LEAN_RUNTIME_HOME": str(home)}
    lock_path = home / "gate.lock.json"
    main_lean = str(ACCEPTANCE / "Main.lean")
    fail_lean = str(ACCEPTANCE / "Fail.lean")

    report: dict[str, object] = {"home": str(home), "steps": [], "ok": False}
    steps: list[dict[str, object]] = report["steps"]  # type: ignore[assignment]

    def step(
        name: str,
        command: list[str],
        *,
        timeout: float,
        expect_exit: int = 0,
        require: str | None = None,
        forbid: str | None = None,
        limit: float | None = None,
    ) -> bool:
        code, output, elapsed = run(command, timeout=timeout, env=env)
        ok = code == expect_exit
        detail = ""
        if ok and require is not None and require not in output:
            ok, detail = False, f"missing expected text: {require!r}"
        if ok and forbid is not None and forbid in output:
            ok, detail = False, f"contains forbidden text: {forbid!r}"
        if ok and limit is not None and elapsed > limit:
            ok, detail = False, f"took {elapsed:.1f}s, limit {limit}s"
        steps.append(
            {
                "name": name,
                "command": command,
                "exit_code": code,
                "elapsed_seconds": round(elapsed, 2),
                "ok": ok,
                "detail": detail or None,
                "tail": output[-2000:],
            }
        )
        suffix = f": {detail}" if detail else ""
        print(f"{'ok  ' if ok else 'FAIL'} {name} ({elapsed:.1f}s){suffix}")
        return ok

    version = subprocess.run(
        [
            sys.executable,
            "-c",
            "import lean_runtime, importlib.metadata as m; print(m.version('lean-runtime'))",
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    report["lean_runtime_version"] = version
    print(f"gate: lean-runtime {version}, home {home}")

    ok = step(
        "cold-no-source-build",
        [
            "lean-run",
            main_lean,
            "--no-source-build",
            "--lock-out",
            str(lock_path),
        ],
        timeout=arguments.cold_timeout,
    )
    ok = ok and step(
        "warm",
        ["lean-run", main_lean],
        timeout=WARM_TIMEOUT,
        limit=WARM_LIMIT,
    )
    ok = ok and step(
        "failing-proof-diagnostics",
        ["lean-run", fail_lean, "--lock", str(lock_path)],
        timeout=WARM_TIMEOUT,
        expect_exit=1,
        require="Fail.lean",
        forbid="instance-",
    )
    ok = ok and step(
        "lock-reproducibility",
        ["lean-run", main_lean, "--lock", str(lock_path)],
        timeout=WARM_TIMEOUT,
        limit=WARM_LIMIT,
    )
    ok = ok and step(
        "python-batch-api",
        [sys.executable, str(ACCEPTANCE / "python_api.py"), str(lock_path)],
        timeout=900,
    )

    usage = shutil.disk_usage(home)
    report["ok"] = ok
    report["home_bytes"] = sum(f.stat().st_size for f in home.rglob("*") if f.is_file())
    report["disk_free_bytes"] = usage.free
    Path(arguments.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"report: {arguments.report}")
    if not arguments.keep_home and arguments.home is None:
        shutil.rmtree(home, ignore_errors=True)
    print("GATE PASSED" if ok else "GATE FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
