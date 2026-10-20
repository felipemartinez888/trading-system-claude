"""Process entry point for `tsclaude app`: `run(settings) -> int`.

Startup order (each step fails closed and visibly):
1. take the single-instance lock on the data directory — two processes writing the same
   `state.db` would corrupt crash detection (the second would mark the first as crashed); a
   refused second launch touches neither the log file nor the store;
2. configure JSON logging (the per-launch token is redacted from every record);
3. bind the loopback socket *before* touching the event store, so "port in use" never writes a
   session to the log;
4. open the event store, verify the hash chain (failure locks monitoring but the UI still serves),
   open the app session (crash detection of the previous one happens here);
5. serve with uvicorn; open the browser once the server accepts connections.

Shutdown paths, all of which stop monitoring and record `session_stopped` before exiting:
- `POST /api/app/quit`: the session is closed first, then the server is asked to stop;
- SIGINT (Ctrl+C), SIGTERM and, on Windows, SIGBREAK (Ctrl+Break): handled by uvicorn, which
  stops gracefully; the session is closed after the server loop returns;
- SIGHUP (POSIX terminal closed): not handled by uvicorn; our handler requests a graceful stop;
- Windows console window closed, logoff or system shutdown: a console control handler closes the
  session within the few seconds Windows allows.
A hard kill cannot be handled; the next start detects it as a crash.

Exit codes: 0 clean, 1 unexpected error or session not closed cleanly, 2 event store unusable,
3 startup refused (port in use / already running).
"""

from __future__ import annotations

import contextlib
import logging
import signal
import socket
import sqlite3
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Iterator
from types import FrameType

import uvicorn

from tsclaude import __version__
from tsclaude.api.app import create_app
from tsclaude.api.context import AppContext
from tsclaude.api.process import InstanceLock, bind_loopback, dashboard_url
from tsclaude.api.security import TokenRedactionFilter
from tsclaude.contracts.timeutil import SystemClock
from tsclaude.runtime.logging import configure_logging
from tsclaude.runtime.settings import Settings
from tsclaude.store.eventstore import EventStore, EventStoreError

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_STORE = 2
EXIT_REFUSED = 3
GRACEFUL_SHUTDOWN_S = 5
BROWSER_WAIT_S = 30.0
LOCK_FILE = "tsclaude.lock"


def run(settings: Settings) -> int:
    # Lock before configuring logging: a refused second launch must not touch the running
    # instance's log file or event store.
    try:
        lock = InstanceLock(settings.data_dir / LOCK_FILE)
    except OSError as exc:
        _console(
            f"Trading System Claude is already running with data dir {settings.data_dir} "
            f"(or its lock file is in use: {exc}). Close the other instance first."
        )
        return EXIT_REFUSED
    try:
        configure_logging(settings.log_level, settings.logs_dir)
        return _run_locked(settings)
    finally:
        lock.release()


def _run_locked(settings: Settings) -> int:
    try:
        sock = bind_loopback(settings.host, settings.port)
    except OSError as exc:
        log.error(
            "could not bind the API port",
            extra={"host": settings.host, "port": settings.port, "error": str(exc)},
        )
        _console(
            f"Port {settings.port} on {settings.host} is not available ({exc}). "
            "Set TSC_PORT or use --port to choose another one."
        )
        return EXIT_REFUSED
    try:
        try:
            store = EventStore(settings.state_db_path)
        except (EventStoreError, sqlite3.Error, OSError) as exc:
            log.error("the event store cannot be opened", extra={"error": str(exc)})
            _console(f"The event store {settings.state_db_path} cannot be opened: {exc}")
            return EXIT_STORE
        try:
            return _serve(settings, store, sock)
        finally:
            store.close()
    finally:
        sock.close()


