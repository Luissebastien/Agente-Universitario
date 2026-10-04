from __future__ import annotations

import os
from pathlib import Path

# OS-level advisory lock on one byte of a lock file. The OS releases it when
# the process dies, so a crash never leaves a stale lock behind. On Windows
# the release after a crash may take a moment ("depends upon available system
# resources" - LockFile docs). Nothing is ever written into the file.
if os.name == "nt":
    import msvcrt

    def _lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class SingleInstanceLock:
    """At most one scheduler process (daemon or one-shot) holds this lock, so
    max_concurrent_jobs = 1 holds across processes, not just within one."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self) -> bool:
        if self._fd is not None:
            return True
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT)  # never truncates
        try:
            _lock(fd)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            _unlock(self._fd)
        finally:
            os.close(self._fd)
            self._fd = None


def is_locked(path: str | Path) -> bool:
    """Non-blocking probe: True when another process holds the lock."""
    probe = SingleInstanceLock(path)
    if probe.try_acquire():
        probe.release()
        return False
    return True
