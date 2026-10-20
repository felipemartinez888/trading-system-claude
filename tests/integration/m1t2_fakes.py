"""Fake `MarketDataService` for API tests (no network). Implements `MarketDataLike`.

Tests drive it directly: set stream states and field provenance, push closed bars to subscribers,
make `start()` fail. Also importable from child processes (lifecycle tests).
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any

from tsclaude.contracts.events import Event
from tsclaude.contracts.market import Bar, BarSource, Interval, Market, StreamState
from tsclaude.contracts.provenance import Provenance, Sourced
from tsclaude.contracts.timeutil import MS_PER_MINUTE, WallClock
from tsclaude.marketdata.bars import BarClosed
from tsclaude.marketdata.models import (
    BookTop,
    ConnectionStatus,
    Funding,
    MarketSnapshot,
    PartialCandle,
    StreamStatus,
    SymbolSnapshot,
)
from tsclaude.store.eventstore import EventStore

PAIRS = [(Market.SPOT, "BTCUSDT"), (Market.USDM, "BTCUSDT")]
KINDS = {
    Market.SPOT: ["kline_1m", "book_ticker"],
    Market.USDM: ["kline_1m", "book_ticker", "mark_price"],
}


def make_bar(
    market: Market,
    symbol: str,
    open_ms: int,
    *,
    open_: float = 100.0,
    close: float = 100.5,
    source: BarSource = BarSource.WS_LIVE,
) -> Bar:
    return Bar(
        market=market,
        symbol=symbol,
        interval=Interval.M1,
        open_time_ms=open_ms,
        close_time_ms=open_ms + MS_PER_MINUTE - 1,
        open=open_,
        high=max(open_, close) + 1,
        low=min(open_, close) - 1,
        close=close,
        volume=10.0,
        quote_volume=1000.0,
        trades=42,
        taker_buy_volume=5.0,
        taker_buy_quote_volume=500.0,
        source=source,
        ingested_at_ms=open_ms + MS_PER_MINUTE,
    )


class FakeSubscription:
    def __init__(self, owner: FakeMarketData) -> None:
        self._owner = owner
        self.queue: asyncio.Queue[BarClosed | Event | None] = asyncio.Queue()
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self._owner.subscriptions.discard(self)
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait(None)

    async def get(self, timeout: float | None = None) -> BarClosed | Event | None:
        try:
            return await asyncio.wait_for(self.queue.get(), timeout)
        except TimeoutError:
            return None


@dataclass
class _Weights:
    windows: dict[str, tuple[int, int | None]] = field(default_factory=lambda: {"1M": (12, 6000)})


@dataclass
class _Rest:
    weights: _Weights = field(default_factory=_Weights)


class FakeMarketData:
    def __init__(self, store: EventStore, clock: WallClock) -> None:
        self.store = store
        self.clock = clock
        self._running = False
        self.closed = False
        self.fail_start: str | None = None
        self.start_calls: list[str | None] = []
        self.stop_calls = 0
        self.subscriptions: set[FakeSubscription] = set()
        self.subscribed_total = 0
        self.state: dict[tuple[Market, str, str], StreamState] = {
            (m, s, k): StreamState.HEALTHY for m, s in PAIRS for k in KINDS[m]
        }
        self.bars: dict[tuple[Market, str], Sourced[Bar]] = {}
        # Provenance overrides per field, to check the API passes them through unchanged.
        self.book_provenance = Provenance.LIVE
        self.crashes = 0

    # ------------------------------------------------------------------ protocol
    @property
    def running(self) -> bool:
        return self._running

    async def start(self, session_id: str | None = None) -> None:
        self.start_calls.append(session_id)
        await asyncio.sleep(0)
        if self.fail_start is not None:
            raise ConnectionError(self.fail_start)
        self._running = True

    async def stop(self) -> None:
        self.stop_calls += 1
        self._running = False
        for sub in list(self.subscriptions):
            sub.close()

    def close(self) -> None:
        self.closed = True

    def subscribe(self, maxsize: int = 10_000) -> FakeSubscription:
        sub = FakeSubscription(self)
        self.subscriptions.add(sub)
        self.subscribed_total += 1
        return sub

    def rest(self, market: Market) -> _Rest:
        return _Rest()

    def snapshot(self) -> MarketSnapshot:
        now = self.clock.now_ms()
        streams = [
            StreamStatus(
                stream=f"{m}:{s}:{k}",
                market=m,
                symbol=s,
                kind=k,
                state=(
                    Sourced[StreamState](value=st, provenance=Provenance.SYSTEM, as_of_ms=now)
                    if self._running
                    else Sourced[StreamState](value=st, provenance=Provenance.STALE, as_of_ms=now)
                ),
                since_ms=now,
                budget_ms=75_000,
                last_message_ms=now,
                last_fresh_ms=now,
                disconnects=0,
            )
            for (m, s, k), st in self.state.items()
        ]
        symbols = []
        for m, s in PAIRS:
            book = BookTop(
                bid_price=100.0,
                bid_qty=1.0,
                ask_price=100.1,
                ask_qty=1.0,
                update_id=1,
                received_at_ms=now,
                event_time_ms=None,
            )
            prov = self.book_provenance if self._running else Provenance.STALE
            symbols.append(
                SymbolSnapshot(
                    market=m,
                    symbol=s,
                    last_closed_bar=self.bars.get(
                        (m, s), Sourced[Bar].unavailable("no closed bar stored yet")
                    ),
                    partial_candle=Sourced[PartialCandle].unavailable("no forming candle received"),
                    best_bid_ask=Sourced[BookTop](value=book, provenance=prov, as_of_ms=now),
                    mark_price=(
                        Sourced[float](value=100.05, provenance=prov, as_of_ms=now)
                        if m is Market.USDM
                        else Sourced[float].unavailable("spot has no mark price")
                    ),
                    funding=(
                        Sourced[Funding](
                            value=Funding(rate=0.0001, next_funding_time_ms=now, source="ws"),
                            provenance=prov,
                            as_of_ms=now,
                        )
                        if m is Market.USDM
                        else Sourced[Funding].unavailable("spot has no funding")
                    ),
                    open_gap_minutes=0,
                    exchange_status=Sourced[str](
                        value="TRADING", provenance=Provenance.HISTORICAL, as_of_ms=now
                    ),
                )
            )
        return MarketSnapshot(
            as_of_ms=now,
            running=self._running,
            clock_offset_ms=Sourced[int](value=3, provenance=Provenance.LIVE, as_of_ms=now),
            rest_banned={"spot": False, "usdm": False},
            streams=streams,
            symbols=symbols,
            connections=[
                ConnectionStatus(
                    group="spot",
                    url="wss://spot.test",
                    connected=self._running,
                    connect_attempts=1,
                    reconnects=0,
                    recycles=0,
                    malformed=0,
                    messages=10,
                    crashes=self.crashes,
                    last_error=None,
                )
            ],
        )

    # ------------------------------------------------------------------ test drivers
    def push_bar(self, bar: Bar) -> None:
        for sub in list(self.subscriptions):
            sub.queue.put_nowait(BarClosed(bar))


class FakeFactory:
    """`ServiceFactory` that remembers the instance it created."""

    def __init__(self, fail_start: str | None = None) -> None:
        self.instance: FakeMarketData | None = None
        self.fail_start = fail_start

    def __call__(self, store: EventStore, clock: WallClock) -> Any:
        self.instance = FakeMarketData(store, clock)
        self.instance.fail_start = self.fail_start
        return self.instance
