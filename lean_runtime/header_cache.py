"""Capability-probed Lean header snapshots for repeated project checks."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from .locking import FileLock
from .serialization import sha256_id
from .store import platform_compatibility

if TYPE_CHECKING:
    from .toolchains import ToolchainManager


def _header_identity(source: str) -> str:
    """Return a conservative identity for the module header/import block."""
    lines: list[str] = []
    saw_import = False
    block_depth = 0
    for line in source.splitlines(keepends=True):
        stripped = line.strip()
        block_depth += stripped.count("/-") - stripped.count("-/")
        header_line = (
            block_depth > 0
            or not stripped
            or stripped.startswith(
                ("--", "/-", "-/", "module", "prelude", "import ", "public import ")
            )
        )
        if stripped.startswith(("import ", "public import ")):
            saw_import = True
        if saw_import and not header_line:
            break
        lines.append(line)
    return "".join(lines)


class LeanHeaderCache:
    """Reuse Lean's native experimental import snapshots when the binary supports them."""

    def __init__(self, home: Path, toolchains: ToolchainManager) -> None:
        self.home = home / "header-snapshots"
        self.toolchains = toolchains
        self._support: dict[str, bool] = {}

    def _toolchain_key(self, toolchain: str) -> str:
        digest = getattr(self.toolchains, "executable_digest", None)
        executable = str(digest(toolchain, "lean")) if callable(digest) else toolchain
        return sha256_id(
            "lean-header",
            {
                "toolchain": toolchain,
                "executable": executable,
                "platform": platform_compatibility(),
            },
        ).removeprefix("lean-header-")

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

    @contextmanager
    def command(
        self,
        toolchain: str,
        workspace_identity: str,
        source: str,
        command: Sequence[str],
    ) -> Iterator[list[str]]:
        if not self.supported(toolchain):
            yield list(command)
            return
        key = sha256_id(
            "header",
            {
                "toolchain": self._toolchain_key(toolchain),
                "workspace": workspace_identity,
                "header": hashlib.sha256(_header_identity(source).encode()).hexdigest(),
            },
        ).removeprefix("header-")
        root = self.home / self._toolchain_key(toolchain)
        snapshot = root / f"{key}.snap"
        deps = Path(str(snapshot) + ".deps")
        root.mkdir(parents=True, exist_ok=True)
        with FileLock(root / f"{key}.lock"):
            if snapshot.is_file() and deps.is_file():
                yield [*command[:-1], f"--incr-load={snapshot}", command[-1]]
                return
            with tempfile.TemporaryDirectory(prefix=f".{key}.", dir=root) as temporary_root:
                temporary = Path(temporary_root) / "header.snap"
                temporary_deps = Path(str(temporary) + ".deps")
                yield [*command[:-1], f"--incr-header-save={temporary}", command[-1]]
                if temporary.is_file() and temporary_deps.is_file():
                    os.replace(temporary, snapshot)
                    os.replace(temporary_deps, deps)
