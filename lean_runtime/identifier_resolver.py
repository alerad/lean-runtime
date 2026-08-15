"""Exact-workspace identifier suggestions derived from Lean `.ilean` indexes."""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .locking import FileLock

if TYPE_CHECKING:
    from .models import ExecutionResult
    from .projects import ProjectContext

_UNKNOWN = re.compile(r"unknown identifier ['‘`](?P<name>[^'’`]+)['’`]", re.IGNORECASE)


class IdentifierResolver:
    def __init__(self, home: Path) -> None:
        self.root = home / "identifier-indexes"
        self._memory: dict[str, dict[str, tuple[str, ...]]] = {}

    def suggestions(self, context: ProjectContext, result: ExecutionResult) -> tuple[str, ...]:
        unknown = tuple(
            dict.fromkeys(
                match.group("name")
                for diagnostic in result.diagnostics
                for match in _UNKNOWN.finditer(diagnostic.message)
            )
        )
        if not unknown:
            return ()
        workspace = context.provenance().workspace_digest.removeprefix("sha256:")
        index = self._index(context, workspace)
        hints: list[str] = []
        for requested in unknown:
            leaf = requested.rsplit(".", 1)[-1]
            matches = difflib.get_close_matches(leaf, index, n=3, cutoff=0.72)
            candidates = tuple(
                dict.fromkeys(name for match in matches for name in index.get(match, ()))
            )[:3]
            if candidates:
                hints.append(
                    f"Unknown `{requested}`; did you mean "
                    + ", ".join(f"`{item}`" for item in candidates)
                    + "?"
                )
        return tuple(hints)

    def _index(self, context: ProjectContext, workspace: str) -> dict[str, tuple[str, ...]]:
        if workspace in self._memory:
            return self._memory[workspace]
        path = self.root / f"{workspace}.json"
        with FileLock(self.root / f"{workspace}.lock"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                index = {str(key): tuple(str(item) for item in value) for key, value in raw.items()}
            except (OSError, json.JSONDecodeError, AttributeError, TypeError):
                mutable: dict[str, list[str]] = {}
                for ilean in self._ilean_files(context):
                    try:
                        value: dict[str, Any] = json.loads(ilean.read_text(encoding="utf-8"))
                        declarations = value.get("decls", {})
                    except (OSError, json.JSONDecodeError, AttributeError):
                        continue
                    if not isinstance(declarations, dict):
                        continue
                    for declaration in declarations:
                        name = str(declaration)
                        leaf = name.rsplit(".", 1)[-1]
                        if not leaf.startswith("_aux"):
                            mutable.setdefault(leaf, []).append(name)
                index = {key: tuple(dict.fromkeys(value)) for key, value in mutable.items()}
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(index, separators=(",", ":")) + "\n", encoding="utf-8")
        self._memory[workspace] = index
        return index

    @staticmethod
    def _ilean_files(context: ProjectContext) -> tuple[Path, ...]:
        roots = [context.root / ".lake" / "build" / "lib" / "lean"]
        manifest = context.current_manifest()
        try:
            value = json.loads(manifest.read_text(encoding="utf-8")) if manifest else {}
            packages_dir = context.root / str(value.get("packagesDir", ".lake/packages"))
            packages = value.get("packages", [])
            for package in packages:
                if isinstance(package, dict) and isinstance(package.get("name"), str):
                    roots.append(
                        packages_dir / package["name"] / ".lake" / "build" / "lib" / "lean"
                    )
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        return tuple(file for root in roots for file in root.rglob("*.ilean"))
