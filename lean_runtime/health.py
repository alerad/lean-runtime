"""Local installation and cache health checks."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from typing import Literal

from .errors import ToolchainError
from .store import EnvironmentStore
from .toolchains import ToolchainManager

CheckStatus = Literal["pass", "warning", "fail"]


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    status: CheckStatus
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.status != "fail" for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "checks": [check.to_dict() for check in self.checks]}


def diagnose(toolchains: ToolchainManager, store: EnvironmentStore) -> DoctorReport:
    """Inspect prerequisites without downloading or building anything."""
    checks: list[DoctorCheck] = []
    git = shutil.which("git")
    if git is None:
        checks.append(DoctorCheck("git", "fail", "Git is not available on PATH"))
    else:
        process = subprocess.run([git, "--version"], text=True, capture_output=True, check=False)
        status: CheckStatus = "pass" if process.returncode == 0 else "fail"
        checks.append(DoctorCheck("git", status, process.stdout.strip() or "Git failed"))

    try:
        with tempfile.NamedTemporaryFile(dir=store.home, prefix="doctor-", delete=True):
            pass
        checks.append(DoctorCheck("store", "pass", f"Store is writable: {store.home}"))
    except OSError as exc:
        checks.append(DoctorCheck("store", "fail", f"Store is not writable: {exc}"))

    free = shutil.disk_usage(store.home).free
    if free < 256 * 1024 * 1024:
        checks.append(DoctorCheck("disk", "fail", f"Only {free // (1024**2)} MiB free"))
    elif free < 2 * 1024 * 1024 * 1024:
        checks.append(DoctorCheck("disk", "warning", f"Only {free // (1024**2)} MiB free"))
    else:
        checks.append(DoctorCheck("disk", "pass", f"{free // (1024**3)} GiB free"))

    try:
        elan = toolchains.elan_path(bootstrap=False)
    except ToolchainError:
        if os.name != "nt" and platform.system() in {"Darwin", "Linux"}:
            checks.append(
                DoctorCheck("elan", "warning", "Private Elan is not installed; it will bootstrap")
            )
        else:
            checks.append(
                DoctorCheck("elan", "fail", "Set LEAN_RUNTIME_ELAN to an Elan executable")
            )
    else:
        checks.append(DoctorCheck("elan", "pass", f"Elan executable: {elan}"))

    staging = tuple(store.environments.glob(".staging-*"))
    status = "warning" if staging else "pass"
    message = f"{len(staging)} incomplete staging directories" if staging else "No stale builds"
    checks.append(DoctorCheck("staging", status, message))
    return DoctorReport(tuple(checks))
