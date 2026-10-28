"""Cooperative, operation-scoped storage growth budgets (not disk quotas)."""

from __future__ import annotations

import inspect
import os
import stat
import threading
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from functools import wraps
from pathlib import Path
from typing import Any, ParamSpec, TypeVar, cast

from ._paths import is_link
from .errors import EnvironmentError

P = ParamSpec("P")
R = TypeVar("R")


class StorageLimitExceeded(EnvironmentError):
    """Infrastructure refusal, never a Lean rejection."""

    def __init__(self, category: str, used: int, limit: int) -> None:
        self.category, self.used_bytes, self.limit_bytes = category, used, limit
        super().__init__(
            f"{category} storage growth {used} bytes exceeds {limit} bytes",
            reason_code="storage_limit_exceeded",
            retryable=False,
        )


@dataclass(frozen=True)
class StorageLimits:
    """Net new logical bytes per operation; existing files cost zero.

    Toolchains, dependency caches, and build outputs have independent limits.
    Hardlinks are counted once, symlinks are never followed. Limits are sampled
    and checked before publication; they are not an OS write or peak-space quota.
    """

    toolchain_bytes: int | None = None
    dependency_bytes: int | None = None
    build_bytes: int | None = None

    def __post_init__(self) -> None:
        for value in asdict(self).values():
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError("storage limits must be nonnegative integer byte counts")

    def to_dict(self) -> dict[str, int | None]:
        return asdict(self)


class StorageInventoryCancelled(EnvironmentError):
    """Cancellation while measuring storage, before a compiler verdict exists."""


def owned_bytes(roots: list[Path], *, cancel: threading.Event | None = None) -> int:
    """Fail closed, skip borrowed links and deduplicate overlapping roots/inodes."""
    seen: set[tuple[int, int]] = set()
    directories: set[tuple[int, int]] = set()
    total = 0
    pending = [str(root) for root in roots]
    while pending:
        if cancel is not None and cancel.is_set():
            raise StorageInventoryCancelled("storage inventory cancelled; no Lean verdict")
        path = pending.pop()
        try:
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode) or (os.name == "nt" and is_link(Path(path))):
                continue
            identity = (info.st_dev, info.st_ino)
            if stat.S_ISDIR(info.st_mode):
                if identity in directories:
                    continue
                directories.add(identity)
                # scandir avoids allocating/statting each Path several times per sample.
                with os.scandir(path) as entries:
                    for entry in entries:
                        if cancel is not None and cancel.is_set():
                            raise StorageInventoryCancelled(
                                "storage inventory cancelled; no Lean verdict"
                            )
                        try:
                            if entry.is_symlink():
                                continue
                            status = entry.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        if os.name == "nt" and is_link(Path(entry.path)):
                            continue
                        if stat.S_ISDIR(status.st_mode):
                            pending.append(entry.path)
                        elif stat.S_ISREG(status.st_mode):
                            key = (status.st_dev, status.st_ino)
                            if key not in seen:
                                seen.add(key)
                                total += status.st_size
            elif stat.S_ISREG(info.st_mode) and identity not in seen:
                seen.add(identity)
                total += info.st_size
        except FileNotFoundError:
            continue  # A concurrent stage rename/removal is not an unreadable tree.
    return total


_ACTIVE: ContextVar[tuple[StorageGuard, ...]] = ContextVar("storage_guards", default=())


