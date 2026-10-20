"""Process-level helpers for `tsclaude app` that do not depend on FastAPI or uvicorn.

- `InstanceLock`: one app per data directory. Two processes on the same `state.db` would break
  crash detection (each would see the other's session as unclosed). The OS releases the lock if the
  process dies, so a crash never leaves a stale lock behind.
- `bind_loopback`: bind the listening socket ourselves, before touching the event store, so a busy
  port fails fast and writes nothing. `SO_REUSEADDR` is only set on POSIX (on Windows it would
  allow binding over another process's live listener).
"""

from __future__ import annotations

import contextlib
import socket
import sys
from pathlib import Path
from typing import IO


def dashboard_url(host: str, port: int) -> str:
    shown = f"[{host}]" if ":" in host else host
    return f"http://{shown}:{port}/"


def bind_loopback(host: str, port: int) -> socket.socket:
    """Bind the listening socket ourselves, exclusively, so a busy port fails fast."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if sys.platform != "win32":
            # POSIX: allow rebinding while old connections sit in TIME_WAIT (fast restart);
            # a port held by a live listener is still refused.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Windows: default semantics. They already refuse a port held by another listener and,
        # unlike SO_EXCLUSIVEADDRUSE, do not block a quick restart on TIME_WAIT connections.
        # (SO_REUSEADDR there would allow binding over a live listener.)
        sock.bind((host, port))
        sock.listen(128)
        sock.set_inheritable(False)
    except OSError:
        sock.close()
        raise
    return sock


class InstanceLock:
    """Exclusive, OS-released lock on a file (released automatically if the process dies)."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file: IO[bytes] | None = open(path, "a+b")  # noqa: SIM115 - held for process life
        try:
            _lock_file(self._file)
        except OSError:
            self._file.close()
            self._file = None
            raise

    def release(self) -> None:
        if self._file is None:
            return
        with contextlib.suppress(OSError):
            _unlock_file(self._file)
        self._file.close()
        self._file = None


if sys.platform == "win32":
    import msvcrt

    def _lock_file(handle: IO[bytes]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_file(handle: IO[bytes]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_file(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
