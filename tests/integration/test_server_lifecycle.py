"""`server.run` end to end on a loopback port (no external network): start, serve, quit cleanly.

These run uvicorn in a worker thread, so signal handling is skipped (uvicorn and the server only
install signal handlers on the main thread); the quit path is driven through `POST /api/app/quit`.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import logging
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from api_testkit import TOKEN
from m1t2_fakes import FakeFactory

from tsclaude.api import context as context_module
from tsclaude.api import server
from tsclaude.api.process import bind_loopback
from tsclaude.contracts.events import EventType
from tsclaude.runtime.settings import Settings
from tsclaude.store.eventstore import EventStore

_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _free_port() -> int:
    sock = bind_loopback("127.0.0.1", 0)
    try:
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _request(url: str, *, method: str = "GET", token: str | None = None) -> tuple[int, Any]:
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["X-TSC-Token"] = token
    data = b"" if method == "POST" else None
    req = urllib.request.Request(url, method=method, headers=headers, data=data)
    try:
        with _NO_PROXY.open(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _wait_until_healthy(base: str, thread: threading.Thread, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not thread.is_alive():
            raise AssertionError("server thread exited during startup")
        try:
            if _request(f"{base}/api/health")[0] == 200:
                return
        except OSError:
            pass
        time.sleep(0.05)
    raise AssertionError("server did not become healthy in time")


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """`run()` reconfigures the root logger; put pytest's handlers back afterwards."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    for handler in root.handlers[:]:
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)


def _settings(tmp_path: Path, port: int) -> Settings:
    return Settings(
        data_dir=tmp_path / "data", port=port, open_browser=False, heartbeat_interval_s=0.2
    )


def test_run_serves_and_quits_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(context_module, "new_token", lambda: TOKEN)
    settings = _settings(tmp_path, _free_port())
    result: dict[str, int] = {}
    factory = FakeFactory()
    options = server.RunOptions(service_factory=factory)
    thread = threading.Thread(
        target=lambda: result.update(code=server.run(settings, options)), daemon=True
    )
    thread.start()
    base = f"http://127.0.0.1:{settings.port}"
    _wait_until_healthy(base, thread)

    code, status = _request(f"{base}/api/status")
    assert code == 200
    assert status["monitoring"]["value"]["state"] == "STOPPED"
    assert status["chain"]["value"]["ok"] is True

    # A second instance on the same data directory is refused and writes nothing.
    assert server.run(settings) == server.EXIT_REFUSED

    assert _request(f"{base}/api/monitoring/start", method="POST")[0] == 403  # no token
    code, started = _request(f"{base}/api/monitoring/start", method="POST", token=TOKEN)
    assert code == 200
    assert started["monitoring"]["value"]["state"] == "RUNNING"

    code, body = _request(f"{base}/api/app/quit", method="POST", token=TOKEN)
    assert code == 202
    assert body["accepted"] is True
    assert body["session"]["value"] == {"clean": True, "reason": "user_quit", "detail": None}
    thread.join(timeout=20)
    assert not thread.is_alive(), "server did not shut down after quit"
    assert result["code"] == server.EXIT_OK

    with EventStore(settings.state_db_path) as store:
        events = store.read()
        assert [e.type for e in events] == [
            EventType.SESSION_STARTED,
            EventType.MONITORING_STARTED,
            EventType.MONITORING_STOPPED,
            EventType.SESSION_STOPPED,
        ]
        # market data is stopped inside the event loop before the session closes
        assert events[2].payload["reason"] == "app_shutdown"
        assert events[3].payload["reason"] == "user_quit"
        assert store.verify().ok


def test_run_refuses_busy_port_without_touching_the_store(tmp_path: Path) -> None:
    holder = bind_loopback("127.0.0.1", 0)
    try:
        settings = _settings(tmp_path, int(holder.getsockname()[1]))
        assert server.run(settings) == server.EXIT_REFUSED
    finally:
        holder.close()
    assert not settings.state_db_path.exists()


