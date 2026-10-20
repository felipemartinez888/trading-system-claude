"""WsManager logic with an in-memory connector (no network, no websockets library).

The same scenarios run against a real local `websockets.serve` server in
tests/integration/marketdata/test_ws_fake_server.py (CI).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

from md_helpers import MIN, T0, Sink, load_fixture

from tsclaude.contracts.market import BarSource, Market
from tsclaude.contracts.timeutil import ManualClock, SystemClock
from tsclaude.marketdata.bars import BarBuilder, SymbolBarBuilder
from tsclaude.marketdata.binance.ws import (
    ConnectionGroup,
    StreamConnection,
    WsManager,
    key_for,
)
from tsclaude.marketdata.config import WsConfig
from tsclaude.marketdata.types import KlineUpdate, ParsedMessage, StreamKey, StreamKind
from tsclaude.quality.gaps import GapRegistry
from tsclaude.store.livebars import LiveBarStore

KL = StreamKey(Market.SPOT, "BTCUSDT", StreamKind.KLINE_1M)
BK = StreamKey(Market.SPOT, "BTCUSDT", StreamKind.BOOK_TICKER)
GROUP = ConnectionGroup(
    "spot", Market.SPOT, ("wss://primary.test", "wss://fallback.test"), (KL, BK), 4
)


def _cfg(**kw: object) -> WsConfig:
    base: dict[str, object] = {
        "recycle_after_s": 3600,
        "first_message_timeout_s": 1.0,
        "recycle_retry_s": 3600,
        "backoff_base_s": 0.01,
        "backoff_cap_s": 0.02,
        "stable_after_s": 0,
        "fallback_after_failures": 2,
        "connect_budget": 50,
    }
    base.update(kw)
    return WsConfig.model_validate(base)


class FakeConn:
    def __init__(self, url: str) -> None:
        self.url = url
        self.inbox: asyncio.Queue[str | Exception] = asyncio.Queue()
        self.sent: list[str] = []
        self.closed = False

    async def recv(self) -> str:
        item = await self.inbox.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True
        self.inbox.put_nowait(ConnectionError("closed by client"))

    def push(self, payload: object) -> None:
        self.inbox.put_nowait(payload if isinstance(payload, str) else json.dumps(payload))

    def drop(self) -> None:
        self.inbox.put_nowait(ConnectionError("server dropped the connection"))


class FakeExchange:
    def __init__(self, fail_first: int = 0) -> None:
        self.conns: list[FakeConn] = []
        self.urls: list[str] = []
        self.fail_first = fail_first
        self.new_conn = asyncio.Event()

    async def connect(self, url: str) -> FakeConn:
        self.urls.append(url)
        if self.fail_first > 0:
            self.fail_first -= 1
            raise OSError("connection refused")
        conn = FakeConn(url)
        self.conns.append(conn)
        self.new_conn.set()
        return conn

    async def wait_conn(self, n: int, timeout: float = 2.0) -> FakeConn:
        async def _wait() -> FakeConn:
            while len(self.conns) < n:
                self.new_conn.clear()
                await self.new_conn.wait()
            return self.conns[n - 1]

        return await asyncio.wait_for(_wait(), timeout)


async def _until(pred: Callable[[], bool], timeout: float = 2.0) -> None:
    async def _poll() -> None:
        while not pred():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout)


def _kline_frame(open_ms: int, closed: bool = True) -> dict[str, object]:
    data = load_fixture("ws_spot_kline_closed.json")
    data["k"]["t"] = open_ms
    data["k"]["T"] = open_ms + MIN - 1
    data["k"]["x"] = closed
    data["E"] = open_ms + MIN + 5
    return {"stream": "btcusdt@kline_1m", "data": data}


def _run(coro: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(asyncio.wait_for(coro(), 10))


def test_normal_flow_parses_and_routes_messages() -> None:
    async def scenario() -> None:
        ex = FakeExchange()
        got: list[ParsedMessage] = []
        opened: list[tuple[StreamKey, ...]] = []
        conn = StreamConnection(
            GROUP,
            got.append,
            _cfg(),
            SystemClock(),
            ex.connect,
            _budget(),
            on_open=lambda keys, _r: opened.append(keys),
        )
        task = asyncio.create_task(conn.run())
        c = await ex.wait_conn(1)
        assert c.url == ("wss://primary.test/stream?streams=btcusdt@kline_1m/btcusdt@bookTicker")
        c.push(_kline_frame(T0))
        c.push({"stream": "btcusdt@bookTicker", "data": load_fixture("ws_spot_book_ticker.json")})
        await _until(lambda: len(got) == 2)
        assert [key_for(m) for m in got] == [KL, BK]
        assert opened == [(KL, BK)]
        assert conn.stats.last_message_ms.keys() == {KL, BK}
        await conn.stop()
        await task
        assert c.closed
        assert not conn.stats.connected

    _run(scenario)


def test_malformed_and_handler_errors_are_counted_and_skipped() -> None:
    async def scenario() -> None:
        ex = FakeExchange()
        got: list[ParsedMessage] = []

        def handler(m: ParsedMessage) -> None:
            if isinstance(m, KlineUpdate) and m.open_time_ms == T0 + MIN:
                raise RuntimeError("consumer bug")
            got.append(m)

        conn = StreamConnection(GROUP, handler, _cfg(), SystemClock(), ex.connect, _budget())
        task = asyncio.create_task(conn.run())
        c = await ex.wait_conn(1)
        c.push("{not json")
        c.push({"e": "kline", "E": 1})
        c.push(_kline_frame(T0 + MIN))  # handler raises
        c.push(load_fixture("ws_error_response.json"))
        c.push(_kline_frame(T0))
        await _until(lambda: len(got) == 1)
        assert conn.stats.malformed == 2
        assert conn.stats.handler_errors == 1
        assert conn.stats.control_errors == 1
        assert len(ex.conns) == 1  # the connection survived all of it
        await conn.stop()
        await task

    _run(scenario)


def test_server_drop_reconnects_and_reports_close() -> None:
    async def scenario() -> None:
        ex = FakeExchange()
        events: list[str] = []
        conn = StreamConnection(
            GROUP,
            lambda _m: None,
            _cfg(),
            SystemClock(),
            ex.connect,
            _budget(),
            on_open=lambda _k, r: events.append(f"open:{r}"),
            on_close=lambda _k, r: events.append(f"close:{r.split(':')[0]}"),
        )
        task = asyncio.create_task(conn.run())
        c1 = await ex.wait_conn(1)
        c1.push(_kline_frame(T0))
        c1.drop()
        c2 = await ex.wait_conn(2)
        await _until(lambda: conn.stats.connected and conn.stats.reconnects == 1)
        assert c1.closed
        assert c2.url == c1.url
        assert events == ["open:connected", "close:connection_lost", "open:connected"]
        await conn.stop()
        await task

    _run(scenario)


def test_connect_failures_back_off_and_fall_back() -> None:
    async def scenario() -> None:
        ex = FakeExchange(fail_first=2)
        conn = StreamConnection(
            GROUP, lambda _m: None, _cfg(), SystemClock(), ex.connect, _budget()
        )
        task = asyncio.create_task(conn.run())
        c = await ex.wait_conn(1)
        assert ex.urls[0].startswith("wss://primary.test")
        assert ex.urls[1].startswith("wss://primary.test")
        assert c.url.startswith("wss://fallback.test")  # after 2 consecutive failures
        assert conn.stats.connect_failures == 2
        assert conn.stats.fallbacks == 1
        await conn.stop()
        await task

    _run(scenario)


def test_connect_attempts_respect_shared_budget() -> None:
    async def scenario() -> None:
        clock = ManualClock(T0)
        slept: list[float] = []

        async def fake_sleep(s: float) -> None:
            slept.append(s)
            clock.advance(int(s * 1000) + 1)
            await asyncio.sleep(0)

        from tsclaude.marketdata.binance.limits import SlidingWindowLimiter

        budget = SlidingWindowLimiter(3, 300_000, clock, fake_sleep)
        ex = FakeExchange(fail_first=5)
        conn = StreamConnection(
            GROUP, lambda _m: None, _cfg(), clock, ex.connect, budget, sleep=fake_sleep
        )
        task = asyncio.create_task(conn.run())
        await ex.wait_conn(1, timeout=5)
        assert len(ex.urls) == 6
        assert any(s >= 290 for s in slept)  # waited for the 5-minute window to free slots
        await conn.stop()
        await task

    _run(scenario)


def test_planned_recycle_overlaps_without_gap_or_duplicate(tmp_path: Path) -> None:
    async def scenario() -> None:
        ex = FakeExchange()
        sink = Sink()
        clock = ManualClock(T0)
        store = LiveBarStore(tmp_path / "state.db")
        gaps = GapRegistry(sink, clock.now_ms)
        router = BarBuilder()
        builder = SymbolBarBuilder(
            market=Market.SPOT,
            symbol="BTCUSDT",
            watch_from_ms=T0,
            store=store,
            gaps=gaps,
            emit=sink,
            now_ms=clock.now_ms,
            server_now_ms=clock.now_ms,
            session_id="S",
        )
        router.add(builder)

        def handler(m: ParsedMessage) -> None:
            if isinstance(m, KlineUpdate):
                router.on_kline(m)

        conn = StreamConnection(
            GROUP, handler, _cfg(recycle_after_s=0.15), SystemClock(), ex.connect, _budget()
        )
        task = asyncio.create_task(conn.run())
        old = await ex.wait_conn(1)
        old.push(_kline_frame(T0))
        new = await ex.wait_conn(2)  # recycle opened a second connection
        assert not old.closed  # old one stays until the new one delivers
        # overlap: both deliver the close of minute 1, then the new one continues
        old.push(_kline_frame(T0 + MIN))
        new.push(_kline_frame(T0 + MIN))
        await _until(lambda: old.closed)
        new.push(_kline_frame(T0 + 2 * MIN))
        await _until(lambda: store.count() == 3)
        assert conn.stats.recycles == 1
        assert conn.stats.reconnects == 0  # a recycle is not a disconnect
        assert builder.stats.duplicates >= 1
        assert [
            s.bar.open_time_ms for s in store.range(Market.SPOT, "BTCUSDT", T0, T0 + 3 * MIN)
        ] == [
            T0,
            T0 + MIN,
            T0 + 2 * MIN,
        ]
        assert all(
            s.bar.source is BarSource.WS_LIVE
            for s in store.range(Market.SPOT, "BTCUSDT", T0, T0 + 3 * MIN)
        )
        assert sink.events == []  # no conflict, no gap
        await conn.stop()
        await task
        store.close()

    _run(scenario)


def test_recycle_keeps_old_connection_if_new_one_is_silent() -> None:
    async def scenario() -> None:
        ex = FakeExchange()
        got: list[ParsedMessage] = []
        cfg = _cfg(recycle_after_s=0.1, first_message_timeout_s=0.1, recycle_retry_s=3600)
        conn = StreamConnection(GROUP, got.append, cfg, SystemClock(), ex.connect, _budget())
        task = asyncio.create_task(conn.run())
        old = await ex.wait_conn(1)
        new = await ex.wait_conn(2)
        await _until(lambda: new.closed)
        assert not old.closed
        assert conn.stats.recycle_failures == 1
        old.push(_kline_frame(T0))
        await _until(lambda: len(got) == 1)
        await conn.stop()
        await task

    _run(scenario)


def test_control_messages_are_rate_limited() -> None:
    async def scenario() -> None:
        ex = FakeExchange()
        clock = SystemClock()
        conn = StreamConnection(GROUP, lambda _m: None, _cfg(), clock, ex.connect, _budget())
        task = asyncio.create_task(conn.run())
        c = await ex.wait_conn(1)
        await _until(lambda: conn.stats.connected)
        loop = asyncio.get_running_loop()
        start = loop.time()
        for _ in range(6):  # limit is 4/s for this group
            await conn.list_subscriptions()
        elapsed = loop.time() - start
        assert elapsed >= 0.9
        assert [json.loads(m)["method"] for m in c.sent] == ["LIST_SUBSCRIPTIONS"] * 6
        assert [json.loads(m)["id"] for m in c.sent] == [1, 2, 3, 4, 5, 6]
        await conn.stop()
        await task

    _run(scenario)


def test_manager_starts_and_stops_all_groups() -> None:
    async def scenario() -> None:
        ex = FakeExchange()
        g2 = ConnectionGroup(
            "usdm-public",
            Market.USDM,
            ("wss://fut.test/public",),
            (StreamKey(Market.USDM, "BTCUSDT", StreamKind.BOOK_TICKER),),
            8,
        )
        mgr = WsManager([GROUP, g2], lambda _m: None, _cfg(), SystemClock(), ex.connect)
        mgr.start()
        await ex.wait_conn(2)
        assert mgr.running
        await mgr.stop()
        assert not mgr.running
        assert all(c.closed for c in ex.conns)

    _run(scenario)


def _budget():  # type: ignore[no-untyped-def]
    from tsclaude.marketdata.binance.limits import SlidingWindowLimiter

    return SlidingWindowLimiter(100, 300_000, SystemClock(), asyncio.sleep)


# ---------------------------------------------------------------- review B1
def test_hostile_frames_never_end_the_read_loop() -> None:
    async def scenario() -> None:
        ex = FakeExchange()
        got: list[ParsedMessage] = []
        conn = StreamConnection(GROUP, got.append, _cfg(), SystemClock(), ex.connect, _budget())
        task = asyncio.create_task(conn.run())
        c = await ex.wait_conn(1)
        bad_trades = _kline_frame(T0)
        bad_trades["data"]["k"]["n"] = "²"  # type: ignore[index]
        c.push(bad_trades)
        c.push('{"stream": "x", "data": {"e": "kline", "E": ' + "9" * 5000 + "}}")
        c.push("[" * 100_000 + "]" * 100_000)
        c.push(_kline_frame(T0))
        await _until(lambda: len(got) == 1)
        assert conn.stats.malformed == 3
        assert len(ex.conns) == 1
        assert conn.stats.connected
        await conn.stop()
        await task

    _run(scenario)


def test_unexpected_parser_crash_is_counted_not_fatal(monkeypatch: object) -> None:
    import tsclaude.marketdata.binance.ws as ws_mod

    real = ws_mod.parse_ws_frame
    calls = {"n": 0}

    def flaky(raw: str | bytes, market: Market) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise TypeError("parser bug")
        return real(raw, market)

    async def scenario() -> None:
        ex = FakeExchange()
        got: list[ParsedMessage] = []
        conn = StreamConnection(GROUP, got.append, _cfg(), SystemClock(), ex.connect, _budget())
        task = asyncio.create_task(conn.run())
        c = await ex.wait_conn(1)
        c.push(_kline_frame(T0))
        c.push(_kline_frame(T0 + MIN))
        await _until(lambda: len(got) == 1)
        assert conn.stats.malformed == 1
        assert len(ex.conns) == 1
        await conn.stop()
        await task

    monkeypatch.setattr(ws_mod, "parse_ws_frame", flaky)  # type: ignore[attr-defined]
    _run(scenario)


def test_manager_restarts_a_crashed_connection_task() -> None:
    async def scenario() -> None:
        ex = FakeExchange()
        closes: list[str] = []
        mgr = WsManager(
            [GROUP],
            lambda _m: None,
            _cfg(),
            SystemClock(),
            ex.connect,
            on_close=lambda _k, r: closes.append(r),
        )
        conn = mgr.connections[0]
        real_serve = conn._serve
        crashed = {"done": False}

        async def crash_once() -> str:
            if not crashed["done"]:
                crashed["done"] = True
                raise RuntimeError("internal bug")
            return await real_serve()

        conn._serve = crash_once  # type: ignore[method-assign]
        mgr.start()
        await ex.wait_conn(2)
        await _until(lambda: conn.stats.connected)
        assert conn.stats.crashes == 1
        assert "internal bug" in (conn.stats.last_error or "")
        assert closes[0].startswith("internal_error")  # surfaced to stream health
        assert ex.conns[0].closed
        assert mgr.running
        await mgr.stop()
        assert not mgr.running

    _run(scenario)
