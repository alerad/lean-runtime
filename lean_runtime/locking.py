"""Small cross-process file locks used for atomic store publication."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import TracebackType
from typing import BinaryIO

from .errors import EnvironmentError


class FileLock:
    def __init__(self, path: Path, timeout: float = 300) -> None:
        self.path = path
        self.timeout = timeout
        self._handle: BinaryIO | None = None

    def __enter__(self) -> FileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        started = time.monotonic()
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
                return self
            except OSError as error:
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