# ---------------------------------------------------------------------- real process + signals
# The app runs as a child process (`server.run` with a fake market-data service: no network); the
# per-launch token is pinned by patching `new_token` inside the child only, so the test can drive
# authenticated requests.
_CHILD = (
    "import sys\n"
    "sys.path.insert(0, sys.argv[3])\n"
    "import tsclaude.api.context as c\n"
    "c.new_token = lambda: sys.argv[1]\n"
    "from m1t2_fakes import FakeFactory\n"
    "from tsclaude.api.server import RunOptions, run\n"
    "from tsclaude.runtime.settings import Settings\n"
    "settings = Settings.from_env().model_copy(update={'port': int(sys.argv[2])})\n"
    "raise SystemExit(run(settings, RunOptions(service_factory=FakeFactory())))\n"
)
_WS_SUPPORTED = any(importlib.util.find_spec(m) is not None for m in ("websockets", "wsproto"))


def _stop_signals() -> list[Any]:
    if sys.platform == "win32":
        return [pytest.param(signal.CTRL_BREAK_EVENT, "signal_SIGBREAK", id="CTRL_BREAK")]
    return [
        pytest.param(signal.SIGTERM, "signal_SIGTERM", id="SIGTERM"),
        pytest.param(signal.SIGINT, "signal_SIGINT", id="SIGINT"),
        pytest.param(signal.SIGHUP, "signal_SIGHUP", id="SIGHUP"),
    ]


def _websocket_handshake(port: int, token: str) -> int:
    """Raw RFC 6455 opening handshake (no client library); returns the HTTP status code."""
    request = (
        f"GET /ws/live?token={token} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode('ascii')}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.sendall(request.encode("ascii"))
        status_line = conn.recv(1024).split(b"\r\n", 1)[0].decode("ascii", "replace")
        time.sleep(0.2)  # let the server log the accepted connection
    return int(status_line.split()[1])


@pytest.mark.parametrize(("sig", "expected_reason"), _stop_signals())
def test_process_stops_cleanly_on_signal_and_never_logs_the_token(
    tmp_path: Path, sig: int, expected_reason: str
) -> None:
    port = _free_port()
    token = "pinned-" + secrets.token_urlsafe(24)
    data_dir = tmp_path / "data"
    env = {
        **os.environ,
        "TSC_DATA_DIR": str(data_dir),
        "TSC_OPEN_BROWSER": "false",
        "TSC_HEARTBEAT_INTERVAL_S": "0.2",
        "NO_PROXY": "127.0.0.1,localhost",
    }
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD, token, str(port), str(Path(__file__).resolve().parent)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=flags,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 30
        while True:
            if proc.poll() is not None:
                out, err = proc.communicate()
                raise AssertionError(
                    f"app exited early ({proc.returncode}): {err.decode()[-2000:]}"
                )
            try:
                if _request(f"{base}/api/health")[0] == 200:
                    break
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise AssertionError("app did not become healthy")
            time.sleep(0.1)

        assert _request(f"{base}/api/monitoring/start", method="POST", token=token)[0] == 200
        ws_status = _websocket_handshake(port, token)  # puts the token in uvicorn's WS log line
        if _WS_SUPPORTED:
            assert ws_status == 101

        proc.send_signal(sig)
        out, err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert proc.returncode == 0, err.decode()[-2000:]
    with EventStore(data_dir / "state.db") as store:
        events = store.read()
        assert store.verify().ok
    assert [e.type for e in events][-2:] == [
        EventType.MONITORING_STOPPED,
        EventType.SESSION_STOPPED,
    ]
    assert events[-1].payload["reason"] == expected_reason

    log_text = (data_dir / "logs" / "tsclaude.jsonl").read_text(encoding="utf-8")
    for stream in (log_text, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")):
        assert token not in stream
    if _WS_SUPPORTED:
        assert "/ws/live?token=***REDACTED***" in log_text
