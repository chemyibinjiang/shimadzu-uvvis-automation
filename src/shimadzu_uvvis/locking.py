"""Small cross-process file lock used around LabSolutions command exchange."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import BinaryIO


class FileLockTimeoutError(TimeoutError):
    """Raised when another controller process keeps the lock too long."""


class InterProcessFileLock:
    """Lock the first byte of a stable file on Windows or POSIX."""

    def __init__(
        self,
        path: str | Path,
        *,
        timeout: float = 5.0,
        poll_interval: float = 0.05,
    ) -> None:
        if timeout <= 0:
            raise ValueError("lock timeout must be greater than zero")
        if poll_interval <= 0:
            raise ValueError("lock poll interval must be greater than zero")
        self.path = Path(path)
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)
        self._handle: BinaryIO | None = None

    def __enter__(self) -> InterProcessFileLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b", buffering=0)
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()

        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._lock(handle)
                self._handle = handle
                return
            except OSError as exc:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise FileLockTimeoutError(
                        f"Timed out after {self.timeout:.1f}s waiting for {self.path}"
                    ) from exc
                time.sleep(self.poll_interval)

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            self._unlock(self._handle)
        finally:
            self._handle.close()
            self._handle = None

    @staticmethod
    def _lock(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
