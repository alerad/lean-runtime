"""Independent file copies that share storage, never writable hard links."""

from __future__ import annotations

import ctypes
import errno
import os
import shutil
import sys
from pathlib import Path


def reflink(source: Path, destination: Path) -> None:
    """Create a new CoW file or raise OSError; never fall back to a full copy."""
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        clone = libc.clonefile
        clone.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        clone.restype = ctypes.c_int
        if clone(os.fsencode(source), os.fsencode(destination), 0):
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(destination))
    elif sys.platform.startswith("linux"):
        import fcntl

        # FICLONE operates on a newly created inode, not a shared writable link.
        with source.open("rb") as src, destination.open("xb") as dst:
            try:
                fcntl.ioctl(dst.fileno(), 0x40049409, src.fileno())
            except OSError:
                destination.unlink()
                raise
    else:
        raise OSError(errno.ENOTSUP, "copy-on-write cloning is unavailable")


def copy_file(source: str | Path, destination: str | Path) -> str:
    """shutil.copytree copy function preserving metadata and write isolation."""
    src, dst = Path(source), Path(destination)
    try:
        reflink(src, dst)
    except OSError:
        return str(shutil.copy2(src, dst))
    shutil.copystat(src, dst)
    return str(dst)


def copy_metadata(source: Path, destination: Path) -> None:
    """Copy metadata including native macOS ACLs (copystat omits those)."""
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        copy = libc.copyfile
        copy.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_uint32]
        copy.restype = ctypes.c_int
        # COPYFILE_METADATA = COPYFILE_ACL | COPYFILE_STAT | COPYFILE_XATTR.
        if copy(os.fsencode(source), os.fsencode(destination), None, 7):
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(destination))
    else:
        shutil.copystat(source, destination)
