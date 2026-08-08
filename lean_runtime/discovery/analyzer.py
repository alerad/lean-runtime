"""Conservative static evidence extraction for Lean source."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..errors import LeanRuntimeError
from ..frontmatter import parse_frontmatter

_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*")
_IMPORT = re.compile(r"^\s*import\s+(.+?)\s*$")
_QUALIFIED = re.compile(r"\b[A-Z][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)+\b")
_CORE_ROOTS = frozenset({"Init", "Lean", "Std"})


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    """Cheap, non-authoritative evidence extracted from a source snippet."""

    imports: tuple[str, ...] = field(default_factory=tuple)
    root_namespaces: tuple[str, ...] = field(default_factory=tuple)
    qualified_identifiers: tuple[str, ...] = field(default_factory=tuple)
    package_hints: tuple[str, ...] = field(default_factory=tuple)
    toolchain_hint: str | None = None
    lock_hint: str | None = None
    appears_core_only: bool = False
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "imports": list(self.imports),
            "root_namespaces": list(self.root_namespaces),
            "qualified_identifiers": list(self.qualified_identifiers),
            "package_hints": list(self.package_hints),
            "toolchain_hint": self.toolchain_hint,
            "lock_hint": self.lock_hint,
            "appears_core_only": self.appears_core_only,
            "warnings": list(self.warnings),
        }


def _without_comments_and_strings(source: str) -> str:
    """Blank comments/strings while retaining newlines and character positions."""

    output: list[str] = []
    index = 0
    block_depth = 0
    in_string = False
    escaped = False
    while index < len(source):
        char = source[index]
        pair = source[index : index + 2]
        if block_depth:
            if pair == "/-":
                block_depth += 1
                output.extend("  ")
                index += 2
            elif pair == "-/":
                block_depth -= 1
                output.extend("  ")
                index += 2
            else:
                output.append("\n" if char == "\n" else " ")
                index += 1
            continue
        if in_string:
            output.append("\n" if char == "\n" else " ")
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if pair == "--":
            while index < len(source) and source[index] != "\n":
                output.append(" ")
                index += 1
            continue
        if pair == "/-":
            block_depth = 1
            output.extend("  ")
            index += 2
            continue
        if char == '"':
            in_string = True
            output.append(" ")
            index += 1
            continue
        output.append(char)
        index += 1
    return "".join(output)


def analyze_source(source: str) -> SourceEvidence:
    """Extract useful evidence without parsing or executing Lean."""

    if not isinstance(source, str):
        raise TypeError("source must be a string")
    warnings: list[str] = []
    package_hints: tuple[str, ...] = ()
    toolchain_hint: str | None = None
    lock_hint: str | None = None
    try:
        frontmatter = parse_frontmatter(source)
    except LeanRuntimeError as exc:
        warnings.append(f"invalid lean-runtime frontmatter: {exc}")
    else:
        if frontmatter is not None:
            package_hints = tuple(dict.fromkeys(frontmatter.requires))
            toolchain_hint = frontmatter.toolchain
            lock_hint = frontmatter.lock

    cleaned = _without_comments_and_strings(source)
    imports: list[str] = []
    for line_number, line in enumerate(cleaned.splitlines(), start=1):
        match = _IMPORT.match(line)
        if match is None:
            continue
        tokens = match.group(1).split()
        if not tokens:
            warnings.append(f"empty import at line {line_number}")
            continue
        for token in tokens:
            if _MODULE.fullmatch(token) is None:
                warnings.append(f"unrecognized import token {token!r} at line {line_number}")
            elif token not in imports:
                imports.append(token)

    roots = tuple(dict.fromkeys(item.split(".", 1)[0] for item in imports))
    identifiers = tuple(
        dict.fromkeys(
            token
            for token in _QUALIFIED.findall(cleaned)
            if token not in imports and not token.startswith("lean.runtime")
        )
    )
    appears_core_only = (
        lock_hint is None and not package_hints and all(root in _CORE_ROOTS for root in roots)
    )
    if "\x00" in source:
        warnings.append("source contains a NUL byte")
    return SourceEvidence(
        imports=tuple(imports),
        root_namespaces=roots,
        qualified_identifiers=identifiers,
        package_hints=package_hints,
        toolchain_hint=toolchain_hint,
        lock_hint=lock_hint,
        appears_core_only=appears_core_only,
        warnings=tuple(warnings),
    )