class StorageGuard:
    def __init__(
        self,
        category: str,
        roots: list[Path] | Callable[[], list[Path]],
        limit: int,
        cancel: threading.Event | None = None,
        *,
        categories: tuple[str, ...] = (),
    ) -> None:
        self.category, self.roots, self.limit = category, roots, limit
        self.categories = (category, *categories)
        self.cancel = threading.Event()
        self.parent_cancel = cancel
        self.finished = threading.Event()
        self.failure: BaseException | None = None
        self.baseline = 0
        self.peak_growth_bytes = 0

    def check(self) -> None:
        if self.failure is not None:
            raise self.failure
        if self.parent_cancel is not None and self.parent_cancel.is_set():
            self.cancel.set()
        try:
            growth = max(
                0,
                owned_bytes(
                    self.roots() if callable(self.roots) else self.roots, cancel=self.parent_cancel
                )
                - self.baseline,
            )
            self.peak_growth_bytes = max(self.peak_growth_bytes, growth)
            if growth > self.limit:
                raise StorageLimitExceeded(self.category, growth, self.limit)
        except (OSError, EnvironmentError) as exc:
            self.failure = exc
            self.cancel.set()
            raise

    def _monitor(self) -> None:
        while not self.finished.wait(0.1):
            try:
                self.check()
            except (OSError, EnvironmentError):
                return

    def __enter__(self) -> StorageGuard:
        self.baseline = owned_bytes(
            self.roots() if callable(self.roots) else self.roots, cancel=self.parent_cancel
        )
        self.token = _ACTIVE.set((*_ACTIVE.get(), self))
        self.thread = threading.Thread(target=self._monitor, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.finished.set()
        self.thread.join()
        _ACTIVE.reset(self.token)
        # Preserve storage failure attribution even if cancellation surfaced first.
        self.check()


def active_scope_budget(roots: Callable[[], list[Path]], limit: int) -> bool:
    """Whether an equal-or-stricter dynamic scope already guards this operation."""
    return any(guard.roots is roots and guard.limit <= limit for guard in _ACTIVE.get())


def check_storage() -> None:
    """Cooperative checkpoint, especially before publishing staged data."""
    for guard in _ACTIVE.get():
        guard.check()


def budgeted(
    category: str, roots: Callable[..., list[Path]]
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Wrap an operation, forwarding cancellation without mutating the caller's event."""

    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        signature = inspect.signature(function)

        @wraps(function)
        def run(self: Any, *args: Any, **kwargs: Any) -> R:
            limits = getattr(self, "storage_limits", StorageLimits())
            limit = getattr(limits, f"{category}_bytes")
            if limit is None or any(category in guard.categories for guard in _ACTIVE.get()):
                return function(self, *args, **kwargs)
            bound = signature.bind(self, *args, **kwargs)
            bound.apply_defaults()
            with StorageGuard(
                category,
                roots(self, bound.arguments),
                limit,
                bound.arguments.get("cancel")
                or (_ACTIVE.get()[-1].cancel if _ACTIVE.get() else None),
            ) as guard:
                if "cancel" in signature.parameters:
                    bound.arguments["cancel"] = guard.cancel
                return function(*bound.args, **bound.kwargs)

        return cast(Callable[P, R], run)

    return decorate


def dependency_roots(home: Path) -> list[Path]:
    return [
        home / name
        for name in (
            "project-packages",
            "project-sources",
            "project-workspaces",
            "sources",
            "resolution",
            "acquisition",
            "environments",
            "programs",
            "oci",
            "cas",
            "declaration-indexes",
        )
    ]


class TransferBudget:
    """Thread-safe aggregate OCI payload reservations, including retries, excluding metadata."""

    def __init__(self, limit: int) -> None:
        self.limit, self.used = limit, 0
        self.lock = threading.Lock()

    def require(self, amount: int, *, consume: bool = False) -> None:
        from .errors import DownloadLimitExceeded

        with self.lock:
            if self.used + amount > self.limit:
                raise DownloadLimitExceeded(
                    f"OCI payload download requires {self.used + amount} bytes; limit {self.limit}"
                )
            if consume:
                self.used += amount


_TRANSFER: ContextVar[TransferBudget | None] = ContextVar("transfer_budget", default=None)


def check_transfer(amount: int, *, consume: bool = False) -> None:
    budget = _TRANSFER.get()
    if budget is not None:
        budget.require(amount, consume=consume)


def download_budgeted(function: Callable[P, R]) -> Callable[P, R]:
    @wraps(function)
    def run(self: Any, *args: Any, **kwargs: Any) -> R:
        limit = getattr(self, "max_download_bytes", None)
        if limit is None or _TRANSFER.get() is not None:
            return function(self, *args, **kwargs)
        token = _TRANSFER.set(TransferBudget(limit))
        try:
            return function(self, *args, **kwargs)
        finally:
            _TRANSFER.reset(token)

    return cast(Callable[P, R], run)


def preflight_storage(destination: Path, additional_bytes: int) -> None:
    """Refuse a known materialization before copying; no budget means no scan."""
    for guard in _ACTIVE.get():
        roots = guard.roots() if callable(guard.roots) else guard.roots
        scope_covers = getattr(guard.roots, "covers", None)
        if (callable(scope_covers) and scope_covers(destination)) or any(
            destination.absolute().is_relative_to(root.absolute()) for root in roots
        ):
            guard.check()
            used = max(0, owned_bytes(roots, cancel=guard.parent_cancel) - guard.baseline)
            if used + additional_bytes > guard.limit:
                raise StorageLimitExceeded(guard.category, used + additional_bytes, guard.limit)


def preflight_clone(source: Path, destination: Path) -> None:
    if _ACTIVE.get():
        preflight_storage(destination, owned_bytes([source]))
