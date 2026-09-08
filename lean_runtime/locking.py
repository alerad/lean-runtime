"""Small cross-process file locks used for atomic store publication."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO

from .errors import EnvironmentError


class FileLock:
    """One advisory cross-process lock.

    ``owner`` is a small JSON-serializable description of the acquiring
    operation; it is published while the lock is held (POSIX: inside the lock
    file, Windows: in a ``.owner`` sidecar, because a byte-locked file cannot
    be read by waiters) so that waiters can attribute their wait. ``on_wait`` is invoked at
    most once, on first contention, with the current holder's description (or
    ``None`` when it cannot be read).
    """

    def __init__(
        self,
        path: Path,
        timeout: float = 300,
        cancel: threading.Event | None = None,
        owner: dict[str, Any] | None = None,
        on_wait: Callable[[dict[str, Any] | None], None] | None = None,
    ) -> None:
        self.path = path
        self.timeout = timeout
        self.cancel = cancel
        self.owner = owner
        self.on_wait = on_wait
        self._handle: BinaryIO | None = None
        self._owner_written = False

    def holder(self) -> dict[str, Any] | None:
        """Best-effort description of the current lock holder."""
        try:
            value = json.loads(self._owner_source().read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _owner_source(self) -> Path:
        """Where the holder's description lives.

        POSIX writes it into the lock file itself. Windows mandatory byte locks
        make a locked file unreadable to waiters, so the description lives in a
        sidecar beside the lock instead.
        """
        if os.name == "nt":
            return self.path.with_name(self.path.name + ".owner")
        return self.path

    def _write_owner(self, handle: BinaryIO) -> None:
        if self.owner is None:
            return
        try:
            payload = json.dumps({"pid": os.getpid(), **self.owner}).encode("utf-8")
            if os.name == "nt":
                sidecar = self._owner_source()
                temporary = sidecar.with_name(f"{sidecar.name}.{os.getpid()}.tmp")
                temporary.write_bytes(payload)
                temporary.replace(sidecar)
            else:
                handle.seek(0)
                handle.truncate()
                handle.write(payload)
                handle.flush()
            self._owner_written = True
        except (OSError, TypeError, ValueError):
            self._owner_written = False

    def __enter__(self) -> FileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        started = time.monotonic()
        announced = False
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    if handle.tell() == 0:
                        handle.write(b"0")
                        handle.flush()
                    handle.seek(0)
                    getattr(msvcrt, "locking")(  # noqa: B009
                        handle.fileno(),
                        getattr(msvcrt, "LK_NBLCK"),  # noqa: B009
                        1,  # noqa: B009
                    )
                else:
                    import fcntl

                    getattr(fcntl, "flock")(  # noqa: B009
                        handle.fileno(),
                        getattr(fcntl, "LOCK_EX") | getattr(fcntl, "LOCK_NB"),  # noqa: B009
                    )
                self._handle = handle
                self._write_owner(handle)
                return self
            except OSError as error:
                if not announced:
                    announced = True
                    if self.on_wait is not None:
                        self.on_wait(self.holder())
                if self.cancel is not None and self.cancel.is_set():
                    handle.close()
                    raise EnvironmentError(
                        f"cancelled while waiting for store lock: {self.path}"
                    ) from error
                if time.monotonic() - started >= self.timeout:
                    handle.close()
                    raise EnvironmentError(
                        f"timed out waiting for store lock: {self.path}"
                    ) from error
                time.sleep(0.05)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        handle = self._handle
        if handle is None:
            return
        if self._owner_written:
            with contextlib.suppress(OSError):
                if os.name == "nt":
                    self._owner_source().unlink()
                else:
                    handle.seek(0)
                    handle.truncate()
                    handle.flush()
            self._owner_written = False
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            getattr(msvcrt, "locking")(  # noqa: B009
                handle.fileno(),
                getattr(msvcrt, "LK_UNLCK"),  # noqa: B009
                1,  # noqa: B009
            )
        else:
            import fcntl

            getattr(fcntl, "flock")(  # noqa: B009
                handle.fileno(),
                getattr(fcntl, "LOCK_UN"),  # noqa: B009
            )
        handle.close()
        self._handle = None


class LockPaths:
    """Every cross-process lock file below one store home.

    A lock only protects an invariant when every participant agrees on its
    location, so no component builds a lock path on its own: the store, the
    shared-project manager, the OCI caches and the garbage collectors all ask
    this registry. Lock files are transient and live apart from the data they
    guard, under ``<home>/.locks``.
    """

    def __init__(self, home: Path) -> None:
        self.root = home / ".locks"

    def _path(self, name: str) -> Path:
        return self.root / f"{name}.lock"

    # Content-addressed store objects.
    def environment(self, environment_id: str) -> Path:
        return self._path(environment_id)

    def environment_lock(self, lock_id: str) -> Path:
        return self._path(lock_id)

    def source(self, source_id: str) -> Path:
        return self._path(source_id)

    def program(self, program_id: str) -> Path:
        return self._path(program_id)

    def name(self, name: str) -> Path:
        return self._path(f"name-{name}")

    def oci_blob(self, hex_digest: str) -> Path:
        return self._path(f"oci-{hex_digest}")

    def cas_artifact(self, hex_digest: str) -> Path:
        return self._path(f"cas-{hex_digest}")

    def declaration_index(self, lock_id: str) -> Path:
        return self._path(f"declaration-index-{lock_id}")

    def toolchain(self, reference: str) -> Path:
        return self._path(f"toolchain-{reference}")

    # Shared Lake projects.
    def package(self, package_id: str) -> Path:
        """Ownership of one shared package tree (materialization and removal)."""
        return self._path(package_id)

    def package_build(self, package_id: str) -> Path:
        """Serializes builds that may write into one shared package tree."""
        return self._path(f"{package_id}-build")

    def project_source(self, source_id: str) -> Path:
        return self._path(source_id)

    def project_workspace(self, workspace_id: str) -> Path:
        return self._path(workspace_id)

    def project_seeds(self) -> Path:
        return self._path("project-seeds")

    # Disposable workspaces and in-flight staging trees.
    def workspace(self, path: Path) -> Path:
        identity = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
        return self._path(f"workspace-{identity}")

    def staging(self, nonce: str) -> Path:
        """Held by the process building the staging tree named by ``nonce``."""
        return self._path(f"staging-{nonce}")

    # Garbage collectors.
    def collector(self, name: str) -> Path:
        return self._path(f"{name}-gc")
