"""Read models exposed by `MarketDataService.snapshot()` (every value wrapped in `Sourced`).

Provenance rules (ADR-0010, DATA_ARCHITECTURE §3):
- last closed bar: LIVE only if it is `ws_live`, seen in the current session (not before its
  `watch_from`) and within the kline freshness budget while the stream is HEALTHY; a recent
  `rest_recovered` bar -> RECOVERED; other recent bars -> HISTORICAL; an old bar, or any bar while
  the kline stream is not HEALTHY -> STALE (always with `as_of_ms` = bar close time);
- forming candle -> PARTIAL (STALE when the kline stream is not HEALTHY); never a `Bar`;
- best bid/ask, mark price, funding -> LIVE while their stream is HEALTHY, else STALE with
  `as_of_ms`; UNAVAILABLE (with a reason) when nothing has been received or it does not apply;
- stream state itself -> SYSTEM while the service runs (the app's own runtime state), STALE after
  stop; exchange status -> HISTORICAL (an exchangeInfo snapshot taken at start, `as_of_ms` = load).
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


class ClockContinuity(BaseModel):
    """Monotonic-clock evidence about the clock round behind `clock_offset_ms` (P-1 hardening,
    F-1): enough for a consumer to bound exchange time now without trusting the wall clock, and to
    detect that the wall clock stepped, was suspended or slewed against it."""

    model_config = _FROZEN

    samples: int
    #: the round's smallest round trip on the wall clock (negative: the wall clock stepped back)
    min_rtt_ms: int
    #: monotonic time from the round's first send to its end
    round_span_ms: int
    #: spread of `wall - monotonic` over the round's readings (0 or 1 for continuous clocks)
    round_step_ms: int
    #: monotonic and wall time from the round's end to this snapshot
    elapsed_ms: int
    wall_elapsed_ms: int


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
    crashes: int  # connection task died unexpectedly and was restarted
    last_error: str | None


class MarketSnapshot(BaseModel):
    model_config = _FROZEN

    as_of_ms: int
    running: bool
    clock_offset_ms: Sourced[int]
    #: the largest round trip of the clock round behind `clock_offset_ms` (same `as_of_ms`): it
    #: bounds that offset's error (the paper runtime's observation anchor, P-1)
    clock_max_rtt_ms: Sourced[int] = Sourced[int].unavailable("not reported")
    #: the same round's monotonic evidence (same `as_of_ms`), measured when the snapshot was taken
    clock_continuity: Sourced[ClockContinuity] = Sourced[ClockContinuity].unavailable(
        "not reported"
    )
    rest_banned: dict[str, bool]
    streams: list[StreamStatus]
    symbols: list[SymbolSnapshot]
    connections: list[ConnectionStatus]
