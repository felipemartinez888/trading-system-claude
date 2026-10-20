"""API integration tests through FastAPI's TestClient (in-process ASGI, no network)."""

from __future__ import annotations

import html
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from api_testkit import (
    BASE_URL,
    PORT,
    WS_URL,
    ContextFactory,
    make_tampered_db,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tsclaude.api import pages
from tsclaude.api.app import create_app
from tsclaude.api.context import AppContext
from tsclaude.contracts.timeutil import ManualClock
from tsclaude.runtime.sessions import SessionManager
from tsclaude.store.eventstore import EventStore

ORIGIN = f"http://127.0.0.1:{PORT}"


@pytest.fixture
def no_bundle(tmp_path: Path) -> Path:
    path = tmp_path / "no-static"
    path.mkdir()
    return path


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    path = tmp_path / "static"
    (path / "assets").mkdir(parents=True)
    (path / "index.html").write_text(
        "<!doctype html><html><head><title>TSC</title></head><body><div id=root></div>"
        '<script type="module" src="/assets/app.js"></script></body></html>',
        encoding="utf-8",
    )
    (path / "assets" / "app.js").write_text("console.log('ok');", encoding="utf-8")
    return path


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=BASE_URL)


@pytest.fixture
def ctx(make_context: ContextFactory) -> AppContext:
    return make_context()


@pytest.fixture
def client(ctx: AppContext, no_bundle: Path) -> Iterator[TestClient]:
    with _client(create_app(ctx, static_dir=no_bundle, heartbeat=False)) as c:
        yield c


def _auth(ctx: AppContext, **extra: str) -> dict[str, str]:
    return {"X-TSC-Token": ctx.token, "Origin": ORIGIN, **extra}


def _monitoring(body: dict[str, Any]) -> dict[str, Any]:
    value: dict[str, Any] = body["monitoring"]["value"]
    return value


# ---------------------------------------------------------------------- status
def test_health_is_plain_liveness(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "app": "tsclaude", "version": "test"}


def test_status_reflects_session(client: TestClient, ctx: AppContext) -> None:
    body = client.get("/api/status").json()
    assert body["app_version"] == "test"
    assert body["server_time"].endswith("Z")
    session = body["session"]
    assert session["provenance"] == "SYSTEM"
    assert session["value"]["session_id"] == ctx.store.read()[0].aggregate_id
    assert session["value"]["started_at"].endswith("Z")
    assert _monitoring(body)["state"] == "STOPPED"
    assert _monitoring(body)["start_allowed"] is True
    assert body["chain"]["value"]["ok"] is True
    assert body["previous_session"]["value"]["outcome"] == "NONE"
    for stream in ("spot", "futures"):
        assert body["streams"][stream] == {
            "value": None,
            "provenance": "UNAVAILABLE",
            "as_of_ms": None,
            "reason": "not implemented until M1",
        }


# ---------------------------------------------------------------------- transitions
def test_start_stop_transitions_and_409(client: TestClient, ctx: AppContext) -> None:
    r = client.post("/api/monitoring/start", headers=_auth(ctx))
    assert r.status_code == 200, r.text
    running = _monitoring(r.json())
    assert running["state"] == "RUNNING"
    assert running["monitoring_id"]
    assert running["since"].endswith("Z")
    assert _monitoring(client.get("/api/status").json())["state"] == "RUNNING"

    again = client.post("/api/monitoring/start", headers=_auth(ctx))
    assert again.status_code == 409
    assert "already running" in again.json()["detail"]

    r = client.post("/api/monitoring/stop", headers=_auth(ctx))
    assert r.status_code == 200
    assert _monitoring(r.json())["state"] == "STOPPED"

    again = client.post("/api/monitoring/stop", headers=_auth(ctx))
    assert again.status_code == 409


# ---------------------------------------------------------------------- security
@pytest.mark.parametrize("path", ["/api/monitoring/start", "/api/monitoring/stop", "/api/app/quit"])
def test_mutations_require_token(client: TestClient, ctx: AppContext, path: str) -> None:
    assert client.post(path, headers={"Origin": ORIGIN}).status_code == 403
    bad = client.post(path, headers={"Origin": ORIGIN, "X-TSC-Token": "wrong"})
    assert bad.status_code == 403
    assert _monitoring(client.get("/api/status").json())["state"] == "STOPPED"


def test_token_is_sufficient_without_origin(client: TestClient, ctx: AppContext) -> None:
    # Non-browser clients send no Origin; the token alone is then accepted.
    r = client.post("/api/monitoring/start", headers={"X-TSC-Token": ctx.token})
    assert r.status_code == 200


