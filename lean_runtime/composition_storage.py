"""Measure only mutable closure paths and newly created shared store objects.

Unrelated retained dependency trees are not writable inputs to this operation.
New staging/object names are observed on every sample, including pre-publication
cooperative checkpoints. This is a growth guard, never a filesystem quota.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runtime import Runtime
    from .shared_projects import SharedProjectWorkspace


class CompositionStorageScope:
    def __init__(self, runtime: Runtime, root: Path, seeds: dict[str, Path]) -> None:
        self.runtime = runtime
        self.root = root
        self.parents = [
            runtime.home / name
            for name in ("project-packages", "project-workspaces", "project-sources")
        ]
        self.existing = {
            parent: {path.name for path in parent.iterdir()} if parent.exists() else set()
            for parent in self.parents
        }
        self.mutable: list[Path] = []
        for path in seeds.values():
            self._include_package(path)

    def _include_package(self, path: Path) -> None:
        owner = (self.runtime.home / "project-packages").resolve()
        path = path.resolve()
        if path.is_relative_to(owner):
            relative = path.relative_to(owner)
            if relative.parts:
                # Include the whole selected Git checkout, including nested package
                # source directories and custom generated targets outside .lake.
                self.mutable.append(owner / relative.parts[0])

    def include(self, workspace: SharedProjectWorkspace) -> None:
        self.mutable.append(workspace.root)
        for entry in json.loads(workspace.overrides_file.read_text())["packages"]:
            self._include_package(Path(entry["dir"]))

    def covers(self, destination: Path) -> bool:
        destination = destination.absolute()
        if any(destination.is_relative_to(root.absolute()) for root in [self.root, *self.mutable]):
            return True
        for parent in self.parents:
            if destination.is_relative_to(parent.absolute()):
                relative = destination.relative_to(parent.absolute())
                return bool(relative.parts and relative.parts[0] not in self.existing[parent])
        return False

    def __call__(self) -> list[Path]:
        roots = [self.root, *self.mutable]
        for parent in self.parents:
            if parent.exists():
                roots += [
                    path for path in parent.iterdir() if path.name not in self.existing[parent]
                ]
        return roots
