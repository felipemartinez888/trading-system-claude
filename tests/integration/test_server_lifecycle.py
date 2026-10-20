"""`server.run` end to end on a loopback port (no external network): start, serve, quit cleanly.

These run uvicorn in a worker thread, so signal handling is skipped (uvicorn and the server only
install signal handlers on the main thread); the quit path is driven through `POST /api/app/quit`.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from api_testkit import TOKEN

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
    thread = threading.Thread(target=lambda: result.update(code=server.run(settings)), daemon=True)
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
    assert (code, body) == (202, {"accepted": True})
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
        assert events[2].payload["reason"] == "session_closing"
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
