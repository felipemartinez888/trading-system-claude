"""M1-T2 API tests with a fake market-data service (no network)."""

from __future__ import annotations

import itertools
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from api_testkit import BASE_URL, PORT, TOKEN, WS_URL
from api_testkit import T0 as _T0
from fastapi.testclient import TestClient
from m1t2_fakes import FakeFactory, FakeMarketData, make_bar

from tsclaude.api.app import create_app
from tsclaude.api.context import AppContext
from tsclaude.contracts.events import AggregateType, EventType, NewEvent
from tsclaude.contracts.market import BarSource, Market, StreamState
from tsclaude.contracts.provenance import Provenance, Sourced
from tsclaude.contracts.timeutil import MS_PER_DAY, MS_PER_MINUTE, ManualClock, SystemClock
from tsclaude.runtime.supervisor import MonitoringSupervisor
from tsclaude.signals.demo import DemoDetectorConfig
from tsclaude.store.eventstore import EventStore
from tsclaude.store.livebars import LiveBarStore

T0 = _T0 - _T0 % 60_000  # minute-aligned (2025-10-09T08:53:00Z)
ORIGIN = f"http://127.0.0.1:{PORT}"
AUTH = {"X-TSC-Token": TOKEN, "Origin": ORIGIN}


class Harness:
    def __init__(self, tmp_path: Path, clock: Any) -> None:
        self.store = EventStore(tmp_path / "state.db")
        self.factory = FakeFactory()
        self.supervisor = MonitoringSupervisor(
            self.store,
            clock,
            service_factory=self.factory,
            demo=DemoDetectorConfig(threshold=0.003),
        )
        self.ctx = AppContext(
            store=self.store,
            clock=clock,
            app_version="test",
            port=PORT,
            heartbeat_interval_s=5.0,
            token=TOKEN,
            supervisor=self.supervisor,
        )
        self.ctx.start()
        static = tmp_path / "no-static"
        static.mkdir()
        self.static = static

    @property
    def service(self) -> FakeMarketData:
        assert self.factory.instance is not None
        return self.factory.instance

    def client(self, **kwargs: Any) -> TestClient:
        kwargs.setdefault("heartbeat", False)
        kwargs.setdefault("push_interval_s", None)
        return TestClient(create_app(self.ctx, static_dir=self.static, **kwargs), base_url=BASE_URL)


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    harness = Harness(tmp_path, SystemClock())
    yield harness
    harness.store.close()


def _mon(body: dict[str, Any]) -> dict[str, Any]:
    value: dict[str, Any] = body["monitoring"]["value"]
    return value


# ---------------------------------------------------------------------- start / stop wiring
def test_start_and_stop_drive_the_service(h: Harness) -> None:
    with h.client() as c:
        before = c.get("/api/status").json()
        assert before["streams"]["spot"]["provenance"] == "UNAVAILABLE"
        r = c.post("/api/monitoring/start", headers=AUTH)
        assert r.status_code == 200, r.text
        session_id = h.ctx.status().session.value.session_id  # type: ignore[union-attr]
        assert h.service.start_calls == [session_id]
        assert _mon(r.json())["state"] == "RUNNING"
        status = c.get("/api/status").json()
        assert status["streams"]["spot"] == {
            "value": "HEALTHY 2/2",
            "provenance": "SYSTEM",
            "as_of_ms": status["streams"]["spot"]["as_of_ms"],
            "reason": None,
        }
        assert status["streams"]["futures"]["value"] == "HEALTHY 3/3"
        assert c.post("/api/monitoring/stop", headers=AUTH).status_code == 200
        assert h.service.stop_calls == 1
        after = c.get("/api/status").json()
        assert after["streams"]["spot"]["provenance"] == "STALE"
        # restart: a fresh subscription per run (the service closes them on stop)
        assert c.post("/api/monitoring/start", headers=AUTH).status_code == 200
        assert h.service.subscribed_total == 2
        assert len(h.service.subscriptions) == 1
    types = [str(e.type) for e in h.store.read()]
    assert types.count("monitoring_started") == 2
    assert types[-1] == "monitoring_stopped"  # app shutdown stopped monitoring
    assert h.service.closed


