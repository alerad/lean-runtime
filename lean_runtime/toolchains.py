"""Private Elan bootstrap and Lean toolchain management."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from .backends import LocalBackend
from .errors import ToolchainError
from .events import EventEmitter
from .policies import ExecutionPolicy
from .serialization import write_json_atomic
from .toolchain_slim import (
    SLIM_PROFILE,
    SlimManifest,
    materialize,
    verify_capabilities,
)

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

    def __init__(
        self,
        home: str | os.PathLike[str] | None = None,
        events: EventEmitter | None = None,
    ) -> None:
        self.home = Path(home).expanduser().resolve() if home else default_runtime_home()
        self.elan_home = self.home / "elan"
        self.events = events or EventEmitter()
        self.remote_ensure: Callable[[str, threading.Event | None], bool] | None = None
        self._executable_digests: dict[str, tuple[int, int, str]] = {}

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

    @staticmethod
    def _toolchain_dir_name(name: str) -> str:
        return name.replace("/", "--").replace(":", "---")

    @staticmethod
    def _toolchain_name(directory: str) -> str:
        return directory.replace("---", ":").replace("--", "/")

    def available_toolchains(self) -> tuple[str, ...]:
        """List private, slim, and safely detected user toolchains."""
        roots = [self.elan_home / "toolchains", self.slim_root]
        user_home = self._user_elan_home()
        if user_home is not None:
            roots.append(user_home / "toolchains")
        names: set[str] = set()
        for root in roots:
            if not root.is_dir():
                continue
            names.update(
                self._toolchain_name(path.name)
                for path in root.iterdir()
                if path.is_dir() and (path / "bin" / "lean").is_file()
            )
        return tuple(sorted(names))

    def _elan_toolchain_dir(self, name: str) -> Path:
        elan_home = Path(os.environ.get("LEAN_RUNTIME_ELAN_HOME", self.elan_home))
        return elan_home / "toolchains" / self._toolchain_dir_name(name)

    def _user_elan_home(self) -> Path | None:
        """Find an ordinary user Elan home without taking ownership of it."""
        if os.environ.get("LEAN_RUNTIME_ELAN") or os.environ.get("LEAN_RUNTIME_ELAN_HOME"):
            return None
        candidates: list[Path] = []
        discovered = shutil.which("elan")
        if discovered:
            candidates.append(Path(discovered).expanduser().absolute().parent.parent)
        candidates.append(Path.home() / ".elan")
        private = self.elan_home.resolve()
        for candidate in candidates:
            try:
                selected = candidate.resolve()
            except OSError:
                continue
            if selected != private and (selected / "toolchains").is_dir():
                return selected
        return None

    def _user_toolchain_dir(self, name: str) -> Path | None:
        home = self._user_elan_home()
        if home is None:
            return None
        directory = home / "toolchains" / self._toolchain_dir_name(name)
        return directory if (directory / "bin" / "lean").is_file() else None

    def _full_toolchain_dir(self, name: str) -> Path:
        private = self._elan_toolchain_dir(name)
        if (private / "bin" / "lean").is_file():
            return private
        external = self._user_toolchain_dir(name)
        return external if external is not None else private

    @property
    def slim_root(self) -> Path:
        return self.home / "toolchains" / SLIM_PROFILE

    def slim_path(self, toolchain: str) -> Path:
        return self.slim_root / self._toolchain_dir_name(normalize_toolchain(toolchain))

    def slim_manifest(self, toolchain: str) -> SlimManifest | None:
        return SlimManifest.load(self.slim_path(toolchain))

    def has_slim(self, toolchain: str) -> bool:
        directory = self.slim_path(toolchain)
        return SlimManifest.load(directory) is not None and (directory / "bin" / "lean").is_file()

    def is_available_locally(self, toolchain: str) -> bool:
        """Check for a usable local toolchain without bootstrapping or network access."""
        name = normalize_toolchain(toolchain)
        full = self._full_toolchain_dir(name)
        return self.has_slim(name) or (full / "bin" / "lean").is_file()

    def materialize_slim(self, toolchain: str, *, verify: bool = True) -> SlimManifest:
        """Materialize and verify a slim check-profile copy of one toolchain.

        The Elan-managed original is never modified; use ``prune_original``
        afterwards to realize the disk saving.
        """
        name = self.ensure(toolchain)
        source = self._full_toolchain_dir(name)
        if not source.is_dir():
            raise ToolchainError(
                f"toolchain {name!r} is not present as a full installation; "
                "a slim copy can only be materialized from one"
            )
        self.events.emit(
            "toolchain.slim_started",
            f"Materializing slim {SLIM_PROFILE} toolchain {name}",
            toolchain=name,
        )
        created_at = datetime.now(timezone.utc).isoformat()
        destination = self.slim_path(name)
        manifest = materialize(source, destination, toolchain=name, created_at=created_at)
        if verify:
            results = verify_capabilities(destination, environment=self.environment)
            failures = [(probe, detail) for probe, ok, detail in results if not ok]
            if failures:
                shutil.rmtree(destination)
                summary = "; ".join(f"{probe}: {detail}" for probe, detail in failures)
                raise ToolchainError(
                    f"slim toolchain failed capability verification and was removed: {summary}"
                )
        self.events.emit(
            "toolchain.slim_ready",
            f"Slim {SLIM_PROFILE} toolchain is ready",
            toolchain=name,
            files=manifest.files,
            bytes=manifest.bytes,
            excluded_bytes=manifest.excluded_bytes,
        )
        return manifest

    def prune_original(self, toolchain: str) -> None:
        """Uninstall the full Elan toolchain after a verified slim copy exists.

        Checking keeps working through the slim copy. Native compilation and
        source builds of new environments need the full toolchain again.
        """
        name = normalize_toolchain(toolchain)
        if not self.has_slim(name):
            raise ToolchainError(
                f"refusing to prune {name!r}: no verified slim toolchain is present"
            )
        external = self._user_toolchain_dir(name)
        if external is not None and not (self._elan_toolchain_dir(name) / "bin" / "lean").is_file():
            raise ToolchainError(
                "refusing to prune a user-managed Elan toolchain; Lean Runtime only removes "
                "toolchains from its private store"
            )
        process = LocalBackend().execute(
            [str(self.elan_path()), "toolchain", "uninstall", name],
            cwd=self.home,
            environment=self.environment,
            policy=ExecutionPolicy(timeout_seconds=300, max_output_bytes=1_000_000),
            cancel=None,
        )
        if process.exit_code:
            raise ToolchainError(
                f"could not uninstall Lean toolchain {name!r}:\n{process.stdout}{process.stderr}"
            )
        self.events.emit(
            "toolchain.original_pruned",
            f"Removed full toolchain {name}; the slim copy remains",
            toolchain=name,
        )

    def ensure(self, toolchain: str, *, cancel: threading.Event | None = None) -> str:
        """Install a toolchain if necessary and return its normalized name."""
        name = normalize_toolchain(toolchain)
        if self.has_slim(name) or (self._full_toolchain_dir(name) / "bin" / "lean").is_file():
            return name
        if self.remote_ensure is not None and self.remote_ensure(name, cancel):
            if not self.has_slim(name):
                raise ToolchainError("remote toolchain acquisition returned without a slim copy")
            return name
        if self.is_installed(name):
            return name
        self.events.emit(
            "toolchain.install_started",
            f"Installing Lean toolchain {name}",
            toolchain=name,
        )
        process = LocalBackend().execute(
            [str(self.elan_path()), "toolchain", "install", name],
            cwd=self.home,
            environment=self.environment,
            policy=ExecutionPolicy(timeout_seconds=1800, max_output_bytes=10_000_000),
            cancel=cancel,
        )
        if process.cancelled:
            raise ToolchainError(f"Lean toolchain installation was cancelled: {name!r}")
        if process.exit_code:
            raise ToolchainError(
                f"could not install Lean toolchain {name!r}:\n{process.stdout}{process.stderr}"
            )
        return name

    def ensure_full(self, toolchain: str, *, cancel: threading.Event | None = None) -> str:
        """Install the full Lake-capable toolchain even when a slim copy exists."""
        name = normalize_toolchain(toolchain)
        directory = self._full_toolchain_dir(name)
        if (directory / "bin" / "lean").is_file() and (directory / "bin" / "lake").is_file():
            return name
        self.events.emit(
            "toolchain.install_started",
            f"Installing full Lean toolchain {name}",
            toolchain=name,
            capability="lake",
        )
        process = LocalBackend().execute(
            [str(self.elan_path()), "toolchain", "install", name],
            cwd=self.home,
            environment=self.environment,
            policy=ExecutionPolicy(timeout_seconds=1800, max_output_bytes=10_000_000),
            cancel=cancel,
        )
        if process.cancelled:
            raise ToolchainError(f"Lean toolchain installation was cancelled: {name!r}")
        if process.exit_code:
            raise ToolchainError(
                f"could not install full Lean toolchain {name!r}:\n{process.stdout}{process.stderr}"
            )
        if not (directory / "bin" / "lake").is_file():
            raise ToolchainError(f"installed toolchain {name!r} does not provide 'lake'")
        return name

    def command(self, toolchain: str, executable: str, *args: str) -> list[str]:
        """Construct a command pinned to one toolchain.

        A full Elan installation is preferred; when only a verified slim copy
        remains, its executables are invoked directly.
        """
        name = self.ensure(toolchain)
        full = self._full_toolchain_dir(name)
        full_lean = full / "bin" / "lean"
        if os.name == "nt":
            full_lean = full_lean.with_suffix(".exe")
        if self.has_slim(name) and not full_lean.is_file():
            binary = self.slim_path(name) / "bin" / executable
            if os.name == "nt":
                binary = binary.with_suffix(".exe")
            if not binary.is_file():
                raise ToolchainError(
                    f"slim toolchain {name!r} does not provide {executable!r}; "
                    "reinstall the full toolchain for this operation"
                )
            return [str(binary), *args]
        external = self._user_toolchain_dir(name)
        if external is not None and full == external:
            binary = external / "bin" / executable
            if os.name == "nt":
                binary = binary.with_suffix(".exe")
            if not binary.is_file():
                raise ToolchainError(f"toolchain {name!r} does not provide {executable!r}")
            return [str(binary), *args]
        return [str(self.elan_path()), "run", name, executable, *args]

    def executable_digest(self, toolchain: str, executable: str) -> str:
        """Hash the exact resolved executable for compatibility-cache identities."""
        name = self.ensure(toolchain)
        full = self._full_toolchain_dir(name)
        full_lean = full / "bin" / "lean"
        if os.name == "nt":
            full_lean = full_lean.with_suffix(".exe")
        if self.has_slim(name) and not full_lean.is_file():
            binary = self.slim_path(name) / "bin" / executable
        else:
            binary = full / "bin" / executable
        if os.name == "nt" and not binary.is_file():
            binary = binary.with_suffix(".exe")
        if not binary.is_file():
            raise ToolchainError(f"toolchain {name!r} does not provide {executable!r}")
        stat_result = binary.stat()
        cache_key = str(binary.resolve())
        identity = (stat_result.st_size, stat_result.st_mtime_ns)
        cached = self._executable_digests.get(cache_key)
        if cached is not None and cached[:2] == identity:
            return cached[2]
        cache_path = self.home / "toolchain-executable-digests.json"
        try:
            persisted = json.loads(cache_path.read_text(encoding="utf-8"))
            entry = persisted.get(cache_key, {})
            if entry.get("size") == identity[0] and entry.get("mtime_ns") == identity[1]:
                value = entry.get("digest")
                if isinstance(value, str):
                    self._executable_digests[cache_key] = (*identity, value)
                    return value
        except (OSError, AttributeError, json.JSONDecodeError):
            persisted = {}
        digest = hashlib.sha256()
        with binary.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        value = "sha256:" + digest.hexdigest()
        self._executable_digests[cache_key] = (*identity, value)
        persisted[cache_key] = {
            "size": identity[0],
            "mtime_ns": identity[1],
            "digest": value,
        }
        self.home.mkdir(parents=True, exist_ok=True)
        write_json_atomic(cache_path, persisted)
        return value
