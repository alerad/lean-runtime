"""Capability-probed Lean header snapshots for repeated project checks."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from .errors import EnvironmentError
from .import_syntax import is_import_line
from .locking import FileLock
from .serialization import sha256_id
from .store import platform_compatibility

if TYPE_CHECKING:
    import threading

    from .events import EventEmitter
    from .toolchains import ToolchainManager

ENABLE_VARIABLE = "LEAN_RUNTIME_HEADER_SNAPSHOTS"
_TRUTHY = {"1", "true", "on", "yes"}


def _configured_enabled() -> bool:
    return os.environ.get(ENABLE_VARIABLE, "").strip().lower() in _TRUTHY


def _header_identity(source: str) -> str:
    """Return a conservative identity for the module header/import block."""
    lines: list[str] = []
    saw_import = False
    block_depth = 0
    for line in source.splitlines(keepends=True):
        stripped = line.strip()
        block_depth += stripped.count("/-") - stripped.count("-/")
        import_line = is_import_line(stripped)
        header_line = (
            block_depth > 0
            or not stripped
            or stripped.startswith(("--", "/-", "-/", "module", "prelude"))
            or import_line
        )
        if import_line:
            saw_import = True
        if saw_import and not header_line:
            break
        lines.append(line)
    return "".join(lines)


class _SnapshotPaths(NamedTuple):
    snapshot: Path
    deps: Path
    lock: Path

    def valid(self) -> bool:
        return self.snapshot.is_file() and self.deps.is_file()


class LeanHeaderCache:
    """Reuse Lean's native experimental import snapshots when the binary supports them.

    Snapshots are keyed by exact toolchain, workspace identity, logical module
    name, and import-block content, so distinct modules never share a snapshot.
    Existing snapshots are loaded without holding any lock; only first-time
    snapshot creation serializes behind a per-key file lock.
    """

    def __init__(
        self,
        home: Path,
        toolchains: ToolchainManager,
        events: EventEmitter | None = None,
    ) -> None:
        self.home = home / "header-snapshots"
        self.toolchains = toolchains
        self.events = events
        self.enabled = _configured_enabled()
        self._support: dict[str, bool] = {}
        self._toolchain_keys: dict[str, str] = {}

    def _toolchain_key(self, toolchain: str) -> str:
        if toolchain in self._toolchain_keys:
            return self._toolchain_keys[toolchain]
        digest = getattr(self.toolchains, "executable_digest", None)
        executable = str(digest(toolchain, "lean")) if callable(digest) else toolchain
        key = sha256_id(
            "lean-header",
            {
                "toolchain": toolchain,
                "executable": executable,
                "platform": platform_compatibility(),
            },
        ).removeprefix("lean-header-")
        self._toolchain_keys[toolchain] = key
        return key

    def supported(self, toolchain: str) -> bool:
        key = self._toolchain_key(toolchain)
        if key in self._support:
            return self._support[key]
        marker = self.home / "capabilities" / f"{key}.json"
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
            supported = value["incr_header"] is True
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            try:
                process = subprocess.run(
                    self.toolchains.command(toolchain, "lean", "--help"),
                    env=self.toolchains.environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=30,
                    check=False,
                )
                supported = process.returncode == 0 and "--incr-header-save" in process.stdout
            except (OSError, subprocess.TimeoutExpired):
                supported = False
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"incr_header": supported}) + "\n", encoding="utf-8")
        self._support[key] = supported
        return supported

    def _paths(
        self, toolchain: str, workspace_identity: str, module: str, source: str
    ) -> _SnapshotPaths:
        key = sha256_id(
            "header",
            {
                "toolchain": self._toolchain_key(toolchain),
                "workspace": workspace_identity,
                "module": module,
                "header": hashlib.sha256(_header_identity(source).encode()).hexdigest(),
            },
        ).removeprefix("header-")
        root = self.home / self._toolchain_key(toolchain)
        snapshot = root / f"{key}.snap"
        return _SnapshotPaths(snapshot, Path(str(snapshot) + ".deps"), root / f"{key}.lock")

    def discard(self, toolchain: str, workspace_identity: str, module: str, source: str) -> None:
        """Quarantine a snapshot that produced timeout or staleness symptoms."""
        paths = self._paths(toolchain, workspace_identity, module, source)
        for path in (paths.snapshot, paths.deps):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)

    @contextmanager
    def _creation_lock(
        self, paths: _SnapshotPaths, module: str, cancel: threading.Event | None
    ) -> Iterator[None]:
        """Acquire the per-key creation lock, announcing contention once."""
        lock = FileLock(paths.lock, timeout=0)
        try:
            lock.__enter__()
        except EnvironmentError:
            if self.events is not None:
                self.events.emit(
                    "check.header_wait",
                    f"Waiting for header snapshot initialization: {module}",
                    phase="check",
                    module=module,
                )
            lock = FileLock(paths.lock, cancel=cancel)
            lock.__enter__()
        try:
            yield
        finally:
            lock.__exit__(None, None, None)

    @contextmanager
    def command(
        self,
        toolchain: str,
        workspace_identity: str,
        module: str,
        source: str,
        command: Sequence[str],
        *,
        cancel: threading.Event | None = None,
    ) -> Iterator[list[str]]:
        if not self.enabled or not self.supported(toolchain):
            yield list(command)
            return
        paths = self._paths(toolchain, workspace_identity, module, source)
        if paths.valid():
            yield [*command[:-1], f"--incr-load={paths.snapshot}", command[-1]]
            return
        paths.snapshot.parent.mkdir(parents=True, exist_ok=True)
        hit_after_wait = False
        with self._creation_lock(paths, module, cancel):
            if paths.valid():
                hit_after_wait = True
            else:
                root = str(paths.snapshot.parent)
                with tempfile.TemporaryDirectory(
                    prefix=f".{paths.snapshot.stem}.", dir=root
                ) as temporary_root:
                    temporary = Path(temporary_root) / "header.snap"
                    temporary_deps = Path(str(temporary) + ".deps")
                    yield [*command[:-1], f"--incr-header-save={temporary}", command[-1]]
                    if temporary.is_file() and temporary_deps.is_file():
                        os.replace(temporary, paths.snapshot)
                        os.replace(temporary_deps, paths.deps)
                return
        if hit_after_wait:
            yield [*command[:-1], f"--incr-load={paths.snapshot}", command[-1]]
