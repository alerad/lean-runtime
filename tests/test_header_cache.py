from __future__ import annotations

import os
import sys
from pathlib import Path

from lean_runtime.header_cache import LeanHeaderCache


class SnapshotToolchains:
    @property
    def environment(self) -> dict[str, str]:
        return os.environ.copy()

    def executable_digest(self, _toolchain: str, executable: str) -> str:
        return f"sha256:{executable}"

    def command(self, _toolchain: str, _executable: str, *args: str) -> list[str]:
        assert args == ("--help",)
        return [sys.executable, "-c", "print('--incr-header-save --incr-load')"]


def _saved_path(command: list[str]) -> Path:
    argument = next(item for item in command if item.startswith("--incr-header-save="))
    return Path(argument.split("=", 1)[1])


def test_header_snapshots_are_reused_for_the_same_import_block(tmp_path: Path) -> None:
    cache = LeanHeaderCache(tmp_path, SnapshotToolchains())  # type: ignore[arg-type]
    base = ["lake", "env", "lean", "Main.lean"]
    first_source = "import Mathlib\nexample : True := by trivial\n"

    with cache.command("v4.33.0", "workspace", first_source, base) as first:
        saved = _saved_path(first)
        saved.write_bytes(b"snapshot")
        Path(str(saved) + ".deps").write_text("{}")

    changed_body = "import Mathlib\nexample : 1 = 1 := by rfl\n"
    with cache.command("v4.33.0", "workspace", changed_body, base) as second:
        assert any(argument.startswith("--incr-load=") for argument in second)

    changed_import = "import Mathlib.Data.Nat.Basic\nexample : True := by trivial\n"
    with cache.command("v4.33.0", "workspace", changed_import, base) as third:
        assert any(argument.startswith("--incr-header-save=") for argument in third)
