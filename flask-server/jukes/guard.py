"""Single-owner guard: the download coordinator and cache accounting assume one process."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

_held: dict[str, int] = {}


class AlreadyRunning(RuntimeError):
    """Another process already owns this state directory."""


def acquire_exclusive(database_path: str | os.PathLike[str]) -> None:
    """Hold an advisory lock next to the database for the life of the process.

    Raises ``AlreadyRunning`` if another process holds it (e.g. a second worker
    or a second container sharing the volume), instead of corrupting accounting.
    """
    path = Path(str(database_path) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path)
    if key in _held:
        return
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise AlreadyRunning("another JUKES process owns this database") from None
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    _held[key] = fd


def release_exclusive(database_path: str | os.PathLike[str]) -> None:
    fd = _held.pop(str(Path(str(database_path) + ".lock")), None)
    if fd is not None:
        os.close(fd)