def test_start_failure_is_surfaced_and_monitoring_stays_stopped(h: Harness) -> None:
    h.factory.fail_start = "api.binance.com unreachable"
    with h.client() as c:
        r = c.post("/api/monitoring/start", headers=AUTH)
        assert r.status_code == 503
        assert "api.binance.com unreachable" in r.json()["detail"]
        mon = _mon(c.get("/api/status").json())
        assert mon["state"] == "STOPPED"
        assert mon["transition"] is None
        assert "unreachable" in mon["last_error"]
        assert mon["start_allowed"] is True  # the user may retry
        # nothing was recorded as observation coverage
        assert "monitoring_started" not in [str(e.type) for e in h.store.read()]
        # the subscription created for the failed run was closed
        assert not h.service.subscriptions
        # a later successful start clears the error
        h.service.fail_start = None
        assert c.post("/api/monitoring/start", headers=AUTH).status_code == 200
        assert _mon(c.get("/api/status").json())["last_error"] is None


def test_config_error_is_a_start_failure(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "state.db")

    def broken(_: EventStore, __: Any) -> Any:
        raise FileNotFoundError("configs/universe.yaml")

    sup = MonitoringSupervisor(store, SystemClock(), service_factory=broken)
    ctx = AppContext(
        store=store,
        clock=SystemClock(),
        app_version="t",
        port=PORT,
        heartbeat_interval_s=5,
        token=TOKEN,
        supervisor=sup,
    )
    ctx.start()
    static = tmp_path / "s"
    static.mkdir()
    with TestClient(
        create_app(ctx, static_dir=static, heartbeat=False, push_interval_s=None),
        base_url=BASE_URL,
    ) as c:
        r = c.post("/api/monitoring/start", headers=AUTH)
        assert r.status_code == 503
        assert "configuration error" in r.json()["detail"]
    store.close()


# ---------------------------------------------------------------------- read models
def test_markets_pass_provenance_through_unchanged(h: Harness) -> None:
    with h.client() as c:
        empty = c.get("/api/markets").json()
        assert empty["rows"] == []
        assert empty["running"]["value"] is False
        c.post("/api/monitoring/start", headers=AUTH)
        # every provenance the service might use for a book value is passed through as-is
        for prov in (
            Provenance.LIVE,
            Provenance.STALE,
            Provenance.RECOVERED,
            Provenance.HISTORICAL,
        ):
            h.service.book_provenance = prov
            rows = c.get("/api/markets").json()["rows"]
            assert {r["best_bid_ask"]["provenance"] for r in rows} == {str(prov)}
        rows = c.get("/api/markets").json()["rows"]
        spot = next(r for r in rows if r["market"] == "spot")
        assert spot["mark_price"] == {
            "value": None,
            "provenance": "UNAVAILABLE",
            "as_of_ms": None,
            "reason": "spot has no mark price",
        }
        assert spot["exchange_status"]["provenance"] == "HISTORICAL"
        assert spot["freshness"]["value"] == "HEALTHY"
        assert spot["freshness"]["provenance"] == "SYSTEM"
        assert spot["change_24h"]["provenance"] == "UNAVAILABLE"
        h.service.state[(Market.SPOT, "BTCUSDT", "book_ticker")] = StreamState.STALE
        spot = next(r for r in c.get("/api/markets").json()["rows"] if r["market"] == "spot")
        assert spot["freshness"]["value"] == "STALE"
        c.post("/api/monitoring/stop", headers=AUTH)
        rows = c.get("/api/markets").json()["rows"]
        assert {r["best_bid_ask"]["provenance"] for r in rows} == {"STALE"}
        assert {r["freshness"]["provenance"] for r in rows} == {"STALE"}


def test_change_24h_from_stored_bars(h: Harness) -> None:
    with h.client() as c:
        c.post("/api/monitoring/start", headers=AUTH)
        bars = LiveBarStore(h.store.path)
        last = make_bar(Market.SPOT, "BTCUSDT", T0, close=110.0)
        ref = make_bar(Market.SPOT, "BTCUSDT", T0 - MS_PER_DAY, close=100.0)
        bars.write(ref)
        bars.write(last)
        h.service.bars[(Market.SPOT, "BTCUSDT")] = Sourced(
            value=last, provenance=Provenance.LIVE, as_of_ms=last.close_time_ms
        )
        spot = next(r for r in c.get("/api/markets").json()["rows"] if r["market"] == "spot")
        assert spot["change_24h"]["value"] == pytest.approx(0.10)
        assert spot["change_24h"]["provenance"] == "LIVE"
        assert spot["last_closed_bar"]["provenance"] == "LIVE"
        bars.close()


