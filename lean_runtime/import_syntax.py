"""Shared recognition of ordinary Lean import declarations."""

from __future__ import annotations

import re

IMPORT_STATEMENT = re.compile(
    r"^\s*(?:(?:public|meta)\s+)*import\s+(.+?)\s*$",
    re.MULTILINE,
)


def import_payload(line: str) -> str | None:
    """Return the module-list payload of one import declaration."""

    match = IMPORT_STATEMENT.fullmatch(line)
    return match.group(1) if match is not None else None


def is_import_line(line: str) -> bool:
    return import_payload(line) is not None
