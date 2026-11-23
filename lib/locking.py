"""Cross-platform exclusive file lock.

Unix uses ``fcntl.flock``. Windows uses ``msvcrt.locking``. ``fcntl`` is
imported only on non-Windows interpreters, so ``import locking`` (and
therefore ``import fg``) succeeds on Windows Python.

The lock is advisory on Unix and a mandatory byte-range lock on Windows.
Both are tied to an open file handle and released when the ``with`` block
exits, including on error.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


@contextmanager
def file_lock(path: Path):
    """Hold an exclusive lock on ``path`` for the duration of the block.

    The file is created if needed. On Windows the lock covers the first
    byte, so an empty file is given a single NUL before the lock is taken.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")
    try:
        if os.name == "nt":
            _win_lock(fh)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                _win_unlock(fh)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def _win_lock(fh) -> None:
    fh.seek(0, os.SEEK_END)
    if fh.tell() == 0:
        fh.write(b"\0")
        fh.flush()
    fh.seek(0)
    # LK_LOCK gives up after about ten seconds. Spin on the non-blocking
    # variant so a long critical section does not raise.
    while True:
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            time.sleep(0.02)


def _win_unlock(fh) -> None:
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
