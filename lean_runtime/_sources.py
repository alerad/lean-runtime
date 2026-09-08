"""Internal verification of locked package sources against their Git identity."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

from ._archive import safe_name
from ._process import git_output, run_git
from .errors import EnvironmentError
from .lake import ROOT_MODULE
from .lockfiles import EnvironmentLock, LockedPackage
from .oci_protocol import json_object as _json_object
from .serialization import canonical_json_bytes

SOURCE_TREE_INVENTORY = ".lean-runtime-source-tree.json"


def git_object_id(kind: str, data: bytes) -> str:
    framed = f"{kind} {len(data)}\0".encode() + data
    return hashlib.sha1(framed, usedforsecurity=False).hexdigest()  # noqa: S324


def matches_normalized(data: bytes, object_id: str) -> bool:
    """Accept a CRLF working file whose LF-normalized blob is the recorded one.

    Git with ``core.autocrlf`` (the Git for Windows default) stores text blobs
    with LF endings while checking them out with CRLF, so the on-disk bytes of
    an untouched file legitimately hash differently from its recorded object.
    """
    if b"\r\n" not in data:
        return False
    return git_object_id("blob", data.replace(b"\r\n", b"\n")) == object_id


def source_tree_inventory(root: Path, package: LockedPackage) -> bytes:
    process = run_git("-C", str(root), "ls-tree", "-rz", "--full-tree", "HEAD")
    if not process.ok:
        raise EnvironmentError(f"could not inventory package source: {package.name}")
    if process.output_truncated:
        raise EnvironmentError(
            f"package source inventory exceeds the Git output limit: {package.name}"
        )
    entries: list[dict[str, str]] = []
    for record in process.stdout.encode("utf-8", errors="surrogateescape").split(b"\0"):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            mode, kind, object_id = header.decode("ascii").split(" ")
            path = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise EnvironmentError(f"package has an unsupported Git tree: {package.name}") from exc
        safe_name(path)
        if path == SOURCE_TREE_INVENTORY:
            raise EnvironmentError(
                f"package uses reserved runtime path {SOURCE_TREE_INVENTORY!r}: {package.name}"
            )
        supported = (kind == "blob" and mode in {"100644", "100755", "120000"}) or (
            kind == "commit" and mode == "160000"
        )
        if not supported:
            raise EnvironmentError(
                f"package has an unsupported Git tree entry {path!r}: {package.name}"
            )
        entries.append({"path": path, "mode": mode, "object_id": object_id})
    return canonical_json_bytes(
        {
            "schema": "lean-runtime-source-tree/1",
            "revision": package.revision,
            "tree_hash": package.tree_hash,
            "entries": entries,
        }
    )


def inventory_tree_id(entries: list[dict[str, str]]) -> str:
    root: dict[str, Any] = {}
    for entry in entries:
        parts = PurePosixPath(entry["path"]).parts
        node = root
        for part in parts[:-1]:
            existing = node.setdefault(part, {})
            if not isinstance(existing, dict):
                raise EnvironmentError("source tree inventory has conflicting paths")
            node = existing
        if parts[-1] in node:
            raise EnvironmentError("source tree inventory has duplicate paths")
        node[parts[-1]] = (entry["mode"], entry["object_id"])

    def tree_id(node: dict[str, Any]) -> str:
        records: list[tuple[bytes, bytes]] = []
        for name, value in node.items():
            raw_name = name.encode("utf-8")
            if isinstance(value, dict):
                mode = "40000"
                object_id = tree_id(value)
                sort_key = raw_name + b"/"
            else:
                mode, object_id = value
                sort_key = raw_name + b"\0"
            record = mode.encode("ascii") + b" " + raw_name + b"\0" + bytes.fromhex(object_id)
            records.append((sort_key, record))
        return git_object_id("tree", b"".join(record for _, record in sorted(records)))

    return tree_id(root)


def verify_source_tree_inventory(root: Path, package: LockedPackage, path: Path) -> None:
    value = _json_object(path.read_bytes(), f"package {package.name} source tree inventory")
    if (
        value.get("schema") != "lean-runtime-source-tree/1"
        or value.get("revision") != package.revision
        or value.get("tree_hash") != package.tree_hash
        or not isinstance(value.get("entries"), list)
    ):
        raise EnvironmentError(f"bundled package source inventory mismatch: {package.name}")
    entries: list[dict[str, str]] = []
    for raw in value["entries"]:
        if not isinstance(raw, dict) or set(raw) != {"path", "mode", "object_id"}:
            raise EnvironmentError(f"bundled package source inventory is invalid: {package.name}")
        entry = {key: raw[key] for key in ("path", "mode", "object_id")}
        if not all(isinstance(item, str) for item in entry.values()):
            raise EnvironmentError(f"bundled package source inventory is invalid: {package.name}")
        source_name = entry["path"]
        safe_name(source_name)
        mode = entry["mode"]
        object_id = entry["object_id"]
        if (
            mode not in {"100644", "100755", "120000", "160000"}
            or re.fullmatch(r"[0-9a-f]{40}", object_id) is None
        ):
            raise EnvironmentError(f"bundled package source inventory is invalid: {package.name}")
        source = root.joinpath(*PurePosixPath(source_name).parts)
        if mode == "160000":
            if not source.is_dir():
                raise EnvironmentError(f"bundled package Git link mismatch: {package.name}")
            entries.append(entry)
            continue
        if mode == "120000":
            if not source.is_symlink():
                raise EnvironmentError(f"bundled package source mismatch: {package.name}")
            data = os.readlink(source).encode("utf-8")
        else:
            if not source.is_file() or source.is_symlink():
                raise EnvironmentError(f"bundled package source mismatch: {package.name}")
            executable = bool(source.stat().st_mode & 0o111)
            if executable != (mode == "100755"):
                raise EnvironmentError(f"bundled package source mode mismatch: {package.name}")
            data = source.read_bytes()
        if git_object_id("blob", data) != object_id and not matches_normalized(data, object_id):
            raise EnvironmentError(f"bundled package source mismatch: {package.name}")
        entries.append(entry)
    if inventory_tree_id(entries) != package.tree_hash:
        raise EnvironmentError(f"bundled package source tree mismatch: {package.name}")


def verify_package(root: Path, package: LockedPackage) -> None:
    marker = root / ".lean-runtime-source.json"
    if not marker.is_file():
        raise EnvironmentError(f"bundled package source marker is missing: {package.name}")
    marker_value = _json_object(marker.read_bytes(), f"package {package.name} source marker")
    expected_marker = {
        "source_id": package.source_id,
        "url": package.url,
        "revision": package.revision,
        "tree_hash": package.tree_hash,
    }
    if any(marker_value.get(key) != value for key, value in expected_marker.items()):
        raise EnvironmentError(f"bundled package source marker mismatch: {package.name}")
    content_hash = marker_value.get("content_hash")
    if (
        not isinstance(content_hash, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", content_hash) is None
    ):
        raise EnvironmentError(f"bundled package source marker mismatch: {package.name}")
    inventory = root / SOURCE_TREE_INVENTORY
    if inventory.is_file() and not (root / ".git").exists():
        verify_source_tree_inventory(root, package, inventory)
        return
    observed: list[str] = []
    for revision in ("HEAD", "HEAD^{tree}"):
        value = git_output("-C", str(root), "rev-parse", revision)
        if value is None:
            raise EnvironmentError(f"bundled package Git metadata is invalid: {package.name}")
        observed.append(value.lower())
    if observed != [package.revision.lower(), package.tree_hash.lower()]:
        raise EnvironmentError(f"bundled package source mismatch: {package.name}")
    status = run_git(
        "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all", "--ignored=no"
    )
    if not status.ok:
        raise EnvironmentError(f"bundled package Git metadata is invalid: {package.name}")
    changes = [
        line
        for line in status.stdout.splitlines()
        if line and line != "?? .lean-runtime-source.json" and not line.startswith("?? .lake/")
    ]
    if changes:
        raise EnvironmentError(f"bundled package checked-out content mismatch: {package.name}")


def verify_workspace_lock(workspace: Path, lock: EnvironmentLock) -> None:
    expected_text = {
        "lean-toolchain": lock.toolchain + "\n",
        "lakefile.toml": lock.root_lakefile,
        f"{ROOT_MODULE}.lean": lock.root_module,
    }
    for relative, expected in expected_text.items():
        path = workspace / relative
        if not path.is_file() or path.read_text(encoding="utf-8") != expected:
            raise EnvironmentError(f"bundled root workspace does not match lock: {relative}")
    manifest_path = workspace / "lake-manifest.json"
    if (
        not manifest_path.is_file()
        or _json_object(manifest_path.read_bytes(), "root Lake manifest") != lock.manifest
    ):
        raise EnvironmentError("bundled root Lake manifest does not match lock")
    if not (workspace / ".lake" / "build").is_dir():
        raise EnvironmentError("bundled root build artifacts are missing")
