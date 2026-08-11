"""Verified slim toolchain derivatives for proof checking.

A slim toolchain is a separate, non-destructive materialization of an
installed Lean toolchain that keeps everything proof checking needs and drops
artifact classes used only by editors and native compilation. Lean v4.32
loads every ``.olean`` facet and per-module ``.ir`` data during ordinary
elaboration, so those stay; editor indexes (``.ilean``), static libraries
(``.a``), the bundled LLVM/clang, and ``src/`` are dropped.

Materialization hardlinks files where possible, so creating a slim toolchain
costs almost no additional disk. The saving is realized by pruning the
original Elan-managed copy afterwards, which callers must request explicitly.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .backends import LocalBackend
from .errors import ToolchainError
from .policies import ExecutionPolicy
from .serialization import write_json_atomic

SLIM_PROFILE = "check"
SLIM_MANIFEST_NAME = "slim-manifest.json"
SLIM_MANIFEST_SCHEMA = "lean-runtime-slim-toolchain/1"

_EXCLUDED_SUFFIXES = (".ilean", ".a")
_EXCLUDED_TOP_LEVEL_DIRS = ("src",)
_EXCLUDED_LIB_DIRS = ("clang", "libc")
_EXCLUDED_LIB_PREFIXES = ("libLLVM.", "libclang-cpp.")

# Each corpus entry must check successfully with the slim toolchain before it
# is trusted: ordinary elaboration, core tactics, decision procedures, the
# interpreter (#eval), metaprogramming, and Std imports.
CAPABILITY_CORPUS: tuple[tuple[str, str], ...] = (
    ("elaboration", "example : 2 + 2 = 4 := rfl\n"),
    (
        "tactics",
        "example (a b : Nat) : a + b + 0 = b + a := by simp [Nat.add_comm]\n"
        "example (x : Nat) (h : x > 2) : x ≥ 1 := by omega\n",
    ),
    ("decide", "example : (3 : Nat) < 5 := by decide\n"),
    ("interpreter", "#eval (List.range 10).map (· * 2) |>.foldl (· + ·) 0\n"),
    (
        "metaprogramming",
        "import Lean\n"
        'macro "slimProbeTac" : tactic => `(tactic| trivial)\n'
        "example : True := by slimProbeTac\n",
    ),
    ("std", "import Std\nexample : True := trivial\n"),
)


def is_excluded(relative: Path) -> bool:
    """Return whether one toolchain-relative file is outside the check profile."""
    if relative.suffix in _EXCLUDED_SUFFIXES:
        return True
    parts = relative.parts
    if parts and parts[0] in _EXCLUDED_TOP_LEVEL_DIRS:
        return True
    if len(parts) >= 2 and parts[0] == "lib":
        if parts[1] in _EXCLUDED_LIB_DIRS:
            return True
        if len(parts) == 2 and parts[1].startswith(_EXCLUDED_LIB_PREFIXES):
            return True
    return False


@dataclass(frozen=True, slots=True)
class SlimManifest:
    """Description of one materialized slim toolchain."""

    toolchain: str
    profile: str
    files: int
    bytes: int
    excluded_files: int
    excluded_bytes: int
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SLIM_MANIFEST_SCHEMA,
            "toolchain": self.toolchain,
            "profile": self.profile,
            "files": self.files,
            "bytes": self.bytes,
            "excluded_files": self.excluded_files,
            "excluded_bytes": self.excluded_bytes,
            "created_at": self.created_at,
        }

    @classmethod
    def load(cls, directory: Path) -> SlimManifest | None:
        path = directory / SLIM_MANIFEST_NAME
        if not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("schema") != SLIM_MANIFEST_SCHEMA:
            return None
        return cls(
            toolchain=str(value["toolchain"]),
            profile=str(value["profile"]),
            files=int(value["files"]),
            bytes=int(value["bytes"]),
            excluded_files=int(value["excluded_files"]),
            excluded_bytes=int(value["excluded_bytes"]),
            created_at=str(value["created_at"]),
        )


def materialize(
    source: Path, destination: Path, *, toolchain: str, created_at: str
) -> SlimManifest:
    """Materialize the check profile of one installed toolchain.

    Files are hardlinked when the filesystem allows it and copied otherwise.
    The source toolchain is never modified. An existing destination is
    replaced atomically only after the new tree is complete.
    """
    if not (source / "bin").is_dir():
        raise ToolchainError(f"not an installed Lean toolchain: {source}")
    staging = destination.parent / f".{destination.name}.staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    kept_files = 0
    kept_bytes = 0
    excluded_files = 0
    excluded_bytes = 0
    try:
        for root, _dirs, names in os.walk(source):
            for name in names:
                path = Path(root) / name
                relative = path.relative_to(source)
                size = path.lstat().st_size
                if is_excluded(relative):
                    excluded_files += 1
                    excluded_bytes += size
                    continue
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if path.is_symlink():
                    target.symlink_to(os.readlink(path))
                else:
                    try:
                        os.link(path, target)
                    except OSError:
                        shutil.copy2(path, target)
                kept_files += 1
                kept_bytes += size
        manifest = SlimManifest(
            toolchain=toolchain,
            profile=SLIM_PROFILE,
            files=kept_files,
            bytes=kept_bytes,
            excluded_files=excluded_files,
            excluded_bytes=excluded_bytes,
            created_at=created_at,
        )
        write_json_atomic(staging / SLIM_MANIFEST_NAME, manifest.to_dict())
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(staging, destination)
        return manifest
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def verify_capabilities(
    slim_dir: Path,
    *,
    environment: dict[str, str],
    backend: LocalBackend | None = None,
    timeout_seconds: float = 300,
) -> tuple[tuple[str, bool, str], ...]:
    """Run the capability corpus against a slim toolchain's own ``lean``.

    Returns one ``(name, ok, detail)`` row per corpus entry. Callers reject
    the materialization unless every row is ok.
    """
    lean = slim_dir / "bin" / "lean"
    if not lean.is_file():
        raise ToolchainError(f"slim toolchain has no lean executable: {slim_dir}")
    selected = backend or LocalBackend()
    results: list[tuple[str, bool, str]] = []
    with tempfile.TemporaryDirectory(prefix="slim-corpus-") as raw:
        for name, source in CAPABILITY_CORPUS:
            probe = Path(raw) / f"{name}.lean"
            probe.write_text(source, encoding="utf-8")
            outcome = selected.execute(
                [str(lean), str(probe)],
                cwd=Path(raw),
                environment=environment,
                policy=ExecutionPolicy(timeout_seconds=timeout_seconds),
                cancel=None,
            )
            detail = (outcome.stderr or outcome.stdout).strip()
            results.append((name, outcome.exit_code == 0, detail))
    return tuple(results)
