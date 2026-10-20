"""Response models for live market data, health, gaps, bars, demo signals and notifications.

Pass-through rule (M1-T2 brief, ADR-0010): values produced by `MarketDataService.snapshot()` are
returned with their `Sourced` wrapper *unchanged* (no re-labelling in the API). Contract models
inside them (`Bar`, `BookTop`, `PartialCandle`, `Funding`) keep their epoch-millisecond fields.

Values the API derives itself are labeled here:
- `change_24h`: from two stored closed bars; carries the last bar's provenance and `as_of_ms`
  (the newest input), UNAVAILABLE when either bar is missing;
- `freshness`: worst stream state of the symbol; provenance = the stream states' own
  (SYSTEM while running, STALE after stop);
- stream/connection counters, REST weights, gap lists: SYSTEM (the app's own record) while
  monitoring runs, STALE (with `as_of_ms`) once it stopped;
- stored bars: RECOVERED for `rest_recovered`, HISTORICAL for everything else — a stored bar is a
  record, not a fresh observation; the latest live value is in `/api/markets`;
- demo signals: always DEMO.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from tsclaude.contracts.market import Bar, Market, StreamState
from tsclaude.contracts.provenance import Provenance, Sourced
from tsclaude.marketdata.models import (
    BookTop,
    ConnectionStatus,
    Funding,
    PartialCandle,
    StreamStatus,
)


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class MarketRow(_Model):
    market: Market
    symbol: str
    last_closed_bar: Sourced[Bar]
    partial_candle: Sourced[PartialCandle]
    best_bid_ask: Sourced[BookTop]
    mark_price: Sourced[float]
    funding: Sourced[Funding]
    change_24h: Sourced[float]  # fraction, e.g. 0.012 = +1.2 %
    freshness: Sourced[StreamState]
    open_gap_minutes: Sourced[int]
    exchange_status: Sourced[str]


class MarketsResponse(_Model):
    generated_at: str
    running: Sourced[bool]
    rows: list[MarketRow]


class StreamsHealthResponse(_Model):
    generated_at: str
    running: Sourced[bool]
    streams: Sourced[list[StreamStatus]]  # each element keeps its own Sourced `state`
    connections: Sourced[list[ConnectionStatus]]
    clock_offset_ms: Sourced[int]
    rest_weight: Sourced[dict[str, dict[str, tuple[int, int | None]]]]
    rest_banned: Sourced[dict[str, bool]]
    open_gaps: Sourced[int]
    open_gap_minutes: Sourced[int]
    demo_detector: Sourced[DemoDetectorInfo]


class DemoDetectorInfo(_Model):
    enabled: bool
    threshold: float
    bars_seen: int
    emitted: int
    skipped_unhealthy: int
    skipped_not_live: int
    last_skip_reason: str | None


class GapOut(_Model):
    gap_id: str
    market: Market
    symbol: str
    interval: str
    start: str  # ISO open time of the first missing minute
    end: str  # ISO, exclusive
    from_ms: int
    to_ms: int
    minutes: int
    missing_minutes: int
    missing_ranges_ms: list[tuple[int, int]]
    cause: str
    detected_at: str
    resolution: str
    resolved_at: str | None


class GapsResponse(_Model):
    generated_at: str
    gaps: Sourced[list[GapOut]]


BarProvenance = Literal["HISTORICAL", "RECOVERED"]


class BarOut(_Model):
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str
    observed_session_id: str | None
    provenance: Provenance


class BarsResponse(_Model):
    generated_at: str
    market: Market
    symbol: str
    interval: str
    from_ms: int
    to_ms: int
    bars: Sourced[list[BarOut]]
    truncated: bool


class DemoSignalOut(_Model):
    signal_id: str
    seq: int
    created_at: str
    decision_time: str
    market: Market
    symbol: str
    timeframe: str
    side: str
    stage: str
    ref_price: float
    move: float
    threshold: float
    strategy_id: str
    evidence_grade: str
    run_mode: str
    rationale: str


class SignalsResponse(_Model):
    generated_at: str
    signals: Sourced[list[DemoSignalOut]]  # always DEMO


Severity = Literal["info", "warning", "error"]


class NotificationOut(_Model):
    id: str  # "e<seq>" for events
    seq: int
    ts: str
    kind: str
    severity: Severity
    title: str
    detail: str
    market: str | None
    symbol: str | None
    provenance: Provenance  # SYSTEM, or DEMO for demo signals


class NotificationsResponse(_Model):
    generated_at: str
    notifications: Sourced[list[NotificationOut]]
    head_seq: int


StreamsHealthResponse.model_rebuild()
