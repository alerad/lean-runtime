"""Internal staged publication of directory trees.

Every store object is published the same way: acquire the ownership lock for
the object, validate what already exists, build a uniquely named staging tree
whose ownership is held by a lock for as long as the build runs, verify it,
and commit it with one rename. Cleanup of what the commit displaced happens
after the commit and never feeds back into rollback.

:func:`staged_tree` owns the staging tree, :func:`publish_tree` commits it and
:func:`abandoned_staging` finds staging trees whose builder is gone, which is
the only kind repair may delete.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext, suppress
from pathlib import Path

from ._paths import remove_tree
from .errors import EnvironmentError
from .locking import FileLock, LockPaths

STAGING_PREFIX = ".staging-"
TRASH_PREFIX = ".trash-"
# Mathlib artifacts sit over 150 characters below a workspace; keep the stage
# name short so Windows' legacy MAX_PATH budget is spent on the artifacts.
_NONCE_LENGTH = 12


def _nonce() -> str:
    return uuid.uuid4().hex[:_NONCE_LENGTH]


@contextmanager
def staged_tree(
    parent: Path, locks: LockPaths | None, *, prefix: str = STAGING_PREFIX
) -> Iterator[Path]:
    """Yield an empty, uniquely named staging directory owned by this process.

    With a lock registry, the staging lock is held until the block exits so a
    concurrent repair can tell this tree apart from an abandoned one; without
    one (trees outside any store) the name is merely unique. Whatever remains
    of the tree when the block exits, whether the block committed it
    elsewhere or failed, is removed.
    """
    parent.mkdir(parents=True, exist_ok=True)
    nonce = _nonce()
    path = parent / f"{prefix}{nonce}"
    ownership = FileLock(locks.staging(nonce), timeout=0) if locks is not None else nullcontext()
    with ownership:
        path.mkdir()
        try:
            yield path
        finally:
            # A failed Git checkout can leave locked pack files on Windows;
            # cleanup must not mask the actionable error being raised.
            if path.exists():
                with suppress(OSError):
                    remove_tree(path)


def publish_tree(staging: Path, destination: Path, *, replace: bool = False) -> bool:
    """Commit ``staging`` at ``destination`` with a rename.

    Returns whether the staging tree became the destination. When the
    destination already exists and ``replace`` is false, the staging tree is
    left for the caller's cleanup and ``False`` is returned: a concurrent
    publisher won the race and the object is content-addressed, so both
    copies are equivalent.

    With ``replace``, the existing tree is moved aside first and moved back if
    the commit rename fails, so the destination is always either the old or
    the new complete tree. The displaced tree is deleted after the commit;
    a failure there is not a publication failure.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not replace:
        if destination.exists():
            return False
        try:
            staging.replace(destination)
        except OSError as error:
            if destination.exists():
                return False
            raise EnvironmentError(f"could not publish {destination.name}: {error}") from error
        return True
    displaced: Path | None = None
    if destination.exists():
        displaced = destination.with_name(f"{TRASH_PREFIX}{destination.name}-{_nonce()}")
        destination.replace(displaced)
    try:
        staging.replace(destination)
    except OSError as error:
        if displaced is not None:
            with suppress(OSError):
                displaced.replace(destination)
        raise EnvironmentError(f"could not publish {destination.name}: {error}") from error
    if displaced is not None:
        with suppress(OSError):
            remove_tree(displaced)
    return True


def abandoned_staging(
    parent: Path, locks: LockPaths, *, prefix: str = STAGING_PREFIX
) -> list[Path]:
    """Staging trees below ``parent`` that no live process owns.

    A tree whose staging lock can be taken has no builder behind it. Trees
    written by releases that predate staging locks are unowned by definition
    and are reported too.
    """
    abandoned: list[Path] = []
    for path in sorted(parent.glob(f"{prefix}*")):
        if not path.is_dir():
            continue
        nonce = path.name[len(prefix) :]
        try:
            with FileLock(locks.staging(nonce), timeout=0):
                abandoned.append(path)
        except EnvironmentError:
            continue
    return abandoned


def remove_abandoned_staging(
    parent: Path, locks: LockPaths, *, prefix: str = STAGING_PREFIX
) -> int:
    """Delete unowned staging trees below ``parent``; returns how many."""
    removed = 0
    for path in abandoned_staging(parent, locks, prefix=prefix):
        nonce = path.name[len(prefix) :]
        # Re-take the lock across the removal so a builder that starts now
        # with the same nonce (astronomically unlikely) cannot be torn down.
        try:
            with FileLock(locks.staging(nonce), timeout=0):
                if path.exists():
                    remove_tree(path)
                    removed += 1
        except EnvironmentError:
            continue
    return removed
