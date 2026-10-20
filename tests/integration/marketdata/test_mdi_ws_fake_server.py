"""WsManager against a real local WebSocket server (`websockets.serve` on 127.0.0.1).

Scenarios: normal flow, server drop -> reconnect, planned recycle with overlap -> no gap and no
duplicate bar, malformed message -> counted and skipped. Runs in CI (needs `websockets`).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

websockets = pytest.importorskip("websockets")

from mdi_helpers import MIN, T0, ws_kline  # noqa: E402
from websockets.asyncio.server import ServerConnection, serve  # noqa: E402

from tsclaude.contracts.market import BarSource, Market  # noqa: E402
from tsclaude.contracts.timeutil import ManualClock, SystemClock  # noqa: E402
from tsclaude.marketdata.bars import BarBuilder, SymbolBarBuilder  # noqa: E402
from tsclaude.marketdata.binance.ws import (  # noqa: E402
    ConnectionGroup,
    WsManager,
    default_connector,
)
from tsclaude.marketdata.config import WsConfig  # noqa: E402
from tsclaude.marketdata.types import (  # noqa: E402
    KlineUpdate,
    ParsedMessage,
    StreamKey,
    StreamKind,
)
from tsclaude.quality.gaps import GapRegistry  # noqa: E402
from tsclaude.store.livebars import LiveBarStore  # noqa: E402

KL = StreamKey(Market.SPOT, "BTCUSDT", StreamKind.KLINE_1M)


class FakeBinance:
    """Local server speaking Binance's combined-stream format; tests push frames per connection."""

    def __init__(self) -> None:
        self.connections: list[ServerConnection] = []
        self.paths: list[str] = []
        self.received: list[str] = []
        self.finished: set[int] = set()  # indexes of connections whose handler has ended
        self.changed = asyncio.Event()

    async def handler(self, ws: ServerConnection) -> None:
        self.paths.append(ws.request.path if ws.request is not None else "")
        self.connections.append(ws)
        index = len(self.connections) - 1
        self.changed.set()
        try:
            async for message in ws:
                self.received.append(str(message))
        except websockets.ConnectionClosed:
            pass
        finally:
            self.finished.add(index)

    async def wait_connections(self, n: int, timeout: float = 5.0) -> ServerConnection:
        async def _wait() -> ServerConnection:
            while len(self.connections) < n:
                self.changed.clear()
                await self.changed.wait()
            return self.connections[n - 1]

        return await asyncio.wait_for(_wait(), timeout)


async def push_kline(ws: ServerConnection, open_ms: int, closed: bool = True) -> None:
    await ws.send(
        json.dumps({"stream": "btcusdt@kline_1m", "data": ws_kline(open_ms, closed=closed)})
    )


async def until(pred: Callable[[], bool], timeout: float = 5.0) -> None:
    async def _poll() -> None:
        while not pred():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout)


def _config(**kw: Any) -> WsConfig:
    base: dict[str, Any] = {
        "recycle_after_s": 3600,
        "first_message_timeout_s": 3,
        "backoff_base_s": 0.05,
        "backoff_cap_s": 0.2,
        "stable_after_s": 0,
        "ping_interval_s": 20,
        "ping_timeout_s": 20,
    }
    base.update(kw)
    return WsConfig.model_validate(base)


class Harness:
    def __init__(self, tmp_path: Path, port: int, config: WsConfig) -> None:
        clock = ManualClock(T0)
        self.events: list[object] = []
        self.store = LiveBarStore(tmp_path / "state.db")
        self.gaps = GapRegistry(self.events.append, clock.now_ms)
        self.builder = SymbolBarBuilder(
            market=Market.SPOT,
            symbol="BTCUSDT",
            watch_from_ms=T0,
            store=self.store,
            gaps=self.gaps,
            emit=self.events.append,
            now_ms=clock.now_ms,
            server_now_ms=clock.now_ms,
            session_id="S",
        )
        self.router = BarBuilder()
        self.router.add(self.builder)
        self.messages: list[ParsedMessage] = []
        self.closes: list[str] = []
        group = ConnectionGroup("spot", Market.SPOT, (f"ws://127.0.0.1:{port}",), (KL,), 4)
        self.manager = WsManager(
            [group],
            self.on_message,
            config,
            SystemClock(),
            default_connector(config),
            on_close=lambda _k, reason: self.closes.append(reason),
        )

    def on_message(self, m: ParsedMessage) -> None:
        self.messages.append(m)
        if isinstance(m, KlineUpdate):
            self.router.on_kline(m)

    @property
    def stats(self):  # type: ignore[no-untyped-def]
        return self.manager.stats()[0]

    def open_times(self) -> list[int]:
        return [
            s.bar.open_time_ms for s in self.store.range(Market.SPOT, "BTCUSDT", T0, T0 + 99 * MIN)
        ]


