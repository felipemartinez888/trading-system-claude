"""MarketDataService end to end: MockTransport REST + in-memory WS connector, ManualClock.

Covers start (exchangeInfo, clock, backfill), live closes, partial candles, REST recovery of a late
minute, a gap, an induced disconnect -> recovery, snapshot provenance, subscriptions and stop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mdi_helpers import require

httpx = require("httpx")

from mdi_helpers import MIN, T0, FakeExchange, kline_row, load_fixture, ws_kline  # noqa: E402

from tsclaude.contracts.events import Event, EventType  # noqa: E402
from tsclaude.contracts.market import BarSource, Market, StreamState  # noqa: E402
from tsclaude.contracts.provenance import Provenance  # noqa: E402
from tsclaude.contracts.timeutil import ManualClock  # noqa: E402
from tsclaude.marketdata.bars import BarClosed  # noqa: E402
from tsclaude.marketdata.config import MarketDataConfig  # noqa: E402
from tsclaude.marketdata.service import MarketDataService  # noqa: E402
from tsclaude.marketdata.types import StreamKey, StreamKind  # noqa: E402
from tsclaude.store.eventstore import EventStore  # noqa: E402
from tsclaude.store.livebars import LiveBarStore  # noqa: E402

SPOT, USDM = Market.SPOT, Market.USDM
LOOKBACK_MIN = 30
MISSING_IN_REST = {(SPOT, T0 + 2 * MIN)}

CONFIG = MarketDataConfig.from_mapping(
    {
        "version": 0,
        "snapshot_date": "2026-10-19",
        "rule": "test",
        "spot": ["BTCUSDT"],
        "usdm": ["BTCUSDT"],
    },
    {
        # budgets large so the background tick cannot flip states between test steps
        "freshness": {
            "kline_1m_s": 3600,
            "book_ticker_s": 3600,
            "mark_price_s": 3600,
            "tick_interval_s": 3600,
        },
        "watermark": {"check_interval_s": 3600, "gap_retry_interval_s": 0.001},
        "clock": {"sample_interval_s": 3600, "samples_per_round": 3},
        "backfill": {"lookback_days": LOOKBACK_MIN / 1440, "max_backfill_days": 1},
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
            market = SPOT if path.startswith("/api") else USDM
            start, end, limit = (
                int(params["startTime"]),
                int(params["endTime"]),
                int(params["limit"]),
            )
            now = clock.now_ms()
            rows = []
            t = start - start % MIN + (MIN if start % MIN else 0)
            while t <= end and len(rows) < limit:
                # Binance also returns the forming minute; the client must drop it itself
                if t <= now and (market, t) not in MISSING_IN_REST:
                    rows.append(kline_row(t))
                t += MIN
            return httpx.Response(200, json=rows, headers={"X-MBX-USED-WEIGHT-1M": "10"})
        return httpx.Response(404)

    return handler


async def _until(pred: Callable[[], bool], timeout: float = 3.0) -> None:
    async def _poll() -> None:
        while not pred():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout)


def _types(events: list[Event]) -> list[str]:
    return [str(e.type) for e in events]


def test_service_end_to_end(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = ManualClock(T0 + 30_000)  # mid-minute: T0 is the forming minute at start
        store = EventStore(tmp_path / "state.db")
        ex = FakeExchange()
        svc = MarketDataService(
            CONFIG,
            store,
            clock,
            http_transport=httpx.MockTransport(_exchange(clock)),
            ws_connect=ex.connect,
            session_id="SESSION1",
        )
        sub = svc.subscribe()
        await svc.start()
        assert svc.watch_from_ms == T0
        await svc.wait_backfill(5)
        await _until(lambda: len(ex.conns) == 3)
        spot = ex.latest("spot.test")
        fmarket = ex.latest("fut.test/market")
        fpublic = ex.latest("fut.test/public")
        assert "btcusdt@kline_1m/btcusdt@bookTicker" in spot.url
        assert "btcusdt@kline_1m/btcusdt@markPrice@1s" in fmarket.url
        assert fpublic.url.endswith("streams=btcusdt@bookTicker")

        # --- backfill: exactly the closed minutes before watch_from, tagged rest_backfill
        bars = LiveBarStore(store.path)
        for m in (SPOT, USDM):
            got = bars.range(m, "BTCUSDT", T0 - LOOKBACK_MIN * MIN, T0 + MIN)
            assert [s.bar.open_time_ms for s in got] == [
                T0 - i * MIN for i in range(LOOKBACK_MIN, 0, -1)
            ]
            assert {s.bar.source for s in got} == {BarSource.REST_BACKFILL}
        assert len(store.read(types=[EventType.BACKFILL_COMPLETED])) == 2

        snap = svc.snapshot()
        spot_snap = next(s for s in snap.symbols if s.market is SPOT)
        assert spot_snap.last_closed_bar.provenance is Provenance.STALE  # stream not healthy yet
        assert spot_snap.partial_candle.provenance is Provenance.UNAVAILABLE
        assert spot_snap.mark_price.provenance is Provenance.UNAVAILABLE
        assert spot_snap.exchange_status.value == "TRADING"
        # review N4: a startup snapshot, not a live value
        assert spot_snap.exchange_status.provenance is Provenance.HISTORICAL
        assert spot_snap.exchange_status.as_of_ms == T0 + 30_000
        assert all(st.state.provenance is Provenance.SYSTEM for st in snap.streams)
        assert snap.clock_offset_ms.value == 0

        # --- live: partial, book, mark
        spot.push(ws_kline(T0, closed=False), "btcusdt@kline_1m")
        spot.push(load_fixture("ws_spot_book_ticker.json"), "btcusdt@bookTicker")
        fmarket.push(ws_kline(T0, closed=False), "btcusdt@kline_1m")
        fmarket.push(load_fixture("ws_usdm_mark_price.json"), "btcusdt@markPrice@1s")
        fpublic.push(load_fixture("ws_usdm_book_ticker.json"), "btcusdt@bookTicker")
        assert svc.freshness is not None
        fresh = svc.freshness
        keys = fresh.keys()
        await _until(lambda: all(fresh.state(k) is StreamState.HEALTHY for k in keys))
        snap = svc.snapshot()
        spot_snap = next(s for s in snap.symbols if s.market is SPOT)
        fut_snap = next(s for s in snap.symbols if s.market is USDM)
        assert spot_snap.partial_candle.provenance is Provenance.PARTIAL
        assert spot_snap.last_closed_bar.provenance is Provenance.HISTORICAL
        assert spot_snap.best_bid_ask.provenance is Provenance.LIVE
        assert fut_snap.mark_price.provenance is Provenance.LIVE
        assert fut_snap.mark_price.value == pytest.approx(67234.12)
        assert fut_snap.funding.value is not None
        assert fut_snap.funding.value.source == "ws_mark_price"
        assert bars.get(SPOT, "BTCUSDT", T0) is None  # partial never stored

        # --- spot closes T0 live; usdm never sends its close -> REST recovery after the watermark
        clock.set(T0 + MIN + 100)
        spot.push(ws_kline(T0), "btcusdt@kline_1m")
        await _until(lambda: bars.get(SPOT, "BTCUSDT", T0) is not None)
        stored = bars.get(SPOT, "BTCUSDT", T0)
        assert stored is not None
        assert stored.bar.source is BarSource.WS_LIVE
        assert stored.observed_session_id == "SESSION1"
        clock.set(T0 + MIN + 5_001)
        await svc.check_watermarks()
        fut_bar = bars.get(USDM, "BTCUSDT", T0)
        assert fut_bar is not None
        assert fut_bar.bar.source is BarSource.REST_RECOVERED
        late = store.read(types=[EventType.BAR_LATE])
        assert [e.payload["market"] for e in late] == ["usdm"]
        snap = svc.snapshot()
        assert next(s for s in snap.symbols if s.market is SPOT).last_closed_bar.provenance is (
            Provenance.LIVE
        )
        assert next(s for s in snap.symbols if s.market is USDM).last_closed_bar.provenance is (
            Provenance.RECOVERED
        )

        # --- induced disconnect: spot drops while minutes T0+1 and T0+2 close
        spot.drop()
        spot_kline = StreamKey(SPOT, "BTCUSDT", StreamKind.KLINE_1M)
        await _until(lambda: len(ex.conns) == 4)
        await _until(lambda: fresh.state(spot_kline) is StreamState.RECOVERING)
        spot2 = ex.latest("spot.test")
        clock.set(T0 + 3 * MIN + 5_001)
        fmarket.push(ws_kline(T0 + MIN), "btcusdt@kline_1m")
        fmarket.push(ws_kline(T0 + 2 * MIN), "btcusdt@kline_1m")
        await _until(lambda: bars.get(USDM, "BTCUSDT", T0 + 2 * MIN) is not None)
        await svc.check_watermarks()  # spot T0+1 recovered; T0+2 missing in REST -> gap
        assert bars.get(SPOT, "BTCUSDT", T0 + MIN) is not None
        gaps = store.read(types=[EventType.GAP_DETECTED])
        assert [(e.payload["market"], e.payload["from_ms"], e.payload["cause"]) for e in gaps] == [
            ("spot", T0 + 2 * MIN, "disconnect")
        ]
        spot2.push(ws_kline(T0 + 3 * MIN, closed=False), "btcusdt@kline_1m")
        # still RECOVERING: the gap has automatic retries left
        await asyncio.sleep(0.05)
        assert fresh.state(spot_kline) is StreamState.RECOVERING
        MISSING_IN_REST.discard((SPOT, T0 + 2 * MIN))  # exchange now serves the minute
        clock.advance(1_000)  # gap retries are time-gated
        try:
            await svc.check_watermarks()  # gap retry
        finally:
            MISSING_IN_REST.add((SPOT, T0 + 2 * MIN))
        assert len(store.read(types=[EventType.GAP_RESOLVED])) == 1
        spot2.push(ws_kline(T0 + 3 * MIN, closed=False), "btcusdt@kline_1m")
        await _until(lambda: fresh.state(spot_kline) is StreamState.HEALTHY)

        transitions = [
            (e.payload["from"], e.payload["to"])
            for e in store.read(types=[EventType.STREAM_STATE_CHANGED])
            if e.aggregate_id == spot_kline.id
        ]
        assert transitions == [
            ("CONNECTING", "HEALTHY"),
            ("HEALTHY", "DISCONNECTED"),
            ("DISCONNECTED", "RECOVERING"),
            ("RECOVERING", "HEALTHY"),
        ]

        # --- subscription saw bar closes and market-data events, in order
        updates: list[object] = []
        while (item := await sub.get(timeout=0.01)) is not None:
            updates.append(item)
        closed_bars = [u.bar for u in updates if isinstance(u, BarClosed)]
        assert (SPOT, T0, BarSource.WS_LIVE) in [
            (b.market, b.open_time_ms, b.source) for b in closed_bars
        ]
        evs = [u for u in updates if isinstance(u, Event)]
        for t in ("bar_late", "gap_detected", "gap_resolved", "stream_state_changed"):
            assert t in _types(evs)

        await svc.stop()
        assert not svc.running
        assert all(c.closed for c in ex.conns)
        snap = svc.snapshot()
        assert all(s.state.provenance is Provenance.STALE for s in snap.streams)
        assert all(
            s.last_closed_bar.provenance in (Provenance.STALE, Provenance.UNAVAILABLE)
            for s in snap.symbols
        )
        assert store.verify().ok
        svc.close()
        assert next(iter(svc.snapshot().symbols)).last_closed_bar.provenance is (
            Provenance.UNAVAILABLE
        )
        bars.close()
        store.close()

    asyncio.run(asyncio.wait_for(scenario(), 30))


def test_restart_resumes_and_registers_unobserved_interval(tmp_path: Path) -> None:
    """Hard kill -> restart: the hole since the last stored bar is a gap, then backfilled."""

    async def run_once(clock: ManualClock) -> MarketDataService:
        store = EventStore(tmp_path / "state.db")
        svc = MarketDataService(
            CONFIG,
            store,
            clock,
            http_transport=httpx.MockTransport(_exchange(clock)),
            ws_connect=FakeExchange().connect,
        )
        await svc.start()
        await svc.wait_backfill(5)
        return svc

    async def scenario() -> None:
        clock = ManualClock(T0 + 30_000)
        svc = await run_once(clock)
        await svc.stop()  # stands in for the killed process (its state.db survives)
        svc.close()
        clock.set(T0 + 20 * MIN + 30_000)
        svc2 = await run_once(clock)
        bars = LiveBarStore(tmp_path / "state.db")
        got = [s.bar.open_time_ms for s in bars.range(SPOT, "BTCUSDT", T0, T0 + 20 * MIN)]
        expected = [T0 + i * MIN for i in range(20) if (SPOT, T0 + i * MIN) not in MISSING_IN_REST]
        assert got == expected
        store = EventStore(tmp_path / "state.db")
        gaps = store.read(types=[EventType.GAP_DETECTED])
        spot_gaps = [
            (e.payload["from_ms"], e.payload["to_ms"], e.payload["cause"])
            for e in gaps
            if e.payload["market"] == "spot"
        ]
        assert spot_gaps == [(T0, T0 + 20 * MIN, "downtime")]
        res = next(r for r in svc2.backfill_results if r.market is SPOT)
        assert res.remaining_open_minutes == 1  # the minute REST does not have stays visible
        await svc2.stop()
        svc2.close()
        bars.close()
        store.close()

    asyncio.run(asyncio.wait_for(scenario(), 30))


@pytest.mark.parametrize(
    ("restart_at", "expected"),
    [
        (T0 + 3 * 1440 * MIN + 30_000, Provenance.STALE),  # three days later: old bar
        (T0 + MIN + 20_000, Provenance.HISTORICAL),  # within budget, but another session's bar
    ],
    ids=["days-later", "immediately"],
)
def test_old_bar_is_never_labelled_live_after_restart(
    tmp_path: Path, restart_at: int, expected: Provenance
) -> None:
    """Review B2: after a restart with failing backfill, a healthy stream must not make the
    previous session's last ws_live bar LIVE."""

    def failing_klines(clock: ManualClock) -> Callable[[Any], Any]:
        ok = _exchange(clock)

        def handler(request: Any) -> Any:
            if request.url.path.endswith("/klines"):
                return httpx.Response(400, json={"code": -1, "msg": "test: klines unavailable"})
            return ok(request)

        return handler

    async def scenario() -> None:
        clock = ManualClock(T0 + 30_000)
        store = EventStore(tmp_path / "state.db")
        ex1 = FakeExchange()
        svc1 = MarketDataService(
            CONFIG,
            store,
            clock,
            http_transport=httpx.MockTransport(_exchange(clock)),
            ws_connect=ex1.connect,
            session_id="OLD",
        )
        await svc1.start()
        await svc1.wait_backfill(5)
        await _until(lambda: len(ex1.conns) == 3)
        clock.set(T0 + MIN + 100)
        ex1.latest("spot.test").push(ws_kline(T0), "btcusdt@kline_1m")
        bars = LiveBarStore(store.path)
        await _until(lambda: bars.get(SPOT, "BTCUSDT", T0) is not None)
        await svc1.stop()
        svc1.close()

        clock.set(restart_at)
        ex2 = FakeExchange()
        svc2 = MarketDataService(
            CONFIG,
            store,
            clock,
            http_transport=httpx.MockTransport(failing_klines(clock)),
            ws_connect=ex2.connect,
            session_id="NEW",
        )
        await svc2.start()
        await svc2.wait_backfill(5)
        await _until(lambda: len(ex2.conns) == 3)
        ex2.latest("spot.test").push(
            ws_kline(clock.now_ms() - 30_000, closed=False), "btcusdt@kline_1m"
        )
        key = StreamKey(SPOT, "BTCUSDT", StreamKind.KLINE_1M)
        assert svc2.freshness is not None
        fresh = svc2.freshness
        await _until(lambda: fresh.state(key) is StreamState.HEALTHY)
        snap = next(s for s in svc2.snapshot().symbols if s.market is SPOT)
        assert snap.last_closed_bar.value is not None
        assert snap.last_closed_bar.value.open_time_ms == T0
        assert snap.last_closed_bar.provenance is expected
        assert snap.last_closed_bar.as_of_ms == T0 + MIN - 1
        await svc2.stop()
        svc2.close()
        bars.close()
        store.close()

    asyncio.run(asyncio.wait_for(scenario(), 30))


