"""MVP-2 paper endpoints (P7): provenance labels, the token-protected new-session action, and the
paper state in `/api/status`. Fake market data, no network; nothing here can place an order."""

from __future__ import annotations

import time
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from api_testkit import BASE_URL, PORT, TOKEN
from api_testkit import T0 as _T0
from fastapi.testclient import TestClient
from m1t2_fakes import FakeFactory, make_bar

from tsclaude.api.app import create_app
from tsclaude.api.context import AppContext
from tsclaude.contracts.market import Market
from tsclaude.contracts.timeutil import ManualClock
from tsclaude.runtime.paper_runtime import AUDIT_FILE, PaperRuntime, PaperSettings
from tsclaude.runtime.supervisor import MonitoringSupervisor
from tsclaude.signals.demo import DemoDetectorConfig
from tsclaude.sim.paper import PaperConfig
from tsclaude.sim.testing import spot_spec, usdm_spec
from tsclaude.store.eventstore import EventStore

T0 = _T0 - _T0 % 60_000
ORIGIN = f"http://127.0.0.1:{PORT}"
AUTH = {"X-TSC-Token": TOKEN, "Origin": ORIGIN}


def _app(tmp_path: Path, *, enabled: bool = True, paper: bool = True) -> TestClient:
    store = EventStore(tmp_path / "state.db")
    clock = ManualClock(T0)
    factory = FakeFactory()
    supervisor = MonitoringSupervisor(
        store, clock, service_factory=factory, demo=DemoDetectorConfig(enabled=False)
    )
    if paper:
        supervisor.attach_paper(
            PaperRuntime(
                PaperSettings(enabled=enabled, poll_s=0.01),
                store,
                clock,
                config_factory=lambda: PaperConfig(
                    instruments=(spot_spec(), usdm_spec()), starting_equity=Decimal(10_000)
                ),
                audit_path=tmp_path / AUDIT_FILE,
            )
        )
    ctx = AppContext(
        store=store,
        clock=clock,
        app_version="test",
        port=PORT,
        heartbeat_interval_s=5.0,
        token=TOKEN,
        supervisor=supervisor,
    )
    ctx.start()
    static = tmp_path / "no-static"
    static.mkdir(exist_ok=True)
    app = create_app(ctx, static_dir=static, heartbeat=False, push_interval_s=None)
    client = TestClient(app, base_url=BASE_URL)
    client.fake_factory = factory  # type: ignore[attr-defined]  # test drivers for test_p7
    client.fake_clock = clock  # type: ignore[attr-defined]
    return client


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with _app(tmp_path) as c:
        yield c


def _new(client: TestClient, note: str = "clean start", rid: str = "req-1000", **h: Any) -> Any:
    headers = h.get("headers", AUTH)
    return client.post(
        "/api/paper/session/new", json={"note": note, "request_id": rid}, headers=headers
    )


