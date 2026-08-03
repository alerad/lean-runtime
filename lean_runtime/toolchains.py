"""Private Elan bootstrap and Lean toolchain management."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from .errors import ToolchainError

ELAN_VERSION = "4.2.3"
ELAN_INIT_URL = f"https://raw.githubusercontent.com/leanprover/elan/v{ELAN_VERSION}/elan-init.sh"
ELAN_INIT_SHA256 = "a620ff1641616222c8d37c54845492004bb84d6877cdbc944dd65c1aa685bf53"


def default_runtime_home() -> Path:
    """Return the cache root without depending on a platform-specific package."""
    override = os.environ.get("LEAN_RUNTIME_HOME")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "lean-runtime"
    if sys_platform() == "darwin":
        return Path.home() / "Library" / "Caches" / "lean-runtime"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "lean-runtime"


def sys_platform() -> str:
    """Small indirection used by tests."""
    import sys

    return sys.platform


def normalize_toolchain(value: str) -> str:
    """Normalize common Lean release spellings to an Elan toolchain name."""
    value = value.strip()
    if not value:
        raise ToolchainError("toolchain must not be empty")
    if "/" in value or ":" in value:
        return value
    if value.startswith("v"):
        return f"leanprover/lean4:{value}"
    if value[0].isdigit():
        return f"leanprover/lean4:v{value}"
    return value


class ToolchainManager:
    """Own an isolated Elan home and install Lean releases on demand.

    Set ``LEAN_RUNTIME_ELAN`` to use an explicit existing Elan executable.
    This is useful in CI and development; ordinary users get a private copy.
    """

    def __init__(self, home: str | os.PathLike[str] | None = None) -> None:
        self.home = Path(home).expanduser().resolve() if home else default_runtime_home()
        self.elan_home = self.home / "elan"

    @property
    def environment(self) -> dict[str, str]:
        env = os.environ.copy()
        elan_home = Path(os.environ.get("LEAN_RUNTIME_ELAN_HOME", self.elan_home))
        env["ELAN_HOME"] = str(elan_home)
        env["PATH"] = os.pathsep.join((str(elan_home / "bin"), env.get("PATH", "")))
        return env

    def elan_path(self, *, bootstrap: bool = True) -> Path:
        override = os.environ.get("LEAN_RUNTIME_ELAN")
        if override:
            # Elan is commonly installed as symlinks to one multicall binary.
            # Resolving the link changes argv[0] to `elan-init`, which selects
            # the installer interface instead of the toolchain manager.
            path = Path(override).expanduser().absolute()
            if path.is_file():
                return path
            raise ToolchainError(f"LEAN_RUNTIME_ELAN does not name a file: {path}")
        name = "elan.exe" if os.name == "nt" else "elan"
        private = self.elan_home / "bin" / name
        if private.is_file():
            return private
        if not bootstrap:
            raise ToolchainError("private Elan is not installed")
        self.bootstrap_elan()
        if not private.is_file():
            raise ToolchainError("Elan installer completed without creating the executable")
        return private

    def bootstrap_elan(self) -> Path:
        """Install Elan under the runtime cache without touching user defaults."""
        if os.name == "nt":
            raise ToolchainError(
                "automatic Elan bootstrap currently supports macOS and Linux; "
                "set LEAN_RUNTIME_ELAN on Windows"
            )
        self.home.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="elan-bootstrap-", dir=self.home) as raw:
            script = Path(raw) / "elan-init.sh"
            try:
                with urllib.request.urlopen(ELAN_INIT_URL, timeout=30) as response:
                    installer = response.read()
            except OSError as exc:
                raise ToolchainError(f"could not download Elan installer: {exc}") from exc
            observed = hashlib.sha256(installer).hexdigest()
            if observed != ELAN_INIT_SHA256:
                raise ToolchainError("downloaded Elan installer failed its SHA-256 integrity check")
            script.write_bytes(installer)
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
            env = self.environment
            process = subprocess.run(
                [str(script), "-y", "--no-modify-path", "--default-toolchain", "none"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
        if process.returncode:
            raise ToolchainError(f"Elan installer exited {process.returncode}:\n{process.stdout}")
        return self.elan_path(bootstrap=False)

    def is_installed(self, toolchain: str) -> bool:
        name = normalize_toolchain(toolchain)
        process = subprocess.run(
            [str(self.elan_path()), "toolchain", "list"],
            env=self.environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if process.returncode:
            raise ToolchainError(f"could not list installed Lean toolchains:\n{process.stdout}")
        installed = {
            fields[0] for line in process.stdout.splitlines() if (fields := line.split(maxsplit=1))
        }
        return name in installed

    def ensure(self, toolchain: str) -> str:
        """Install a toolchain if necessary and return its normalized name."""
        name = normalize_toolchain(toolchain)
        if self.is_installed(name):
            return name
        process = subprocess.run(
            [str(self.elan_path()), "toolchain", "install", name],
            env=self.environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if process.returncode:
            raise ToolchainError(f"could not install Lean toolchain {name!r}:\n{process.stdout}")
        return name

    def command(self, toolchain: str, executable: str, *args: str) -> list[str]:
        """Construct a command pinned to one toolchain."""
        name = self.ensure(toolchain)
        return [str(self.elan_path()), "run", name, executable, *args]
