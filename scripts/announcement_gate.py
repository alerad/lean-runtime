#!/usr/bin/env python3
"""Fail-closed announcement gate: the advertised user journeys, from clean state.

Runs the exact journeys a new user follows after ``pip install lean-runtime``
against an empty runtime home, with hard wall-clock timeouts, and writes one
JSON report. The critical property is fail-closed distribution: the first cold
check runs with ``--no-source-build``, so a broken or private registry fails
in seconds instead of degrading into a silent source build.

Steps:
1. side-effect-free narrow-import plan with byte budgets
2. cold narrow check with --no-source-build and --lock-out
3. incremental full-Mathlib plan and check
4. warm re-check and clean failing diagnostics
5. lock replay and the documented Python batch API

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
WARM_LIMIT = 300
NARROW_ENVIRONMENT_LIMIT = 100 * 1024**2
NARROW_TOTAL_LIMIT = 900 * 1024**2
FULL_INCREMENTAL_LIMIT = 2 * 1024**3
HOME_LIMIT = 9 * 1024**3


def plan_data(value: object) -> dict[str, object]:
    """Return data from a successful, versioned plan envelope."""
    if not isinstance(value, dict):
        return {}
    data = value.get("data")
    if (
        value.get("schema") != "lean-runtime.plan/v1"
        or value.get("ok") is not True
        or not isinstance(data, dict)
    ):
        return {}
    return data


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
    narrow_lean = str(ACCEPTANCE / "Narrow.lean")
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

    def plan(name: str, source: str) -> tuple[bool, dict[str, object]]:
        code, output, elapsed = run(
            ["lean-runtime", "check", source, "--plan", "--json"],
            timeout=WARM_TIMEOUT,
            env=env,
        )
        try:
            value = json.loads(output)
        except json.JSONDecodeError:
            value = {}
        data = plan_data(value)
        ok = code == 0 and data.get("download_bytes_complete") is True
        steps.append(
            {
                "name": name,
                "command": ["lean-runtime", "check", source, "--plan", "--json"],
                "exit_code": code,
                "elapsed_seconds": round(elapsed, 2),
                "ok": ok,
                "detail": None if ok else "plan was unavailable or incomplete",
                "plan": value,
                "tail": output[-2000:],
            }
        )
        print(f"{'ok  ' if ok else 'FAIL'} {name} ({elapsed:.1f}s)")
        return ok, data

    ok, narrow_plan = plan("narrow-plan", narrow_lean)
    narrow_environment = narrow_plan.get("environment_download_bytes")
    narrow_total = narrow_plan.get("download_bytes")
    budgets_ok = (
        isinstance(narrow_environment, int)
        and narrow_environment <= NARROW_ENVIRONMENT_LIMIT
        and isinstance(narrow_total, int)
        and narrow_total <= NARROW_TOTAL_LIMIT
        and not any(home.joinpath("cas", "artifacts", "sha256").iterdir())
    )
    steps.append(
        {
            "name": "narrow-byte-budgets",
            "ok": budgets_ok,
            "environment_limit": NARROW_ENVIRONMENT_LIMIT,
            "total_limit": NARROW_TOTAL_LIMIT,
            "environment_download_bytes": narrow_environment,
            "download_bytes": narrow_total,
        }
    )
    ok = ok and budgets_ok

    cold_ok = step(
        "cold-narrow-no-source-build",
        [
            "lean-runtime",
            "check",
            narrow_lean,
            "--no-source-build",
            "--lock-out",
            str(lock_path),
            "--max-download",
            str(NARROW_TOTAL_LIMIT),
        ],
        timeout=arguments.cold_timeout,
    )
    ok = cold_ok and ok
    full_plan_ok, full_plan = plan("full-incremental-plan", main_lean)
    full_incremental = full_plan.get("download_bytes")
    full_budget_ok = (
        full_plan_ok
        and isinstance(full_incremental, int)
        and full_incremental <= FULL_INCREMENTAL_LIMIT
    )
    steps.append(
        {
            "name": "full-incremental-byte-budget",
            "ok": full_budget_ok,
            "limit": FULL_INCREMENTAL_LIMIT,
            "download_bytes": full_incremental,
        }
    )
    ok = ok and full_budget_ok
    full_ok = step(
        "complete-Mathlib-closure",
        [
            "lean-runtime",
            "check",
            main_lean,
            "--using",
            str(lock_path),
            "--no-source-build",
        ],
        timeout=arguments.cold_timeout,
    )
    ok = full_ok and ok
    warm_ok = step(
        "warm",
        ["lean-runtime", "check", main_lean],
        timeout=WARM_TIMEOUT,
        limit=WARM_LIMIT,
    )
    ok = warm_ok and ok
    diagnostic_ok = step(
        "failing-proof-diagnostics",
        ["lean-runtime", "check", fail_lean, "--using", str(lock_path)],
        timeout=WARM_TIMEOUT,
        expect_exit=1,
        require="Fail.lean",
        forbid="instance-",
    )
    ok = diagnostic_ok and ok
    replay_ok = step(
        "lock-reproducibility",
        ["lean-runtime", "check", main_lean, "--using", str(lock_path)],
        timeout=WARM_TIMEOUT,
        limit=WARM_LIMIT,
    )
    ok = replay_ok and ok
    batch_ok = step(
        "python-batch-api",
        [sys.executable, str(ACCEPTANCE / "python_api.py"), str(lock_path)],
        timeout=1500,
    )
    ok = batch_ok and ok

    usage = shutil.disk_usage(home)
    seen: set[tuple[int, int]] = set()
    home_bytes = 0
    for file in home.rglob("*"):
        if not file.is_file() or file.is_symlink():
            continue
        stat = file.stat()
        identity = (stat.st_dev, stat.st_ino)
        if identity not in seen:
            seen.add(identity)
            home_bytes += stat.st_size
    report["home_bytes"] = home_bytes
    report["home_limit_bytes"] = HOME_LIMIT
    size_ok = home_bytes <= HOME_LIMIT
    steps.append(
        {
            "name": "installed-size-budget",
            "ok": size_ok,
            "home_bytes": home_bytes,
            "limit": HOME_LIMIT,
        }
    )
    ok = size_ok and ok
    report["ok"] = ok
    report["disk_free_bytes"] = usage.free
    Path(arguments.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"report: {arguments.report}")
    if not arguments.keep_home and arguments.home is None:
        shutil.rmtree(home, ignore_errors=True)
    print("GATE PASSED" if ok else "GATE FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
