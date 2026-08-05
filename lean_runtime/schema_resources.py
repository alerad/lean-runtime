"""Access to the versioned JSON schemas installed with Lean Runtime."""

from __future__ import annotations

import sysconfig
from pathlib import Path

SCHEMA_NAMES = frozenset(
    {
        "comparison-v1.schema.json",
        "execution-v1.schema.json",
        "cleanup-v1.schema.json",
        "inspect-v1.schema.json",
        "matrix-v1.schema.json",
        "profile-v1.schema.json",
        "verify-v1.schema.json",
    }
)


def schema_path(name: str) -> Path:
    """Return one installed public schema, rejecting unknown or unsafe names."""
    if name not in SCHEMA_NAMES:
        raise ValueError(f"unknown Lean Runtime schema: {name!r}")
    installed = Path(sysconfig.get_path("data")) / "share" / "lean-runtime" / "schemas" / name
    if installed.is_file():
        return installed
    source = Path(__file__).resolve().parent.parent / "schemas" / name
    if source.is_file():
        return source
    raise FileNotFoundError(f"Lean Runtime schema is missing: {name}")
