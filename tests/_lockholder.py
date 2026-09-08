"""Child process that holds a FileLock until told to stop (test helper)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import IO

from lean_runtime.locking import FileLock

_SCRIPT = """
import sys
from pathlib import Path
from lean_runtime.locking import FileLock
with FileLock(Path(sys.argv[1]), timeout=0):
    sys.stdout.write("held\\n")
    sys.stdout.flush()
    sys.stdin.readline()
"""


class ForeignLockHolder:
    """Hold ``path`` from another process for the duration of the block."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._process: subprocess.Popen[str] | None = None

    def __enter__(self) -> ForeignLockHolder:
        self._process = subprocess.Popen(
            [sys.executable, "-c", _SCRIPT, str(self.path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        stdout: IO[str] = self._process.stdout  # type: ignore[assignment]
        assert stdout.readline().strip() == "held"
        return self

    def __exit__(self, *_: object) -> None:
        assert self._process is not None
        assert self._process.stdin is not None
        self._process.stdin.write("\n")
        self._process.stdin.close()
        self._process.wait(timeout=10)


def is_held(path: Path) -> bool:
    try:
        with FileLock(path, timeout=0):
            return False
    except Exception:  # noqa: BLE001
        return True
