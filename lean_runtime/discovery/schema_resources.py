"""Access to JSON schemas bundled in the installed wheel."""

from __future__ import annotations

from pathlib import Path

SCHEMA_NAMES = (
    "catalog-v1.schema.json",
    "plan-v1.schema.json",
    "result-v1.schema.json",
)


def schema_path(name: str) -> Path:
    """Return the filesystem path of one bundled schema."""

    if name not in SCHEMA_NAMES:
        raise ValueError(f"unknown Lean Runtime discovery schema: {name!r}")
    path = Path(__file__).resolve().parent / "schemas" / name
    if not path.is_file():
        raise FileNotFoundError(f"Lean Runtime discovery schema is missing: {name}")
    return path
