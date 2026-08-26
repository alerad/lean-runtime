"""Explicit, side-effect-free file context selection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .errors import ProjectNotFoundError, SpecificationError
from .frontmatter import LeanFrontmatter
from .projects import ProjectContext, discover_project

ContextKind = Literal["lock", "references", "toolchain", "project", "discovery"]


@dataclass(frozen=True, slots=True)
class FileContextResolution:
    kind: ContextKind
    explicit: LeanFrontmatter
    project: ProjectContext | None = None
    reasons: tuple[str, ...] = ()


def resolve_file_context(
    path: Path,
    explicit: LeanFrontmatter,
    *,
    discover: bool,
    standalone: bool = False,
) -> FileContextResolution:
    """Apply the public context precedence table without executing anything."""

    if explicit.lock is not None:
        return FileContextResolution("lock", explicit, reasons=("exact lock selected",))
    if explicit.requires:
        return FileContextResolution(
            "references", explicit, reasons=("explicit package references selected",)
        )
    if explicit.toolchain is not None:
        return FileContextResolution(
            "toolchain", explicit, reasons=("explicit toolchain selected",)
        )
    if standalone:
        if not discover:
            raise SpecificationError("standalone context selection requires discovery")
        return FileContextResolution(
            "discovery", explicit, reasons=("standalone context explicitly requested",)
        )
    try:
        project = discover_project(path)
    except ProjectNotFoundError:
        if not discover:
            raise SpecificationError(
                "the file has no explicit context or pinned Lake project"
            ) from None
        return FileContextResolution(
            "discovery",
            explicit,
            reasons=("no explicit context or pinned Lake project",),
        )
    ownership = project.owns_file(path)
    if ownership is False:
        if not discover:
            raise SpecificationError(
                "the file is beneath a pinned Lake project but is not owned by a declared target"
            )
        return FileContextResolution(
            "discovery",
            explicit,
            project=project,
            reasons=(
                "nearest pinned Lake project found but file is not owned by a declared target",
            ),
        )
    reason = (
        "nearest pinned Lake project owns the file"
        if ownership is True
        else "nearest pinned Lake project selected; target ownership is ambiguous"
    )
    return FileContextResolution(
        "project",
        explicit,
        project=project,
        reasons=(reason,),
    )
