"""Writable Lake configuration views over borrowed dependency trees.

These are layout adapters, not an OS sandbox. Sources and library outputs are
symlinked, never copied. Callers must coordinate writes to shared build outputs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ._paths import link_directory
from ._transaction import publish_tree, staged_tree
from .errors import ProjectError
from .locking import FileLock
from .serialization import sha256_id, write_json_atomic


def _link(source: Path, target: Path) -> None:
    try:
        if os.name == "nt":
            if source.is_dir():
                link_directory(source, target)
            else:
                os.link(source, target)
        else:
            target.symlink_to(source, target_is_directory=source.is_dir())
    except OSError as exc:
        raise ProjectError(f"cannot borrow dependency entry {source}: {exc}") from exc


def dependency_view(source: Path, destination: Path) -> Path:
    """Own .lake/config and locks while borrowing source and .lake/build.

    Destination must be caller-owned; source may be read-only. The source
    identity and immediate entry inventory select a fresh view after changes.
    """
    source = source.resolve(strict=True)
    inventory = sorted(item.name for item in source.iterdir() if item.name != ".lake")
    lake_entries = [name for name in ("build", "packages") if (source / ".lake" / name).exists()]
    key = sha256_id(
        "dependency_view",
        {"source": str(source), "entries": inventory, "lake_entries": lake_entries},
    )
    target = destination / key
    with FileLock(destination / ".locks" / f"{key}.lock"):
        if not target.is_dir():
            with staged_tree(destination, None) as stage:
                for name in inventory:
                    item = source / name
                    _link(item, stage / name)
                (stage / ".lake").mkdir()
                for name in lake_entries:
                    _link(source / ".lake" / name, stage / ".lake" / name)
                publish_tree(stage, target)
    return target


def workspace_overrides(overrides: Path, destination: Path) -> Path:
    """Return an override manifest using configuration-isolated package roots."""
    document = json.loads(overrides.read_text(encoding="utf-8"))
    for entry in document["packages"]:
        if entry.get("type") == "path":
            source = Path(entry["dir"])
            # Managed build outputs must remain shared even on the first build.
            # Only generated state is created; authored files are never copied.
            (source / ".lake" / "build").mkdir(parents=True, exist_ok=True)
            entry["dir"] = str(dependency_view(source, destination / "packages"))
    key = sha256_id("view_overrides", document)
    output = destination / f"{key}.json"
    with FileLock(destination / ".locks" / f"{key}.lock"):
        if not output.is_file():
            write_json_atomic(output, document)
    return output
