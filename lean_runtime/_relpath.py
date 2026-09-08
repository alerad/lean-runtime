"""Internal portable relative-path policy for external input.

Manifests, lock files, capsule inventories, archive members and declaration
indexes all carry paths chosen by someone else. Every one of those boundaries
applies this single policy before joining such a path below a directory it
owns; each boundary wraps the :class:`ValueError` raised here in its own
domain error so messages stay specific to the input being read.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

_DRIVE_OR_SCHEME = re.compile(r"^[A-Za-z]:")
_FORBIDDEN_CHARACTERS = ("\\", "\x00")


def safe_relative_posix(value: object, *, allow_root: bool = False) -> PurePosixPath:
    """Return ``value`` as a normalized POSIX path that stays below its base.

    Rejected forms: non-strings, empty strings, absolute POSIX paths, UNC and
    Windows drive forms, backslashes (never a valid separator in manifests and
    never a valid file-name character on Windows), NUL bytes and any ``..``
    component. ``.`` and ``./`` prefixes normalize away; the bare root ``.``
    is rejected unless ``allow_root`` is set.
    """
    if not isinstance(value, str):
        raise ValueError("path must be a string")
    if not value:
        raise ValueError("path must not be empty")
    if any(character in value for character in _FORBIDDEN_CHARACTERS):
        raise ValueError(f"path contains a forbidden character: {value!r}")
    if _DRIVE_OR_SCHEME.match(value):
        raise ValueError(f"path must not carry a drive letter: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise ValueError(f"path must be relative: {value!r}")
    if ".." in path.parts:
        raise ValueError(f"path must not traverse upward: {value!r}")
    if path == PurePosixPath(".") and not allow_root:
        raise ValueError("path must not be the root")
    return path


def safe_relative_name(value: object) -> PurePosixPath:
    """A relative path that also rejects components Windows cannot store."""
    path = safe_relative_posix(value)
    for part in path.parts:
        if part.endswith((" ", ".")) and part not in (".", ".."):
            raise ValueError(f"path component is not portable: {part!r}")
        if any(character in part for character in '<>:"|?*'):
            raise ValueError(f"path component is not portable: {part!r}")
    return path


def packages_directory(manifest: object) -> PurePosixPath:
    """The ``packagesDir`` of a Lake manifest, validated for joining below a workspace."""
    if not isinstance(manifest, dict):
        raise ValueError("Lake manifest must be a JSON object")
    value = manifest.get("packagesDir", ".lake/packages")
    if not isinstance(value, str):
        raise ValueError("Lake manifest packagesDir must be a string")
    return safe_relative_posix(value)


def below(base: Path, relative: PurePosixPath) -> Path:
    """Join an already validated relative path below ``base``."""
    return base.joinpath(*relative.parts)