def test_the_status_shows_why_paper_is_not_observing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N-2: while the clock evidence is not trustworthy the status says paper is RUNNING but its
    observation is blocked, and why; once valid evidence arrives it shows the effective anchor."""
    from m1t2_fakes import FakeMarketData

    from tsclaude.contracts.provenance import Sourced
    from tsclaude.marketdata.models import ClockContinuity

    real = FakeMarketData.snapshot
    broken = {"on": True}

    def snapshot(self: FakeMarketData) -> Any:
        snap = real(self)
        if not broken["on"]:
            return snap
        missing = Sourced[ClockContinuity].unavailable("no monotonic clock readings")
        return snap.model_copy(update={"clock_continuity": missing})

    monkeypatch.setattr(FakeMarketData, "snapshot", snapshot)

    def observation(c: TestClient, state: str) -> dict[str, Any]:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            value = c.get("/api/paper/status").json()["status"]["value"]
            got = value["observation"]
            if got is not None and got["state"] == state:
                return value
            time.sleep(0.02)
        raise AssertionError(f"observation never became {state}: {got}")

    with _app(tmp_path) as c:
        assert c.get("/api/paper/status").json()["status"]["value"]["observation"] is None
        assert c.post("/api/monitoring/start", headers=AUTH).status_code == 200
        value = observation(c, "blocked")
        assert value["state"] == "RUNNING"
        got = value["observation"]
        assert got["code"] == "clock_continuity_unknown"
        assert "no monotonic clock evidence (no monotonic clock readings)" in got["problem"]
        assert got["observing_from_ms"] is None
        broken["on"] = False
        got = observation(c, "active")["observation"]
        assert got["observing_from_ms"] == T0 + 60_000  # the fake's round: T0 + 3 + 23 + 2 ms
        assert got["problem"] is None
        assert {"offset_ms", "max_rtt_ms", "uncertainty_ms", "allowance_ms"} <= set(got)


def test_p7_status_and_values_are_labelled(client: TestClient) -> None:
    status = client.get("/api/paper/status").json()["status"]
    assert status["provenance"] == "SYSTEM"
    assert status["value"]["state"] == "IDLE"
    assert any("not guaranteed" in s for s in status["value"]["limitations"])
    books = client.get("/api/paper/books").json()["books"]
    assert books["provenance"] == "UNAVAILABLE"  # no session open while monitoring is stopped
    assert client.get("/api/status").json()["paper"]["value"].startswith("IDLE")

    assert client.post("/api/monitoring/start", headers=AUTH).status_code == 200
    status = client.get("/api/paper/status").json()["status"]
    assert status["value"]["state"] == "RUNNING"
    assert status["value"]["session_id"] == "paper"
    assert client.get("/api/status").json()["paper"]["value"] == "RUNNING"
    # 1. RUNNING but no bar processed yet: no value is backed by market data -> UNAVAILABLE
    books = client.get("/api/paper/books").json()["books"]
    assert books["provenance"] == "UNAVAILABLE"
    assert "no bar processed" in books["reason"]
    fills = client.get("/api/paper/fills", params={"limit": 5}).json()["fills"]
    assert fills["provenance"] == "UNAVAILABLE"

    # 2. a live (ws_live) minute closes for every instrument and the paper consumer processes it
    clock = client.fake_clock  # type: ignore[attr-defined]
    md = client.fake_factory.instance  # type: ignore[attr-defined]
    last_close = T0 + 60_000 - 1
    clock.set(last_close + 1 + 1_500)
    for market, symbol in ((Market.SPOT, "BTCUSDT"), (Market.USDM, "BTCUSDT")):
        bar = make_bar(market, symbol, T0)
        client.portal.call(md.push_bar, bar)  # type: ignore[union-attr]  # on the app's loop
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        status = client.get("/api/paper/status").json()["status"]["value"]
        if status["live_bar_since_start"]:
            break
        time.sleep(0.02)
    assert status["live_bar_since_start"] is True
    assert status["values_as_of_ms"] == last_close
    books = client.get("/api/paper/books").json()["books"]
    assert books["provenance"] == "SIMULATED"  # RUNNING, live bar processed, fresh
    assert books["as_of_ms"] == last_close  # the bar close, not the publication time
    assert set(books["value"]["books"]) == {"baseline", "shadow", "benchmark"}
    fills = client.get("/api/paper/fills", params={"limit": 5}).json()["fills"]
    assert fills["provenance"] == "SIMULATED"

    # 3./4. after monitoring stops, the last-known values are STALE, stamped with the bar close
    assert client.post("/api/monitoring/stop", headers=AUTH).status_code == 200
    published_at = client.get("/api/paper/status").json()["status"]["as_of_ms"]
    books = client.get("/api/paper/books").json()["books"]
    assert books["provenance"] == "STALE"
    assert books["as_of_ms"] == last_close
    assert books["as_of_ms"] != published_at  # never the API publication time
    assert "IDLE" in books["reason"]
    fills = client.get("/api/paper/fills", params={"limit": 5}).json()["fills"]
    assert fills["provenance"] == "STALE"
    assert fills["as_of_ms"] == last_close


def test_p7_new_session_requires_token_note_and_a_stopped_paper(
    client: TestClient, tmp_path: Path
) -> None:
    assert _new(client, headers={"Origin": ORIGIN}).status_code == 403  # no token
    assert _new(client, note="").status_code == 422
    assert _new(client, rid="short").status_code == 422
    assert client.post("/api/monitoring/start", headers=AUTH).status_code == 200
    assert _new(client).status_code == 409  # refused while paper is RUNNING
    assert client.post("/api/monitoring/stop", headers=AUTH).status_code == 200
    first = _new(client)
    assert first.status_code == 200
    body = first.json()
    assert body["created"] is True
    assert body["previous_session_id"] == "paper"
    assert body["new_session_id"].startswith("paper-")
    again = _new(client).json()
    assert again["created"] is False
    assert again["new_session_id"] == body["new_session_id"]
    assert _new(client, note="different").status_code == 409
    assert len((tmp_path / AUDIT_FILE).read_text(encoding="utf-8").splitlines()) == 1
    status = client.get("/api/paper/status").json()["status"]["value"]
    assert status["next_session_id"] == body["new_session_id"]


def test_p7_disabled_and_unconfigured_paper(tmp_path: Path) -> None:
    with _app(tmp_path / "a", enabled=False) as c:
        assert c.get("/api/paper/status").json()["status"]["value"]["state"] == "DISABLED"
        assert c.post("/api/monitoring/start", headers=AUTH).status_code == 200
        assert c.get("/api/paper/status").json()["status"]["value"]["state"] == "DISABLED"
        assert _new(c).status_code == 409
    with _app(tmp_path / "b", paper=False) as c:
        assert c.get("/api/paper/status").json()["status"]["provenance"] == "UNAVAILABLE"
        assert c.get("/api/status").json()["paper"]["provenance"] == "UNAVAILABLE"
        assert _new(c).status_code == 409