def _serve(settings: Settings, store: EventStore, sock: socket.socket) -> int:
    context = AppContext(
        store=store,
        clock=SystemClock(),
        app_version=__version__,
        port=settings.port,
        heartbeat_interval_s=settings.heartbeat_interval_s,
    )
    _install_token_redaction(context.token)
    context.start()

    app = create_app(context)
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_config=None,  # keep our JSON logging (configured above)
        access_log=False,
        loop="asyncio",  # Proactor on Windows, selector elsewhere
        ws="auto",
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
        server_header=False,
    )
    server = uvicorn.Server(config)

    def request_exit() -> None:
        server.should_exit = True  # polled by uvicorn's main loop

    context.set_shutdown_callback(request_exit)
    url = dashboard_url(settings.host, settings.port)
    if settings.open_browser:
        threading.Thread(
            target=_open_browser_when_ready, args=(server, url), name="open-browser", daemon=True
        ).start()
    log.info("serving dashboard", extra={"url": url})
    _console(f"Trading System Claude {__version__} is running at {url}  (Ctrl+C to quit)")

    exit_code = EXIT_OK
    reason = "interrupted"  # overwritten below unless something escapes (e.g. KeyboardInterrupt)
    try:
        with (
            _console_close_handler(lambda: context.close("console_closed")),
            _signal_handlers(server) as received,
        ):
            try:
                server.run(sockets=[sock])
            except SystemExit as exc:  # uvicorn exits like this on startup failure
                log.error("server failed to start", extra={"code": exc.code})
                exit_code, reason = EXIT_ERROR, "startup_failure"
            except Exception:
                log.exception("server stopped with an unexpected error")
                exit_code, reason = EXIT_ERROR, "error"
            else:
                if context.shutdown_reason is not None:
                    reason = context.shutdown_reason
                elif received:
                    reason = f"signal_{signal.Signals(received[0]).name}"
                else:
                    reason = "server_stopped"
    finally:
        # Records monitoring_stopped + session_stopped (no-op if quit already closed it).
        outcome = context.close(reason)
    if not outcome.clean and exit_code == EXIT_OK:
        exit_code = EXIT_ERROR
    log.info(
        "shutdown complete",
        extra={
            "reason": outcome.reason,
            "session_closed_cleanly": outcome.clean,
            "detail": outcome.detail,
            "exit_code": exit_code,
        },
    )
    return exit_code


# ---------------------------------------------------------------------- helpers
def _open_browser_when_ready(server: uvicorn.Server, url: str) -> None:
    deadline = time.monotonic() + BROWSER_WAIT_S
    while not server.started:
        if server.should_exit or time.monotonic() > deadline:
            return
        time.sleep(0.05)
    try:
        webbrowser.open(url, new=2)
    except Exception:
        log.warning("could not open the browser; open the URL manually", extra={"url": url})


def _install_token_redaction(token: str) -> None:
    redactor = TokenRedactionFilter(token)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)


# Signals uvicorn handles itself during `server.run` (and re-raises afterwards).
_UVICORN_SIGNALS = [signal.SIGINT, signal.SIGTERM]
if sys.platform == "win32":
    _UVICORN_SIGNALS.append(signal.SIGBREAK)
# Signals uvicorn ignores that must still end the session cleanly.
_EXTRA_SIGNALS: list[signal.Signals] = []
if sys.platform != "win32":
    _EXTRA_SIGNALS.append(signal.SIGHUP)


@contextlib.contextmanager
def _signal_handlers(server: uvicorn.Server) -> Iterator[list[int]]:
    """Install handlers that record the signal and request a graceful stop.

    - SIGINT/SIGTERM/SIGBREAK: uvicorn replaces these during `run()` and, after its graceful
      shutdown, re-raises the captured signal into the handler that was installed before it.
      Ours records it, so the re-raise neither kills the process (SIGTERM default) nor raises
      KeyboardInterrupt before the session is closed.
    - SIGHUP (POSIX, terminal closed): uvicorn does not handle it; ours asks the server to stop,
      so the session is closed instead of the process dying with it open.
    """
    received: list[int] = []
    if threading.current_thread() is not threading.main_thread():
        yield received
        return

    def on_signal(sig: int, _frame: FrameType | None) -> None:
        received.append(sig)
        server.should_exit = True  # polled by uvicorn's main loop

    previous = {sig: signal.signal(sig, on_signal) for sig in _UVICORN_SIGNALS + _EXTRA_SIGNALS}
    try:
        yield received
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _CLOSE_EVENTS = {2, 5, 6}  # CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT

    @contextlib.contextmanager
    def _console_close_handler(on_close: Callable[[], object]) -> Iterator[None]:
        """Closing the console window, logoff or shutdown terminate the process shortly after a
        control event that Python does not map to a signal. Record a clean stop when it happens.
        """
        handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def handler(event: int) -> bool:
            if event in _CLOSE_EVENTS:
                try:
                    on_close()
                except Exception:
                    log.exception("cleanup on console close failed")
                return True
            return False  # let Ctrl+C / Ctrl+Break reach the normal signal handlers

        callback = handler_type(handler)  # keep a reference while the handler is installed
        kernel32 = ctypes.windll.kernel32
        installed = bool(kernel32.SetConsoleCtrlHandler(callback, True))
        try:
            yield
        finally:
            if installed:
                kernel32.SetConsoleCtrlHandler(callback, False)

else:

    @contextlib.contextmanager
    def _console_close_handler(on_close: Callable[[], object]) -> Iterator[None]:
        """Only Windows needs this; a closed POSIX terminal sends SIGHUP (see _signal_handlers)."""
        del on_close
        yield


def _console(message: str) -> None:
    """Human-readable line for the launcher window (logs are JSON on stderr)."""
    with contextlib.suppress(OSError, ValueError):
        sys.stdout.write(message + "\n")
        sys.stdout.flush()