def test_failed_start_closes_http_clients(tmp_path: Path) -> None:
    """Review N5: an exception during start() must not leak httpx clients or tasks."""

    async def scenario() -> None:
        clock = ManualClock(T0 + 30_000)
        store = EventStore(tmp_path / "state.db")
        svc = MarketDataService(
            CONFIG,
            store,
            clock,
            http_transport=httpx.MockTransport(_exchange(clock)),
            ws_connect=FakeExchange().connect,
        )

        async def boom(symbol: str) -> None:
            raise RuntimeError("unexpected failure during start")

        svc._load_premium_index = boom  # type: ignore[method-assign]
        clients: list[Any] = []
        real_start = svc._start

        async def spy() -> None:
            try:
                await real_start()
            finally:
                clients.extend(r._client for r in svc._rest.values())

        svc._start = spy  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="unexpected failure"):
            await svc.start()
        assert len(clients) == 2
        assert all(c.is_closed for c in clients)
        assert not svc.running
        assert svc.builder.all() == []
        svc.close()
        store.close()

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_start_stop_start_on_the_same_service(tmp_path: Path) -> None:
    """Re-review R4: monitoring can be stopped and started again within one process."""

    async def scenario() -> None:
        clock = ManualClock(T0 + 30_000)
        store = EventStore(tmp_path / "state.db")
        ex = FakeExchange()
        svc = MarketDataService(
            CONFIG,
            store,
            clock,
            http_transport=httpx.MockTransport(_exchange(clock)),
            ws_connect=ex.connect,
            session_id="RUN1",
        )
        bars = LiveBarStore(store.path)
        for run, session in ((1, "RUN1"), (2, "RUN2")):
            sub = svc.subscribe()
            await svc.start(session_id=None if run == 1 else session)
            await svc.wait_backfill(5)
            await _until(lambda n=run: len(ex.conns) == 3 * n)
            minute = svc.watch_from_ms
            assert minute is not None
            ex.latest("spot.test").push(ws_kline(minute), "btcusdt@kline_1m")
            await _until(lambda m=minute: bars.get(SPOT, "BTCUSDT", m) is not None)
            stored = bars.get(SPOT, "BTCUSDT", minute)
            assert stored is not None
            assert stored.observed_session_id == session
            assert await sub.get(timeout=1) is not None  # the new subscription receives updates
            await svc.stop()
            assert all(c.closed for c in ex.conns)
            clock.advance(10 * MIN)
        assert len(store.read(types=[EventType.BACKFILL_COMPLETED])) == 4  # 2 runs x 2 markets
        # the 10 minutes between the runs were backfilled by the second start
        between = bars.range(SPOT, "BTCUSDT", T0 + MIN, T0 + 10 * MIN)
        assert len(between) == 9 - sum(
            1 for m, t in MISSING_IN_REST if m is SPOT and T0 + MIN <= t < T0 + 10 * MIN
        )
        svc.close()
        bars.close()
        store.close()

    asyncio.run(asyncio.wait_for(scenario(), 30))
