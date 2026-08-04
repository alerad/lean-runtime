"""Semantic comparison of exact Lean locks and environments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .lockfiles import EnvironmentLock, LockedPackage


@dataclass(frozen=True, slots=True)
class DiffEntry:
    path: str
    kind: str
    identity_effect: bool
    before: Any
    after: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "identity_effect": self.identity_effect,
            "before": self.before,
            "after": self.after,
        }


@dataclass(frozen=True, slots=True)
class ContextDiff:
    left: dict[str, Any]
    right: dict[str, Any]
    changes: tuple[DiffEntry, ...]

    @property
    def equal(self) -> bool:
        return not self.changes

    @property
    def identity_equal(self) -> bool:
        return not any(item.identity_effect for item in self.changes)

    @property
    def summary(self) -> str:
        changed = sum(item.kind == "changed" for item in self.changes)
        added = sum(item.kind == "added" for item in self.changes)
        removed = sum(item.kind == "removed" for item in self.changes)
        return f"{changed} changed, {added} added, {removed} removed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "equal": self.equal,
            "identity_equal": self.identity_equal,
            "summary": self.summary,
            "left": self.left,
            "right": self.right,
            "changes": [item.to_dict() for item in self.changes],
        }


def _package_fields(package: LockedPackage) -> dict[str, Any]:
    return {
        "url": package.url,
        "requested_revision": package.requested_revision,
        "revision": package.revision,
        "tree_hash": package.tree_hash,
        "source_id": package.source_id,
        "subdir": package.subdir,
        "root_module": package.root_module,
        "artifact_command": list(package.artifact_command),
        "inherited": package.inherited,
    }


def diff_locks(left: EnvironmentLock, right: EnvironmentLock) -> ContextDiff:
    changes: list[DiffEntry] = []
    for field in ("toolchain", "spec_digest", "root_lakefile", "root_module", "manifest"):
        before = getattr(left, field)
        after = getattr(right, field)
        if before != after:
            changes.append(DiffEntry(field, "changed", True, before, after))
    left_packages = {item.name: item for item in left.packages}
    right_packages = {item.name: item for item in right.packages}
    for name in sorted(left_packages.keys() | right_packages.keys()):
        before = left_packages.get(name)
        after = right_packages.get(name)
        if before is None:
            assert after is not None
            changes.append(
                DiffEntry(f"packages.{name}", "added", True, None, _package_fields(after))
            )
            continue
        if after is None:
            assert before is not None
            changes.append(
                DiffEntry(f"packages.{name}", "removed", True, _package_fields(before), None)
            )
            continue
        before_fields = _package_fields(before)
        after_fields = _package_fields(after)
        for field in before_fields:
            if before_fields[field] != after_fields[field]:
                changes.append(
                    DiffEntry(
                        f"packages.{name}.{field}",
                        "changed",
                        True,
                        before_fields[field],
                        after_fields[field],
                    )
                )
    return ContextDiff(
        {"kind": "lock", "lock_id": left.lock_id},
        {"kind": "lock", "lock_id": right.lock_id},
        tuple(changes),
    )