def test_streams_health(h: Harness) -> None:
    with h.client() as c:
        before = c.get("/api/health/streams").json()
        assert before["streams"]["provenance"] == "UNAVAILABLE"
        c.post("/api/monitoring/start", headers=AUTH)
        body = c.get("/api/health/streams").json()
        assert body["streams"]["provenance"] == "SYSTEM"
        assert len(body["streams"]["value"]) == 5
        assert body["streams"]["value"][0]["state"]["provenance"] == "SYSTEM"
        assert body["connections"]["value"][0]["group"] == "spot"
        assert body["clock_offset_ms"] == {
            "value": 3,
            "provenance": "LIVE",  # as the service labels it
            "as_of_ms": body["clock_offset_ms"]["as_of_ms"],
            "reason": None,
        }
        assert body["rest_weight"]["value"]["spot"]["1M"] == [12, 6000]
        assert body["demo_detector"]["provenance"] == "DEMO"
        c.post("/api/monitoring/stop", headers=AUTH)
        after = c.get("/api/health/streams").json()
        assert after["streams"]["provenance"] == "STALE"
        assert after["connections"]["provenance"] == "STALE"


def _gap_event(gap_id: str, lo: int, hi: int) -> NewEvent:
    return NewEvent(
        type=EventType.GAP_DETECTED,
        aggregate_type=AggregateType.MARKET_DATA,
        aggregate_id="spot:BTCUSDT",
        ts_ms=lo,
        payload={
            "gap_id": gap_id,
            "market": "spot",
            "symbol": "BTCUSDT",
            "interval": "1m",
            "from_ms": lo,
            "to_ms": hi,
            "minutes": (hi - lo) // MS_PER_MINUTE,
            "cause": "disconnect",
            "detected_at_ms": hi,
            "resolution": "pending",
            "detail": None,
        },
    )


def test_gaps_endpoint_projects_events(h: Harness) -> None:
    h.store.append(_gap_event("G1", T0, T0 + 3 * MS_PER_MINUTE))
    h.store.append(
        NewEvent(
            type=EventType.GAP_RESOLVED,
            aggregate_type=AggregateType.MARKET_DATA,
            aggregate_id="spot:BTCUSDT",
            ts_ms=T0 + 4 * MS_PER_MINUTE,
            payload={
                "gap_id": "G1",
                "market": "spot",
                "symbol": "BTCUSDT",
                "ranges": [[T0, T0 + MS_PER_MINUTE]],
                "minutes": 1,
                "resolution": "recovered",
                "source": "rest_recovered",
                "remaining_minutes": 2,
                "fully_resolved": False,
                "resolved_at_ms": T0 + 4 * MS_PER_MINUTE,
            },
        )
    )
    with h.client() as c:
        body = c.get("/api/gaps").json()
        assert body["gaps"]["provenance"] == "SYSTEM"
        (gap,) = body["gaps"]["value"]
        assert gap["missing_minutes"] == 2
        assert gap["resolution"] == "pending"
        assert gap["missing_ranges_ms"] == [[T0 + MS_PER_MINUTE, T0 + 3 * MS_PER_MINUTE]]
        health = c.get("/api/health/streams").json()
        assert health["open_gaps"]["value"] == 1
        assert health["open_gap_minutes"]["value"] == 2
        notes = c.get("/api/notifications").json()["notifications"]["value"]
        assert {n["kind"] for n in notes} >= {"gap_detected", "gap_resolved"}


def test_bars_endpoint(h: Harness) -> None:
    bars = LiveBarStore(h.store.path)
    bars.write(make_bar(Market.SPOT, "BTCUSDT", T0, source=BarSource.REST_BACKFILL))
    bars.write(make_bar(Market.SPOT, "BTCUSDT", T0 + MS_PER_MINUTE, source=BarSource.WS_LIVE), "S1")
    bars.write(
        make_bar(Market.SPOT, "BTCUSDT", T0 + 2 * MS_PER_MINUTE, source=BarSource.REST_RECOVERED)
    )
    bars.close()
    with h.client() as c:
        params = {
            "market": "spot",
            "symbol": "BTCUSDT",
            "from": "2025-10-09T08:53:00.000Z",
            "to": "2025-10-09T09:00:00.000Z",
        }
        body = c.get("/api/bars", params=params).json()
        items = body["bars"]["value"]
        assert [(b["source"], b["provenance"]) for b in items] == [
            ("rest_backfill", "HISTORICAL"),
            ("ws_live", "HISTORICAL"),
            ("rest_recovered", "RECOVERED"),
        ]
        assert items[1]["observed_session_id"] == "S1"
        assert body["bars"]["provenance"] == "HISTORICAL"
        assert c.get("/api/bars", params={**params, "market": "nasdaq"}).status_code == 422
        assert c.get("/api/bars", params={**params, "symbol": "btc;drop"}).status_code == 422
        assert c.get("/api/bars", params={**params, "interval": "5m"}).status_code == 422
        bad_range = {**params, "from": params["to"], "to": params["from"]}
        assert c.get("/api/bars", params=bad_range).status_code == 400
        assert c.get("/api/bars", params={**params, "to": "yesterday"}).status_code == 400


