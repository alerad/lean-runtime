"""Internal cross-platform filesystem operations."""

from __future__ import annotations

import os
import shutil
import stat
import time
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any


def _retry_writable(
    function: Callable[..., Any],
    path: str,
    _error: tuple[type[BaseException], BaseException, TracebackType | None],
) -> None:
    """Clear Git-for-Windows read-only attributes and retry one removal."""

    os.chmod(path, stat.S_IWRITE)
    function(path)


def remove_tree(path: Path) -> None:
    """Remove a tree containing Git packs, tolerating transient Windows locks."""

    for attempt in range(4):
        try:
            shutil.rmtree(path, onerror=_retry_writable)
            return
        except PermissionError:
            if not path.exists():
                return
            if attempt == 3:
                raise
            time.sleep(0.05 * (2**attempt))
