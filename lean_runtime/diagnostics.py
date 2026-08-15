"""Best-effort normalization of Lean's version-dependent diagnostics."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace

from .models import Diagnostic, Severity

_HEADER = re.compile(
    r"^(?P<file>.*?):(?P<line>\d+):(?P<column>\d+): "
    r"(?P<severity>error|warning|information)(?:\([^)]*\))?:\s*(?P<message>.*)$"
)


def parse_diagnostics(output: str) -> tuple[Diagnostic, ...]:
    """Parse ordinary Lean diagnostic headers and their continuation lines.

    Lean does not expose one stable machine-readable diagnostic format across
    all supported releases. Unknown output remains available verbatim on the
    execution result rather than being discarded.
    """
    diagnostics: list[Diagnostic] = []
    current: dict[str, object] | None = None

    def finish() -> None:
        nonlocal current
        if current is not None:
            diagnostics.append(Diagnostic(**current))  # type: ignore[arg-type]
            current = None

    for line in output.splitlines():
        match = _HEADER.match(line)
        if match:
            finish()
            current = {
                "severity": match.group("severity"),
                "message": match.group("message"),
                "file": match.group("file"),
                "line": int(match.group("line")),
                "column": int(match.group("column")),
            }
        elif current is not None:
            message = str(current["message"])
            current["message"] = f"{message}\n{line}" if line else message
    finish()
    return tuple(diagnostics)


def map_diagnostic_paths(
    diagnostics: tuple[Diagnostic, ...],
    path_map: Mapping[str, str] | None,
) -> tuple[Diagnostic, ...]:
    """Rewrite staged physical paths to the caller's logical input names.

    Only exact staged-file matches are rewritten; package files may
    legitimately share basenames, and raw compiler output remains authoritative
    on the execution result.
    """
    if not path_map:
        return diagnostics
    return tuple(
        replace(item, file=path_map[item.file])
        if item.file is not None and item.file in path_map
        else item
        for item in diagnostics
    )


def error_diagnostic(message: str) -> Diagnostic:
    """Create an infrastructure diagnostic."""
    severity: Severity = "error"
    return Diagnostic(severity=severity, message=message)