@pytest.mark.parametrize(
    "origin",
    ["http://evil.example", "http://127.0.0.1:9999", "https://127.0.0.1:8765", "null"],
)
def test_foreign_origin_is_rejected_even_with_token(
    client: TestClient, ctx: AppContext, origin: str
) -> None:
    r = client.post("/api/monitoring/start", headers={"X-TSC-Token": ctx.token, "Origin": origin})
    assert r.status_code == 403
    assert client.get("/api/status", headers={"Origin": origin}).status_code == 403
    assert _monitoring(client.get("/api/status").json())["state"] == "STOPPED"


@pytest.mark.parametrize("origin", [f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"])
def test_loopback_origins_are_accepted(client: TestClient, ctx: AppContext, origin: str) -> None:
    r = client.post("/api/monitoring/start", headers={"X-TSC-Token": ctx.token, "Origin": origin})
    assert r.status_code == 200


def test_foreign_host_is_rejected(ctx: AppContext, no_bundle: Path) -> None:
    # DNS rebinding: a page on attacker.example resolving to 127.0.0.1 sends Host: attacker...
    app = create_app(ctx, static_dir=no_bundle, heartbeat=False)
    with TestClient(app, base_url=f"http://attacker.example:{PORT}") as c:
        assert c.get("/api/status").status_code == 403
        assert c.get("/").status_code == 403


def test_no_cors_headers_are_sent(client: TestClient) -> None:
    r = client.get("/api/status", headers={"Origin": ORIGIN})
    assert "access-control-allow-origin" not in r.headers


# ---------------------------------------------------------------------- chain failure
def test_423_when_chain_verification_failed(
    db_path: Path, clock: ManualClock, make_context: ContextFactory, no_bundle: Path
) -> None:
    make_tampered_db(db_path, clock)
    ctx = make_context()
    with _client(create_app(ctx, static_dir=no_bundle, heartbeat=False)) as c:
        status = c.get("/api/status").json()
        chain = status["chain"]["value"]
        assert chain["ok"] is False
        assert chain["first_bad_seq"] == 1
        assert chain["reason"] == "hash mismatch"
        monitoring = _monitoring(status)
        assert monitoring["start_allowed"] is False
        assert "verification failed" in monitoring["blocked_reason"]

        r = c.post("/api/monitoring/start", headers=_auth(ctx))
        assert r.status_code == 423
        assert "verification failed" in r.json()["detail"]
        # Projections folded from an untrusted chain are not presented as facts.
        assert status["previous_session"]["provenance"] == "UNAVAILABLE"
        coverage = c.get("/api/coverage").json()
        assert coverage["intervals"]["provenance"] == "UNAVAILABLE"
        assert "verification failed" in coverage["intervals"]["reason"]
        # ...but the raw events stay readable for the audit page.
        events = c.get("/api/events").json()
        assert events["events"]["provenance"] == "SYSTEM"
        assert events["chain"]["value"]["ok"] is False
        # The UI still serves and shows the failure.
        page = c.get("/")
        assert page.status_code == 200
        assert "hash mismatch" in page.text


# ---------------------------------------------------------------------- coverage
def test_coverage_after_start_stop_cycle(
    client: TestClient, ctx: AppContext, clock: ManualClock
) -> None:
    empty = client.get("/api/coverage").json()
    assert empty["intervals"] == {
        "value": [],
        "provenance": "SYSTEM",
        "as_of_ms": clock.now_ms(),
        "reason": None,
    }
    assert empty["gaps"]["value"] == []

    clock.advance(1_000)
    client.post("/api/monitoring/start", headers=_auth(ctx))
    clock.advance(60_000)
    client.post("/api/monitoring/stop", headers=_auth(ctx))
    clock.advance(5_000)
    client.post("/api/monitoring/start", headers=_auth(ctx))
    clock.advance(10_000)

    body = client.get("/api/coverage").json()
    intervals = body["intervals"]["value"]
    assert body["intervals"]["provenance"] == "SYSTEM"
    assert len(intervals) == 2
    first, second = intervals
    assert first["end_reason"] == "user"
    assert first["start"] == "2025-10-09T08:53:21.000Z"
    assert first["end"] == "2025-10-09T08:54:21.000Z"
    assert second["end"] is None  # still observing
    assert body["gaps"]["value"] == [
        {"start": first["end"], "end": second["start"], "after_reason": "user"}
    ]


# ---------------------------------------------------------------------- events
def test_events_endpoint_pages(client: TestClient, ctx: AppContext) -> None:
    client.post("/api/monitoring/start", headers=_auth(ctx))
    client.post("/api/monitoring/stop", headers=_auth(ctx))
    newest = client.get("/api/events", params={"limit": 2}).json()
    page = newest["events"]["value"]
    assert newest["events"]["provenance"] == "SYSTEM"
    assert [e["type"] for e in page["items"]] == ["monitoring_started", "monitoring_stopped"]
    assert page["has_more"] is True
    assert page["head_seq"] == 3
    assert page["items"][0]["ts"].endswith("Z")

    after = client.get("/api/events", params={"after_seq": 1, "limit": 1}).json()
    assert [e["seq"] for e in after["events"]["value"]["items"]] == [2]
    assert after["events"]["value"]["has_more"] is True
    assert client.get("/api/events", params={"limit": 0}).status_code == 422


# ---------------------------------------------------------------------- previous session
def test_status_reports_previous_crash(
    db_path: Path, clock: ManualClock, make_context: ContextFactory, no_bundle: Path
) -> None:
    with EventStore(db_path) as store:
        crashed = SessionManager(store, clock, app_version="old")
        crashed.open()
        crashed.start_monitoring()
        clock.advance(30_000)
        crashed.heartbeat()  # last sign of life; then the process is killed
    clock.advance(600_000)
    ctx = make_context()
    with _client(create_app(ctx, static_dir=no_bundle, heartbeat=False)) as c:
        prev = c.get("/api/status").json()["previous_session"]["value"]
        assert prev["outcome"] == "CRASHED"
        assert prev["monitoring_was_active"] is True
        assert prev["unobserved_from"] == "2025-10-09T08:53:50.000Z"
        assert prev["unobserved_to"] == "2025-10-09T09:03:50.000Z"
        coverage = c.get("/api/coverage").json()
        assert coverage["intervals"]["value"][0]["end_reason"] == "crash"


def test_status_reports_previous_clean_stop(
    clock: ManualClock, make_context: ContextFactory, no_bundle: Path
) -> None:
    first = make_context()
    clock.advance(10_000)
    first.close("user")
    clock.advance(20_000)
    ctx = make_context()
    with _client(create_app(ctx, static_dir=no_bundle, heartbeat=False)) as c:
        prev = c.get("/api/status").json()["previous_session"]["value"]
        assert prev["outcome"] == "CLEAN"
        assert prev["end_reason"] == "user"


# ---------------------------------------------------------------------- websocket
def test_websocket_rejects_missing_or_wrong_token(client: TestClient) -> None:
    for url in (WS_URL, f"{WS_URL}?token=wrong"):
        with pytest.raises(WebSocketDisconnect) as info, client.websocket_connect(url) as ws:
            ws.receive_json()
        assert info.value.code == 1008
        assert info.value.reason == "missing or invalid token"


def test_websocket_rejects_foreign_origin(client: TestClient, ctx: AppContext) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as info,
        client.websocket_connect(
            f"{WS_URL}?token={ctx.token}", headers={"Origin": "http://evil.example"}
        ) as ws,
    ):
        ws.receive_json()
    assert info.value.code == 1008
    assert info.value.reason == "forbidden origin"


def test_websocket_pushes_status_on_change(client: TestClient, ctx: AppContext) -> None:
    with client.websocket_connect(f"{WS_URL}?token={ctx.token}", headers={"Origin": ORIGIN}) as ws:
        first = ws.receive_json()
        assert first["type"] == "status"
        assert _monitoring(first["data"])["state"] == "STOPPED"
        assert client.post("/api/monitoring/start", headers=_auth(ctx)).status_code == 200
        pushed = ws.receive_json()
        assert pushed["type"] == "status"
        assert _monitoring(pushed["data"])["state"] == "RUNNING"


def test_websocket_pushes_on_heartbeat(make_context: ContextFactory, no_bundle: Path) -> None:
    ctx = make_context(heartbeat_interval_s=0.05)
    with (
        _client(create_app(ctx, static_dir=no_bundle, heartbeat=True)) as c,
        c.websocket_connect(f"{WS_URL}?token={ctx.token}") as ws,
    ):
        ws.receive_json()  # initial snapshot
        beat = ws.receive_json()  # produced by the heartbeat loop
        assert beat["type"] == "status"
        assert beat["data"]["heartbeat"]["provenance"] in ("SYSTEM", "STALE")


# ---------------------------------------------------------------------- quit
def test_quit_closes_session_then_triggers_shutdown_callback(
    client: TestClient, ctx: AppContext
) -> None:
    calls: list[list[str]] = []
    # The server is asked to stop only after session_stopped is in the event log.
    ctx.set_shutdown_callback(lambda: calls.append([str(e.type) for e in ctx.store.read()]))
    assert client.post("/api/monitoring/start", headers=_auth(ctx)).status_code == 200
    with client.websocket_connect(f"{WS_URL}?token={ctx.token}") as ws:
        ws.receive_json()  # initial snapshot
        r = client.post("/api/app/quit", headers=_auth(ctx))
        assert r.status_code == 202
        body = r.json()
        assert body["accepted"] is True
        assert body["session"]["provenance"] == "SYSTEM"
        assert body["session"]["value"] == {"clean": True, "reason": "user_quit", "detail": None}
        types: list[str] = []
        final: dict[str, Any] = {}
        for _ in range(20):  # status snapshots may interleave; the final message is `shutdown`
            message = ws.receive_json()
            types.append(message["type"])
            if message["type"] == "shutdown":
                final = message["data"]
                break
    assert calls == [
        ["session_started", "monitoring_started", "monitoring_stopped", "session_stopped"]
    ]
    assert ctx.shutdown_reason == "user_quit"
    assert "shutting_down" in types
    assert types.index("shutting_down") < types.index("shutdown")
    assert final == {"clean": True, "reason": "user_quit", "detail": None}


def test_quit_reports_unclean_close_truthfully(
    client: TestClient, ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.set_shutdown_callback(lambda: None)

    def broken_close(reason: str = "user") -> None:
        raise OSError("disk full")

    monkeypatch.setattr(ctx._sessions, "close", broken_close)
    body = client.post("/api/app/quit", headers=_auth(ctx)).json()
    value = body["session"]["value"]
    assert value["clean"] is False
    assert "session_stopped could not be recorded" in value["detail"]
    assert "session_stopped" not in [str(e.type) for e in ctx.store.read()]


# ---------------------------------------------------------------------- static
def test_fallback_page_when_bundle_missing(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "dashboard bundle is not built" in r.text.lower()
    assert '"provenance": "SYSTEM"' in html.unescape(r.text)  # the /api/status JSON is shown
    assert "test-token" not in r.text  # the fallback page has no use for the token


def test_index_injects_token_and_serves_assets(ctx: AppContext, bundle: Path) -> None:
    with _client(create_app(ctx, static_dir=bundle, heartbeat=False)) as c:
        page = c.get("/")
        assert page.status_code == 200
        assert f'<meta name="tsc-token" content="{ctx.token}">' in page.text
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert page.headers["cache-control"] == "no-store"
        asset = c.get("/assets/app.js")
        assert asset.status_code == 200
        assert asset.headers["content-type"].startswith("text/javascript")
        # client-side routes fall back to index.html; missing assets are 404
        assert ctx.token in c.get("/system/health").text
        assert c.get("/assets/missing.js").status_code == 404
        assert c.get("/api/does-not-exist").status_code == 404
        (bundle.parent / "secret.txt").write_text("outside the bundle", encoding="utf-8")
        for probe in ("/%2e%2e/secret.txt", "/assets/%2e%2e/%2e%2e/secret.txt"):
            r = c.get(probe)
            assert r.status_code == 404
            assert "outside the bundle" not in r.text


def test_unc_and_absolute_paths_are_refused_before_filesystem_access(
    ctx: AppContext, bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # F1: <img src="http://127.0.0.1:8765///attacker/share/x"> must not trigger SMB on Windows.
    calls: list[str] = []
    real = pages.resolve_static_file

    def spy(static_dir: Path, rel_path: str) -> Path | None:
        calls.append(rel_path)
        return real(static_dir, rel_path)

    monkeypatch.setattr(pages, "resolve_static_file", spy)
    with _client(create_app(ctx, static_dir=bundle, heartbeat=False)) as c:
        # Absolute URLs: a relative "///x" would be normalized by the client, not by a browser.
        probes = ("///attacker/share/x", "/%5C%5Cattacker%5Cshare%5Cx", "/C:/x", "//etc/passwd")
        for probe in probes:
            r = c.get(BASE_URL + probe)
            assert r.status_code == 404, probe
            assert ctx.token not in r.text
        assert calls == []
        assert c.get("/assets/app.js").status_code == 200
        assert calls == ["assets/app.js"]
