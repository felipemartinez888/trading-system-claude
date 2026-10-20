"""Shared helpers for market-data unit tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tsclaude.contracts.events import NewEvent
from tsclaude.contracts.market import Interval, Market
from tsclaude.marketdata.types import KlineUpdate, RestKline

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "binance"

T0 = 1_792_411_200_000  # 2026-10-19T12:00:00Z, a 1m boundary
MIN = 60_000


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def raw_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class Sink:
    """Collects emitted events (stands in for EventStore.append)."""

    def __init__(self) -> None:
        self.events: list[NewEvent] = []

    def __call__(self, event: NewEvent) -> NewEvent:
        self.events.append(event)
        return event

    def of(self, type_: str) -> list[NewEvent]:
        return [e for e in self.events if e.type == type_]


def kline(
    open_ms: int,
    *,
    closed: bool = True,
    close: float = 100.5,
    market: Market = Market.SPOT,
    symbol: str = "BTCUSDT",
    event_ms: int | None = None,
    volume: float = 10.0,
) -> KlineUpdate:
    return KlineUpdate(
        market=market,
        symbol=symbol,
        interval=Interval.M1,
        event_time_ms=event_ms if event_ms is not None else open_ms + MIN,
        open_time_ms=open_ms,
        close_time_ms=open_ms + MIN - 1,
        open=100.0,
        high=101.0,
        low=99.0,
        close=close,
        volume=volume,
        quote_volume=volume * 100,
        trades=42,
        taker_buy_volume=volume / 2,
        taker_buy_quote_volume=volume * 50,
        is_closed=closed,
    )


def rest_kline(open_ms: int, *, close: float = 100.5, volume: float = 10.0) -> RestKline:
    return RestKline(
        open_time_ms=open_ms,
        open=100.0,
        high=101.0,
        low=99.0,
        close=close,
        volume=volume,
        close_time_ms=open_ms + MIN - 1,
        quote_volume=volume * 100,
        trades=42,
        taker_buy_volume=volume / 2,
        taker_buy_quote_volume=volume * 50,
    )


class FakeFetcher:
    """In-memory REST klines source: returns stored rows in [start, end), records calls."""

    def __init__(self, rows: dict[tuple[Market, str], dict[int, RestKline]] | None = None):
        self.rows = rows or {}
        self.calls: list[tuple[Market, str, int, int]] = []
        self.fail: Exception | None = None

    def put(self, market: Market, symbol: str, row: RestKline) -> None:
        self.rows.setdefault((market, symbol), {})[row.open_time_ms] = row

    async def __call__(self, market: Market, symbol: str, start: int, end: int) -> list[RestKline]:
        self.calls.append((market, symbol, start, end))
        if self.fail is not None:
            raise self.fail
        table = self.rows.get((market, symbol), {})
        return [table[k] for k in sorted(table) if start <= k < end]