# ---------------------------------------------------------------------- demo signals
def test_demo_watch_end_to_end_and_fail_closed(h: Harness) -> None:
    with h.client() as c:
        c.post("/api/monitoring/start", headers=AUTH)
        svc = h.service
        big = make_bar(Market.SPOT, "BTCUSDT", T0, open_=100.0, close=101.0)  # +1 %
        small = make_bar(Market.SPOT, "BTCUSDT", T0 + MS_PER_MINUTE, open_=100.0, close=100.1)
        recovered = make_bar(
            Market.SPOT,
            "BTCUSDT",
            T0 + 2 * MS_PER_MINUTE,
            close=102.0,
            source=BarSource.REST_RECOVERED,
        )
        for bar in (big, small, recovered):
            _on_loop(c, svc.push_bar, bar)
        _wait(lambda: h.supervisor.detector.stats.bars_seen == 3)
        signals = c.get("/api/signals").json()["signals"]
        assert signals["provenance"] == "DEMO"
        (sig,) = signals["value"]
        assert sig["stage"] == "WATCH"
        assert sig["strategy_id"] == "demo_detector"
        assert sig["evidence_grade"] == "X"
        assert sig["run_mode"] == "LIVE"
        assert sig["side"] == "long"
        # unhealthy stream for the symbol -> no signal (fail closed)
        svc.state[(Market.SPOT, "BTCUSDT", "book_ticker")] = StreamState.STALE
        _on_loop(
            c, svc.push_bar, make_bar(Market.SPOT, "BTCUSDT", T0 + 3 * MS_PER_MINUTE, close=99.0)
        )
        _wait(lambda: h.supervisor.detector.stats.bars_seen == 4)
        assert len(c.get("/api/signals").json()["signals"]["value"]) == 1
        assert h.supervisor.detector.stats.skipped_unhealthy == 1
        notes = c.get("/api/notifications").json()["notifications"]["value"]
        demo = [n for n in notes if n["kind"] == "signal_created"]
        assert [n["provenance"] for n in demo] == ["DEMO"]
    created = h.store.read(types=[EventType.SIGNAL_CREATED])
    assert {e.payload["stage"] for e in created} == {"WATCH"}


def _on_loop(c: TestClient, fn: Any, *args: Any) -> None:
    """asyncio queues are not thread-safe: drive the fake on the app's event loop."""
    assert c.portal is not None
    c.portal.call(fn, *args)


def _wait(pred: Any, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


# ---------------------------------------------------------------------- push
def test_push_is_rate_limited_to_one_per_second(h: Harness) -> None:
    """The fake's snapshot changes constantly (timestamps), so every tick has a delta."""
    with h.client(push_interval_s=1.0) as c:
        c.post("/api/monitoring/start", headers=AUTH)
        with c.websocket_connect(f"{WS_URL}?token={TOKEN}") as ws:
            stamps: list[float] = []
            start = time.monotonic()
            while time.monotonic() - start < 3.6:
                msg = ws.receive_json()
                if msg["type"] == "delta":
                    stamps.append(time.monotonic())
                    assert msg["data"]["markets"], "first deltas carry the rows"
            assert 2 <= len(stamps) <= 4
            gaps = [b - a for a, b in itertools.pairwise(stamps)]
            assert all(g >= 0.9 for g in gaps), gaps


def test_push_tick_contents(tmp_path: Path) -> None:
    import asyncio

    from tsclaude.api.push import MarketPusher

    harness = Harness(tmp_path, ManualClock(T0))

    async def scenario() -> None:
        pusher = MarketPusher(harness.ctx)
        assert pusher.tick() is None  # nothing started yet
        await harness.supervisor.start("S")
        first = pusher.tick()
        assert first is not None
        assert len(first["data"]["markets"]) == 2
        assert first["data"]["health_changed"] is True
        assert pusher.tick() is None  # nothing changed (manual clock)
        harness.service.crashes = 1
        crash = pusher.tick()
        assert crash is not None
        assert crash["data"]["notifications"][0]["kind"] == "connection_crashed"
        await harness.supervisor.close()

    asyncio.run(scenario())
    harness.ctx.release_resources()
    harness.store.close()
