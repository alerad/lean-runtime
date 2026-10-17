"""Portable, exact source inventories for composed development environments."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from ._paths import is_link
from ._process import run_git
from .errors import ProjectError
from .serialization import sha256_id

EXCLUDED = {".git", ".lake", ".venv", "node_modules", "__pycache__"}


def lean_name(value: str) -> str:
    import re

    return ".".join(
        part
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_']*", part)
        or (part.startswith("«") and part.endswith("»"))
        else f"«{part}»"
        for part in value.split(".")
    )


def raw_name(value: str) -> str:
    return ".".join(
        part[1:-1] if part.startswith("«") and part.endswith("»") else part
        for part in value.split(".")
    )


def inventory(
    root: Path,
    *,
    max_files: int = 20000,
    max_bytes: int = 200_000_000,
    exclude: tuple[str, ...] = (),
    git_policy: bool = True,
) -> dict[str, Any]:
    """Honor Git ignore/tracked semantics; refuse unsafe or omitted build inputs."""
    if not root.is_dir() or is_link(root):
        raise ProjectError(f"source root must be a real directory: {root}")
    candidates: list[Path] = []
    query = run_git("-C", str(root), "rev-parse", "--show-toplevel") if git_policy else None
    if query is not None and query.ok:
        listed = run_git(
            "-C",
            str(root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            ".",
        )
        if not listed.ok or listed.output_truncated:
            raise ProjectError("Git source listing failed or was truncated")
        candidates = [root / item for item in listed.stdout.split("\0") if item]
    else:
        for current, dirs, files in os.walk(root, followlinks=False):
            parent = Path(current)
            for name in dirs:
                if name not in EXCLUDED and is_link(parent / name):
                    raise ProjectError(f"source symlink/junction is unsupported: {parent / name}")
            dirs[:] = sorted(name for name in dirs if name not in EXCLUDED)
            candidates.extend(parent / name for name in sorted(files))
    entries: list[dict[str, Any]] = []
    size = 0
    for path in sorted(set(candidates)):
        rel = path.relative_to(root).as_posix()
        if any(part in EXCLUDED for part in path.relative_to(root).parts) or any(
            fnmatch.fnmatchcase(rel, pattern) for pattern in exclude
        ):
            continue
        if is_link(path):
            raise ProjectError(f"source links are unsupported: {rel}")
        if not path.exists():
            continue  # A tracked deletion is part of the captured working tree.
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ProjectError(f"unsupported source entry: {rel}")
        size += info.st_size
        if size > max_bytes or len(entries) >= max_files:
            raise ProjectError("source inventory exceeds the reviewed file/byte allowance")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append(
            {
                "path": rel,
                "sha256": digest,
                "bytes": info.st_size,
                "executable": bool(info.st_mode & 0o111),
            }
        )
    return {"source_id": sha256_id("snapshot", entries), "entries": entries, "bytes": size}


def validate_inventory(root: Path, expected: dict[str, Any]) -> None:
    # Stored snapshots are already policy-selected; do not reapply Git ignores.
    observed = inventory(
        root,
        max_bytes=max(expected["bytes"], 1),
        max_files=max(len(expected["entries"]), 1),
        git_policy=False,
    )
    if observed != expected:
        raise ProjectError("immutable snapshot contents changed; refusing reuse")


def module_index(metadata: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, str]:
    from ._relpath import safe_relative_posix

    result: dict[str, str] = {}
    libraries = metadata.get("lean_lib", [])
    for library in libraries:
        package_dir = safe_relative_posix(str(metadata.get("srcDir", ".")), allow_root=True)
        library_dir = safe_relative_posix(str(library.get("srcDir", ".")), allow_root=True)
        prefix = package_dir / library_dir
        roots = library.get("roots", [library["name"]])
        globs = library.get("globs", roots)
        if not isinstance(roots, list) or not isinstance(globs, list):
            raise ProjectError("invalid declarative library roots/globs")
        for item in entries:
            path = safe_relative_posix(item["path"])
            if (
                path.suffix != ".lean"
                or path.name == "lakefile.lean"
                or not path.is_relative_to(prefix)
            ):
                continue
            name = ".".join(path.relative_to(prefix).with_suffix("").parts)
            # Match Lake LeanLibConfig.isLocalModule: roots include descendants;
            # explicit globs add their own members. Executable-only Main is not
            # an exported library merely because it shares the same source directory.
            local = any(
                name == raw_name(root) or name.startswith(raw_name(root) + ".") for root in roots
            )
            for glob in globs:
                glob = raw_name(glob)
                if glob.endswith((".*", ".+")):
                    parent = glob[:-2]
                    local |= name.startswith(parent + ".") or (
                        glob.endswith(".*") and name == parent
                    )
                else:
                    local |= name == glob
            if not local:
                continue
            if name in result and result[name] != item["path"]:
                raise ProjectError(f"ambiguous source directories for {name}")
            result[name] = item["path"]
    return result


def write_lake_root(
    root: Path,
    *,
    name: str,
    toolchain: str,
    packages: list[dict[str, Any]],
    library: str | None = None,
) -> None:
    """Generate standard Lake files; all snapshot paths are machine-local generated state."""
    root.mkdir(parents=True, exist_ok=True)
    lines = [f"name = {json.dumps(name)}", 'version = "0.1.0"', "enableArtifactCache = false"]
    for entry in packages:
        lines += [
            "",
            "[[require]]",
            f"name = {json.dumps(raw_name(entry['name']))}",
            f"path = {json.dumps(entry['dir'])}",
        ]
    if library:
        lines.insert(3, "defaultTargets = " + json.dumps([library]))
        lines += ["", "[[lean_lib]]", f"name = {json.dumps(library)}"]
    (root / "lakefile.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (root / "lean-toolchain").write_text(toolchain + "\n", encoding="utf-8")
    (root / "lake-manifest.json").write_text(
        json.dumps({"version": "1.1.0", "packagesDir": ".lake/packages", "packages": packages}),
        encoding="utf-8",
    )
