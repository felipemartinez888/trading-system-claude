"""Real `MarketDataService` behind the API, against the in-memory fake Binance (no network).

start (POST) -> streams HEALTHY, LIVE book -> a closed bar appears in /api/bars and /api/markets
-> the demo detector emits a DEMO WATCH -> stop (POST) -> every live value is STALE.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mdi_helpers import require

httpx = require("httpx")
fastapi_testclient = require("fastapi.testclient")

from mdi_helpers import MIN, T0, FakeExchange, kline_row, load_fixture, ws_kline  # noqa: E402

from tsclaude.api.app import create_app  # noqa: E402
from tsclaude.api.context import AppContext  # noqa: E402
from tsclaude.contracts.events import EventType  # noqa: E402
from tsclaude.contracts.timeutil import ManualClock, to_iso  # noqa: E402
from tsclaude.marketdata.config import MarketDataConfig  # noqa: E402
from tsclaude.marketdata.service import MarketDataService  # noqa: E402
from tsclaude.runtime.supervisor import MonitoringSupervisor  # noqa: E402
from tsclaude.signals.demo import DemoDetectorConfig  # noqa: E402
from tsclaude.store.eventstore import EventStore  # noqa: E402

PORT = 8765
TOKEN = "live-test-token-" + "y" * 24
AUTH = {"X-TSC-Token": TOKEN, "Origin": f"http://127.0.0.1:{PORT}"}

CONFIG = MarketDataConfig.from_mapping(
    {
        "version": 0,
        "snapshot_date": "2026-10-19",
        "rule": "test",
        "spot": ["BTCUSDT"],
        "usdm": ["BTCUSDT"],
    },
    {
        "freshness": {
            "kline_1m_s": 3600,
            "book_ticker_s": 3600,
            "mark_price_s": 3600,
            "tick_interval_s": 3600,
        },
        "watermark": {"check_interval_s": 3600, "gap_retry_interval_s": 3600},
        "clock": {"sample_interval_s": 3600, "samples_per_round": 3},
        "backfill": {"lookback_days": 10 / 1440, "max_backfill_days": 1},
        "ws": {"backoff_base_s": 0.01, "backoff_cap_s": 0.02, "stable_after_s": 0},
        "endpoints": {
            "spot_rest": ["https://spot.test"],
            "usdm_rest": ["https://fapi.test"],
            "spot_ws": ["wss://spot.test"],
            "usdm_ws_market": ["wss://fut.test/market"],
            "usdm_ws_public": ["wss://fut.test/public"],
        },
    },
)


def _exchange(clock: ManualClock) -> Callable[[Any], Any]:
    def handler(request: Any) -> Any:
        path = request.url.path
        params = dict(request.url.params)
        if path in ("/api/v3/time", "/fapi/v1/time"):
            return httpx.Response(200, json={"serverTime": clock.now_ms()})
        if path == "/api/v3/exchangeInfo":
            return httpx.Response(200, json=load_fixture("rest_spot_exchange_info.json"))
        if path == "/fapi/v1/exchangeInfo":
            return httpx.Response(200, json=load_fixture("rest_usdm_exchange_info.json"))
        if path == "/fapi/v1/premiumIndex":
            return httpx.Response(200, json=load_fixture("rest_usdm_premium_index.json"))
        if path in ("/api/v3/klines", "/fapi/v1/klines"):
            start, end = int(params["startTime"]), int(params["endTime"])
            rows = []
            t = start - start % MIN + (MIN if start % MIN else 0)
            while t <= end and t <= clock.now_ms() and len(rows) < int(params["limit"]):
                rows.append(kline_row(t))
                t += MIN
            return httpx.Response(200, json=rows, headers={"X-MBX-USED-WEIGHT-1M": "10"})
        return httpx.Response(404)

    return handler


def _until(pred: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.02)


def test_live_path_through_the_api(tmp_path: Path) -> None:
    clock = ManualClock(T0 + 30_000)
    store = EventStore(tmp_path / "state.db")
    ex = FakeExchange()

    def factory(s: EventStore, c: Any) -> Any:
        return MarketDataService(
            CONFIG,
            s,
            c,
            http_transport=httpx.MockTransport(_exchange(clock)),
            ws_connect=ex.connect,
        )

    supervisor = MonitoringSupervisor(
        store, clock, service_factory=factory, demo=DemoDetectorConfig(threshold=0.003)
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
    static = tmp_path / "static"
    static.mkdir()
    app = create_app(ctx, static_dir=static, heartbeat=False, push_interval_s=None)
    with fastapi_testclient.TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as c:
        r = c.post("/api/monitoring/start", headers=AUTH)
        assert r.status_code == 200, r.text
        assert r.json()["monitoring"]["value"]["state"] == "RUNNING"
        _until(lambda: len(ex.conns) == 3)
        spot = ex.latest("spot.test")
        fmarket = ex.latest("fut.test/market")
        fpublic = ex.latest("fut.test/public")

        def push(conn: Any, payload: Any, stream: str) -> None:
            assert c.portal is not None
            c.portal.call(conn.push, payload, stream)  # on the app's event loop

        push(spot, ws_kline(T0, closed=False), "btcusdt@kline_1m")
        push(spot, load_fixture("ws_spot_book_ticker.json"), "btcusdt@bookTicker")
        push(fmarket, ws_kline(T0, closed=False), "btcusdt@kline_1m")
        push(fmarket, load_fixture("ws_usdm_mark_price.json"), "btcusdt@markPrice@1s")
        push(fpublic, load_fixture("ws_usdm_book_ticker.json"), "btcusdt@bookTicker")

        def rows() -> dict[str, Any]:
            return {r["market"]: r for r in c.get("/api/markets").json()["rows"]}

        _until(lambda: all(r["freshness"]["value"] == "HEALTHY" for r in rows().values()))
        live = rows()
        assert live["spot"]["best_bid_ask"]["provenance"] == "LIVE"
        assert live["spot"]["partial_candle"]["provenance"] == "PARTIAL"
        assert live["usdm"]["mark_price"]["provenance"] == "LIVE"
        status = c.get("/api/status").json()
        assert status["streams"]["spot"]["value"] == "HEALTHY 2/2"
        assert status["streams"]["futures"]["value"] == "HEALTHY 3/3"

        # a live close: stored bar, LIVE last bar, demo WATCH (+0.5 % body > 0.3 %)
        c.portal.call(clock.set, T0 + MIN + 100)  # type: ignore[union-attr]
        push(spot, ws_kline(T0), "btcusdt@kline_1m")
        params = {
            "market": "spot",
            "symbol": "BTCUSDT",
            "from": to_iso(T0 - 20 * MIN),
            "to": to_iso(T0 + 5 * MIN),
        }

        def last_bar_source() -> str | None:
            items = c.get("/api/bars", params=params).json()["bars"]["value"]
            return items[-1]["source"] if items and items[-1]["open_time_ms"] == T0 else None

        _until(lambda: last_bar_source() == "ws_live")
        assert rows()["spot"]["last_closed_bar"]["provenance"] == "LIVE"
        _until(lambda: bool(c.get("/api/signals").json()["signals"]["value"]))
        signals = c.get("/api/signals").json()["signals"]
        assert signals["provenance"] == "DEMO"
        assert signals["value"][0]["stage"] == "WATCH"

        health = c.get("/api/health/streams").json()
        assert health["streams"]["provenance"] == "SYSTEM"
        assert health["rest_weight"]["provenance"] == "SYSTEM"

        # stop: market data stops, everything live turns STALE
        assert c.post("/api/monitoring/stop", headers=AUTH).status_code == 200
        stopped = rows()
        for row in stopped.values():
            assert row["best_bid_ask"]["provenance"] == "STALE"
            assert row["freshness"]["provenance"] == "STALE"
            assert row["last_closed_bar"]["provenance"] in ("STALE", "UNAVAILABLE")
        assert c.get("/api/status").json()["streams"]["spot"]["provenance"] == "STALE"
        assert c.get("/api/health/streams").json()["streams"]["provenance"] == "STALE"
        assert all(conn.closed for conn in ex.conns)

        # restart works and subscribes again (bars still reach the detector)
        assert c.post("/api/monitoring/start", headers=AUTH).status_code == 200
        _until(lambda: len(ex.conns) == 6)

    types = [e.type for e in store.read()]
    assert types.count(EventType.MONITORING_STARTED) == 2
    assert EventType.BACKFILL_COMPLETED in types
    assert store.verify().ok
    store.close()