def _run(tmp_path: Path, scenario: Callable[[FakeBinance, Harness], Any], **cfg: Any) -> None:
    async def main() -> None:
        fake = FakeBinance()
        async with serve(fake.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            h = Harness(tmp_path, port, _config(**cfg))
            h.manager.start()
            try:
                await scenario(fake, h)
            finally:
                await h.manager.stop()
                h.store.close()

    asyncio.run(asyncio.wait_for(main(), 30))


def test_normal_flow(tmp_path: Path) -> None:
    async def scenario(fake: FakeBinance, h: Harness) -> None:
        ws = await fake.wait_connections(1)
        assert fake.paths[0] == "/stream?streams=btcusdt@kline_1m"
        await push_kline(ws, T0, closed=False)
        await push_kline(ws, T0)
        await push_kline(ws, T0 + MIN)
        await until(lambda: h.open_times() == [T0, T0 + MIN])
        partial = [m for m in h.messages if isinstance(m, KlineUpdate) and not m.is_closed]
        assert len(partial) == 1
        assert {s.bar.source for s in h.store.range(Market.SPOT, "BTCUSDT", T0, T0 + 2 * MIN)} == {
            BarSource.WS_LIVE
        }

    _run(tmp_path, scenario)


def test_server_drop_reconnects(tmp_path: Path) -> None:
    async def scenario(fake: FakeBinance, h: Harness) -> None:
        ws1 = await fake.wait_connections(1)
        await push_kline(ws1, T0)
        await until(lambda: h.open_times() == [T0])
        await ws1.close(code=1011, reason="server restart")
        ws2 = await fake.wait_connections(2)
        await until(lambda: h.stats.connected and h.stats.reconnects == 1)
        assert len(h.closes) == 1
        assert h.closes[0].startswith("connection_lost")
        await push_kline(ws2, T0 + MIN)
        await until(lambda: h.open_times() == [T0, T0 + MIN])

    _run(tmp_path, scenario)


def test_planned_recycle_overlap_no_gap_no_duplicate(tmp_path: Path) -> None:
    async def scenario(fake: FakeBinance, h: Harness) -> None:
        old = await fake.wait_connections(1)
        await push_kline(old, T0)
        new = await fake.wait_connections(2, timeout=5)  # recycle after 0.5 s
        # both connections deliver the same closed minute during the overlap
        await push_kline(old, T0 + MIN)
        await push_kline(new, T0 + MIN)
        await until(lambda: h.stats.recycles == 1)
        await push_kline(new, T0 + 2 * MIN)
        await until(lambda: h.open_times() == [T0, T0 + MIN, T0 + 2 * MIN])
        await until(lambda: 0 in fake.finished)  # the old one was closed by the client
        assert h.stats.reconnects == 0
        assert h.closes == []  # streams never went down
        assert h.builder.stats.conflicts == 0
        assert h.events == []  # no gap, no conflict
        assert h.store.count() == 3

    _run(tmp_path, scenario, recycle_after_s=0.5)


def test_malformed_message_counted_and_skipped(tmp_path: Path) -> None:
    async def scenario(fake: FakeBinance, h: Harness) -> None:
        ws = await fake.wait_connections(1)
        await ws.send("{this is not json")
        await ws.send(json.dumps({"stream": "btcusdt@kline_1m", "data": {"e": "kline", "E": 1}}))
        await ws.send(json.dumps({"stream": "btcusdt@trade", "data": {"e": "trade"}}))
        await push_kline(ws, T0)
        await until(lambda: h.open_times() == [T0])
        assert h.stats.malformed == 3
        assert len(fake.connections) == 1  # the connection survived

    _run(tmp_path, scenario)


def test_control_message_reaches_server(tmp_path: Path) -> None:
    async def scenario(fake: FakeBinance, h: Harness) -> None:
        await fake.wait_connections(1)
        await until(lambda: h.stats.connected)
        await h.manager.connections[0].list_subscriptions()
        await until(lambda: len(fake.received) == 1)
        assert json.loads(fake.received[0]) == {"method": "LIST_SUBSCRIPTIONS", "id": 1}

    _run(tmp_path, scenario)
