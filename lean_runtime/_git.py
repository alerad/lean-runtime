"""Internal portable Git commands and repository queries."""

from __future__ import annotations

from pathlib import Path

from ._process import git_command as git_command
from ._process import git_output, run_git


def git_tree_hash(path: Path) -> str | None:
    root = git_root(path)
    if root is None:
        return None
    try:
        relative = path.resolve().relative_to(root)
    except (OSError, ValueError):
        return None
    revision = "HEAD^{tree}" if not relative.parts else f"HEAD:{relative.as_posix()}"
    return git_output("-C", str(root), "rev-parse", revision) or None


def git_head(path: Path) -> str | None:
    if git_root(path) != path.resolve():
        return None
    return git_output("-C", str(path), "rev-parse", "HEAD")


def git_clean(path: Path) -> bool:
    if git_root(path) != path.resolve():
        return False
    return git_output("-C", str(path), "status", "--porcelain", "--untracked-files=normal") == ""


def git_has_commit(path: Path, revision: str) -> bool:
    if git_root(path) != path.resolve():
        return False
    return run_git("-C", str(path), "cat-file", "-e", f"{revision}^{{commit}}").ok


def git_remote(path: Path) -> str | None:
    if git_root(path) != path.resolve():
        return None
    return git_output("-C", str(path), "config", "--get", "remote.origin.url")


def git_root(path: Path) -> Path | None:
    top = git_output("-C", str(path), "rev-parse", "--show-toplevel")
    if not top:
        return None
    try:
        return Path(top).resolve()
    except OSError:
        return None
