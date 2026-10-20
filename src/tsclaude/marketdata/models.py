"""Read models exposed by `MarketDataService.snapshot()` (every value wrapped in `Sourced`).

Provenance rules (ADR-0010, DATA_ARCHITECTURE §3):
- last closed bar: `ws_live` -> LIVE, `rest_recovered` -> RECOVERED, `rest_backfill`/`archive`
  -> HISTORICAL; any of them -> STALE when the kline stream is not HEALTHY;
- forming candle -> PARTIAL (STALE when the kline stream is not HEALTHY); never a `Bar`;
- best bid/ask, mark price, funding -> LIVE while their stream is HEALTHY, else STALE with
  `as_of_ms`; UNAVAILABLE (with a reason) when nothing has been received or it does not apply;
- stream state itself -> LIVE while the service runs (it is our own live observation).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from tsclaude.contracts.market import Bar, Market, StreamState
from tsclaude.contracts.provenance import Sourced

_FROZEN = ConfigDict(frozen=True)


class PartialCandle(BaseModel):
    """A forming 1m candle. Deliberately not a `Bar`: it must never be stored or used as closed."""

    model_config = _FROZEN

    market: Market
    symbol: str
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    trades: int
    event_time_ms: int


class BookTop(BaseModel):
    model_config = _FROZEN

    bid_price: float
    bid_qty: float
    ask_price: float
    ask_qty: float
    update_id: int
    received_at_ms: int
    event_time_ms: int | None  # Binance spot bookTicker carries no event time


class Funding(BaseModel):
    model_config = _FROZEN

    rate: float
    next_funding_time_ms: int | None
    source: str  # "ws_mark_price" or "rest_premium_index"


class StreamStatus(BaseModel):
    model_config = _FROZEN

    stream: str
    market: Market
    symbol: str
    kind: str
    state: Sourced[StreamState]
    since_ms: int
    budget_ms: int
    last_message_ms: int | None
    last_fresh_ms: int | None
    disconnects: int


class SymbolSnapshot(BaseModel):
    model_config = _FROZEN

    market: Market
    symbol: str
    last_closed_bar: Sourced[Bar]
    partial_candle: Sourced[PartialCandle]
    best_bid_ask: Sourced[BookTop]
    mark_price: Sourced[float]
    funding: Sourced[Funding]
    open_gap_minutes: int
    exchange_status: Sourced[str]


class ConnectionStatus(BaseModel):
    model_config = _FROZEN

    group: str
    url: str | None
    connected: bool
    connect_attempts: int
    reconnects: int
    recycles: int
    malformed: int
    messages: int


class MarketSnapshot(BaseModel):
    model_config = _FROZEN

    as_of_ms: int
    running: bool
    clock_offset_ms: Sourced[int]
    rest_banned: dict[str, bool]
    streams: list[StreamStatus]
    symbols: list[SymbolSnapshot]
    connections: list[ConnectionStatus]
