"""Single-instance execution lock.

An OS-level lock on a dedicated file -- ``flock`` on Linux, ``msvcrt.locking``
on Windows.  The kernel releases it if the process is killed, so a crashed run
cannot wedge the schedule the way a stale PID file would.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

from .exceptions import LockError
from .logging_config import get_logger

logger = get_logger("lock")

if os.name == "nt":  # pragma: no cover - exercised only on Windows
    import msvcrt

    #: Windows locks byte ranges, and a locked byte cannot be read even through
    #: another handle.  Lock one far past the PID text so the holder stays
    #: readable in the error message.
    _LOCK_OFFSET = 1 << 20

    def _lock(handle: IO[str]) -> None:
        handle.seek(_LOCK_OFFSET)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle: IO[str]) -> None:
        handle.seek(_LOCK_OFFSET)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle: IO[str]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: IO[str]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def run_lock(lock_file: Path, *, enabled: bool = True) -> Iterator[None]:
    """Hold an exclusive lock for the duration of the block.

    Raises:
        LockError: If another PromoFlow process already holds the lock.
    """
    if not enabled:
        yield
        return

    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.touch(exist_ok=True)
    handle = lock_file.open("r+")
    try:
        try:
            _lock(handle)
        except OSError as exc:
            raise LockError(
                f"Another PromoFlow run is in progress (lock {lock_file}, holder pid "
                f"{_read_holder(lock_file)}). Refusing to start a second run."
            ) from exc

        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        logger.debug("Acquired run lock %s", lock_file)
        try:
            yield
        finally:
            _unlock(handle)
            logger.debug("Released run lock %s", lock_file)
    finally:
        handle.close()


def _read_holder(lock_file: Path) -> str:
    try:
        return lock_file.read_text().strip() or "unknown"
    except OSError:
        return "unknown"
