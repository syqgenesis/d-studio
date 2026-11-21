""""Is this recording still being written?" -- an OS file lock in raw/.

The recorder holds an exclusive lock on ``raw/.recording.lock`` from start to
stop (:func:`acquire` / :meth:`RecordingLock.release`).  The OS drops the lock
when the process dies, so after a crash, power loss or a killed recorder the
lock is free again while ``capture.json`` still says "recording".

* packager: status != "completed" and :func:`is_live` is False -> the session
  ended abnormally and may be recovered; :func:`is_live` True -> refuse,
  someone is still recording into it.
* GUI: unfinished ``rec-*`` folders whose lock is free are offered for recovery.

Standard library only; Windows uses ``msvcrt.locking``, elsewhere ``fcntl.flock``.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

LOCK_NAME = ".recording.lock"

if sys.platform == "win32":  # pragma: no cover - exercised on Windows CI
    import msvcrt

    def _try_lock(fd: int) -> bool:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


def lock_path(raw_dir: str) -> str:
    return os.path.join(raw_dir, LOCK_NAME)


class RecordingLock:
    """An acquired lock; :meth:`release` (idempotent) when the recording ends."""

    def __init__(self, fd: int, path: str):
        self._fd: Optional[int] = fd
        self.path = path

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        _unlock(fd)
        try:
            os.close(fd)
        except OSError:
            pass

    def __enter__(self) -> "RecordingLock":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def acquire(raw_dir: str) -> Optional[RecordingLock]:
    """Lock ``raw_dir`` for this process; None if another live process holds it
    (or the folder cannot be written -- the caller decides whether that matters)."""
    path = lock_path(raw_dir)
    try:
        os.makedirs(raw_dir, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return None
    try:
        if os.fstat(fd).st_size < 1:
            os.write(fd, b"\0")  # msvcrt locks a byte range; make sure byte 0 exists
    except OSError:
        pass
    if not _try_lock(fd):
        os.close(fd)
        return None
    return RecordingLock(fd, path)


def is_live(raw_dir: str) -> bool:
    """True while some process holds the recording lock of ``raw_dir``.

    False when there is no lock file (recordings made before v0.2.1, or never
    started) or when the lock is free (finished or crashed).  Never raises.
    """
    path = lock_path(raw_dir)
    if not os.path.isfile(path):
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        if _try_lock(fd):
            _unlock(fd)
            return False
        return True
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
