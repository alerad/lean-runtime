"""Internal cross-platform filesystem operations."""

from __future__ import annotations

import os
import shutil
import stat
import time
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any, cast

_WINDOWS_MOUNT_POINT_REPARSE_TAG = 0xA0000003


def _retry_writable(
    function: Callable[..., Any],
    path: str,
    _error: tuple[type[BaseException], BaseException, TracebackType | None],
) -> None:
    """Clear Git-for-Windows read-only attributes and retry one removal."""

    os.chmod(path, stat.S_IWRITE)
    function(path)


def is_link(path: Path) -> bool:
    """Whether ``path`` is a symbolic link or a Windows directory junction.

    Shared package directories are attached as links. Python reports a
    junction as a plain directory (``is_symlink()`` is false), so every place
    that asks "is this an attached link?" must go through this helper.
    """

    if path.is_symlink():
        return True
    if os.name != "nt":
        return False
    try:
        status = os.lstat(path)
    except OSError:
        return False
    attributes = getattr(status, "st_file_attributes", 0)
    reparse_tag = getattr(status, "st_reparse_tag", 0)
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT) and (
        reparse_tag == _WINDOWS_MOUNT_POINT_REPARSE_TAG
    )


def link_directory(target: Path, link: Path) -> None:
    """Attach ``link`` to the directory ``target`` without requiring privileges.

    A symbolic link is preferred everywhere. Windows only grants symlink
    creation to administrators or Developer Mode users
    (``ERROR_PRIVILEGE_NOT_HELD``, 1314); in that case a directory junction,
    which any user may create and which Lake and Git traverse identically, is
    used instead.
    """

    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError as exc:
        if os.name != "nt" or getattr(exc, "winerror", None) != 1314:
            raise
        import _winapi

        # These members exist only in the Windows runtime/typeshed surface.
        # Resolve the API dynamically so strict checking on macOS/Linux still
        # validates this module instead of rejecting the platform-only member.
        create_junction = cast(Callable[[str, str], None], vars(_winapi)["CreateJunction"])
        create_junction(str(target.resolve()), str(link))


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
