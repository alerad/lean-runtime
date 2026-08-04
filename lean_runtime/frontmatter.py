"""Strict TOML metadata embedded at the start of standalone Lean files."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised by the Python 3.10 CI job
    import tomli as tomllib

from .errors import SpecificationError

_START = "-- /// lean-runtime"
_END = "-- ///"


@dataclass(frozen=True, slots=True)
class LeanFrontmatter:
    requires: tuple[str, ...] = ()
    toolchain: str | None = None
    lock: str | None = None


def parse_frontmatter(source: str) -> LeanFrontmatter | None:
    lines = source.splitlines()
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines) or lines[index].strip() != _START:
        if any(line.strip() == _START for line in lines[index + 1 :]):
            raise SpecificationError("lean-runtime frontmatter must precede Lean source")
        return None
    index += 1
    content: list[str] = []
    while index < len(lines) and lines[index].strip() != _END:
        line = lines[index]
        if not line.lstrip().startswith("--"):
            raise SpecificationError("lean-runtime frontmatter lines must be Lean comments")
        comment = line.lstrip()[2:]
        content.append(comment[1:] if comment.startswith(" ") else comment)
        index += 1
    if index >= len(lines):
        raise SpecificationError("lean-runtime frontmatter is missing its closing '-- ///'")
    try:
        parsed = tomllib.loads("\n".join(content))
    except tomllib.TOMLDecodeError as exc:
        raise SpecificationError(f"invalid lean-runtime frontmatter: {exc}") from exc
    unknown = set(parsed) - {"requires", "toolchain", "lock"}
    if unknown:
        raise SpecificationError(
            "unknown lean-runtime frontmatter field(s): " + ", ".join(sorted(unknown))
        )
    requires = parsed.get("requires", [])
    toolchain = parsed.get("toolchain")
    lock = parsed.get("lock")
    if not isinstance(requires, list) or not all(isinstance(item, str) for item in requires):
        raise SpecificationError("frontmatter 'requires' must be an array of strings")
    if toolchain is not None and not isinstance(toolchain, str):
        raise SpecificationError("frontmatter 'toolchain' must be a string")
    if lock is not None and not isinstance(lock, str):
        raise SpecificationError("frontmatter 'lock' must be a string")
    if lock is not None and requires:
        raise SpecificationError("frontmatter cannot combine 'lock' and 'requires'")
    if lock is not None and toolchain is not None:
        raise SpecificationError("frontmatter cannot combine 'lock' and 'toolchain'")
    if not requires and lock is None and toolchain is None:
        raise SpecificationError("lean-runtime frontmatter does not declare an execution context")
    return LeanFrontmatter(tuple(requires), toolchain, lock)


def load_frontmatter(path: str | Path) -> LeanFrontmatter | None:
    return parse_frontmatter(Path(path).read_text(encoding="utf-8"))
