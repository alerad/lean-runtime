"""Names of managed package directories in the shared project store.

A package directory is content-addressed by its identity marker. The original
scheme named directories ``project_package_<64 hex>``; that 80-character name
pushed Mathlib's deepest build outputs past Windows' 260-character path limit
(``…\\project_package_<64>\\.lake\\build\\lib\\lean\\Mathlib\\…\\Isometric.olean.server``).
New directories therefore use ``pkg_<32 hex>``, the same digest truncated to
128 bits. Legacy directories stay valid and reusable.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .serialization import sha256_id

PACKAGE_ID_PATTERN = re.compile(r"(?:project_package_[0-9a-f]{64}|pkg_[0-9a-f]{32})\Z")
_DIRECTORY_GLOBS = ("project_package_*", "pkg_*")


def package_directory_id(identity: dict[str, Any]) -> str:
    """Name the store directory for one package identity marker."""
    digest = sha256_id("project_package", identity).removeprefix("project_package_")
    return f"pkg_{digest[:32]}"


def package_id_matches(identity: dict[str, Any], package_id: str) -> bool:
    """Whether ``package_id`` names ``identity`` under either naming scheme."""
    return package_id in {sha256_id("project_package", identity), package_directory_id(identity)}


def is_package_id(value: str) -> bool:
    return PACKAGE_ID_PATTERN.fullmatch(value) is not None


def package_directories(root: Path) -> list[Path]:
    """Managed package directories under ``root`` in both naming schemes, sorted."""
    return sorted(
        path for pattern in _DIRECTORY_GLOBS for path in root.glob(pattern) if path.is_dir()
    )
