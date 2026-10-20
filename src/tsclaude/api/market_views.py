"""Builders for the market-data read models (pure functions over snapshot, stores and events).

Kept free of FastAPI so they are unit-testable everywhere. See `market_schemas.py` for the
provenance rules; values from `MarketDataService.snapshot()` are passed through unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from tsclaude.api.market_schemas import (
    BarOut,
    BarsResponse,
    DemoDetectorInfo,
    DemoSignalOut,
    GapOut,
    GapsResponse,
    MarketRow,
    MarketsResponse,
    NotificationOut,
    NotificationsResponse,
    Severity,
    SignalsResponse,
    StreamsHealthResponse,
)
from tsclaude.contracts.events import Event, EventType
from tsclaude.contracts.market import BarSource, Market, StreamState
from tsclaude.contracts.provenance import Provenance, Sourced
from tsclaude.contracts.timeutil import MS_PER_DAY, MS_PER_MINUTE, to_iso
from tsclaude.marketdata.models import (
    ConnectionStatus,
    MarketSnapshot,
    StreamStatus,
    SymbolSnapshot,
)
from tsclaude.quality.gaps import GapRegistry
from tsclaude.runtime.supervisor import NOT_STARTED, MonitoringSupervisor
from tsclaude.signals.demo import STRATEGY_ID as DEMO_STRATEGY_ID
from tsclaude.store.eventstore import EventStore
from tsclaude.store.livebars import LiveBarStore

log = logging.getLogger(__name__)

MAX_BARS = 5_000
type RestWeights = dict[str, dict[str, tuple[int, int | None]]]
DEFAULT_BARS_WINDOW_MS = 6 * 60 * MS_PER_MINUTE
NOTIFICATION_TYPES = (
    EventType.STREAM_STATE_CHANGED,
    EventType.GAP_DETECTED,
    EventType.GAP_RESOLVED,
    EventType.SESSION_CRASH_DETECTED,
    EventType.SIGNAL_CREATED,
    EventType.CLOCK_SKEW_CHANGED,
    EventType.BAR_CONFLICT,
)
_SEVERITY = {
    StreamState.HEALTHY: 0,
    StreamState.CONNECTING: 1,
    StreamState.RECOVERING: 2,
    StreamState.STALE: 3,
    StreamState.DISCONNECTED: 4,
}


def _system[T](value: T, running: bool, as_of_ms: int, stopped_as_of_ms: int | None) -> Sourced[T]:
    """SYSTEM while monitoring runs; STALE (last known) after it stopped."""
    if running:
        return Sourced[T](value=value, provenance=Provenance.SYSTEM, as_of_ms=as_of_ms)
    return Sourced[T](
        value=value,
        provenance=Provenance.STALE,
        as_of_ms=stopped_as_of_ms if stopped_as_of_ms is not None else as_of_ms,
        reason="monitoring stopped; last known value",
    )


# ---------------------------------------------------------------------- markets
def freshness_of(streams: Iterable[StreamStatus]) -> Sourced[StreamState]:
    items = list(streams)
    if not items:
        return Sourced[StreamState].unavailable("no streams for this symbol")
    worst = max(items, key=lambda s: _SEVERITY.get(s.state.value, 5) if s.state.value else 5)
    stale = [s.state for s in items if s.state.provenance is not Provenance.SYSTEM]
    if worst.state.value is None:
        return Sourced[StreamState].unavailable(worst.state.reason or "stream state unknown")
    if stale:
        as_of = min((st.as_of_ms for st in stale if st.as_of_ms is not None), default=None)
        return Sourced[StreamState](
            value=worst.state.value,
            provenance=Provenance.STALE,
            as_of_ms=as_of if as_of is not None else worst.since_ms,
            reason=stale[0].reason,
        )
    return Sourced[StreamState](
        value=worst.state.value, provenance=Provenance.SYSTEM, as_of_ms=worst.state.as_of_ms
    )


# Value provenances from strongest to weakest; a value derived from two inputs carries the weaker.
_STRENGTH = {
    Provenance.LIVE: 0,
    Provenance.RECOVERED: 1,
    Provenance.HISTORICAL: 2,
    Provenance.STALE: 3,
}


def reference_provenance(source: BarSource) -> Provenance:
    """A stored reference bar: seen live, recovered via REST, or backfill/archive."""
    if source is BarSource.WS_LIVE:
        return Provenance.LIVE
    if source is BarSource.REST_RECOVERED:
        return Provenance.RECOVERED
    return Provenance.HISTORICAL


def weaker(a: Provenance, b: Provenance) -> Provenance:
    return a if _STRENGTH.get(a, 3) >= _STRENGTH.get(b, 3) else b


def change_24h(sym: SymbolSnapshot, bars: LiveBarStore | None) -> Sourced[float]:
    """close(last) / close(bar 24 h earlier) - 1, labeled with the *weaker* input's provenance;
    `reason` names the reference bar's source so a backfilled baseline is visible."""
    last = sym.last_closed_bar
    if last.value is None or last.provenance is Provenance.UNAVAILABLE:
        return Sourced[float].unavailable(last.reason or "no closed bar stored yet")
    if bars is None:
        return Sourced[float].unavailable("bar store not available")
    ref_open = last.value.open_time_ms - MS_PER_DAY
    ref = bars.get(sym.market, sym.symbol, ref_open)
    if ref is None:
        return Sourced[float].unavailable(
            "no stored 1m bar exactly 24 h before the last close (history or gap)"
        )
    value = last.value.close / ref.bar.close - 1.0
    provenance = weaker(last.provenance, reference_provenance(ref.bar.source))
    as_of = last.as_of_ms if last.as_of_ms is not None else last.value.close_time_ms
    return Sourced[float](
        value=value,
        provenance=provenance,
        as_of_ms=as_of,
        reason=f"24 h reference bar source: {ref.bar.source}",
    )


def market_rows(snapshot: MarketSnapshot, bars: LiveBarStore | None) -> list[MarketRow]:
    rows: list[MarketRow] = []
    for sym in snapshot.symbols:
        streams = [s for s in snapshot.streams if s.market is sym.market and s.symbol == sym.symbol]
        gap_minutes = _system(
            sym.open_gap_minutes, snapshot.running, snapshot.as_of_ms, snapshot.as_of_ms
        )
        rows.append(
            MarketRow(
                market=sym.market,
                symbol=sym.symbol,
                last_closed_bar=sym.last_closed_bar,
                partial_candle=sym.partial_candle,
                best_bid_ask=sym.best_bid_ask,
                mark_price=sym.mark_price,
                funding=sym.funding,
                change_24h=change_24h(sym, bars),
                freshness=freshness_of(streams),
                open_gap_minutes=gap_minutes,
                exchange_status=sym.exchange_status,
            )
        )
    return rows


def markets_response(
    supervisor: MonitoringSupervisor, bars: LiveBarStore | None, now_ms: int
) -> MarketsResponse:
    snapshot = supervisor.snapshot()
    if snapshot is None:
        return MarketsResponse(
            generated_at=to_iso(now_ms),
            running=Sourced[bool](value=False, provenance=Provenance.SYSTEM, as_of_ms=now_ms),
            rows=[],
        )
    return MarketsResponse(
        generated_at=to_iso(now_ms),
        running=Sourced[bool](
            value=snapshot.running, provenance=Provenance.SYSTEM, as_of_ms=now_ms
        ),
        rows=market_rows(snapshot, bars),
    )


# ---------------------------------------------------------------------- health
def streams_health(
    supervisor: MonitoringSupervisor | None, gaps: GapRegistry, now_ms: int
) -> StreamsHealthResponse:
    snapshot = supervisor.snapshot() if supervisor is not None else None
    if supervisor is None:
        detector = Sourced[DemoDetectorInfo].unavailable("market data is not configured")
    else:
        det = supervisor.detector
        detector = Sourced[DemoDetectorInfo](
            value=DemoDetectorInfo(
                enabled=det.config.enabled,
                threshold=det.config.threshold,
                bars_seen=det.stats.bars_seen,
                emitted=det.stats.emitted,
                skipped_unhealthy=det.stats.skipped_unhealthy,
                skipped_not_live=det.stats.skipped_not_live,
                last_skip_reason=det.stats.last_skip_reason,
            ),
            provenance=Provenance.DEMO,
            as_of_ms=now_ms,
        )
    open_gaps = gaps.open_gaps()
    gap_count = Sourced[int](value=len(open_gaps), provenance=Provenance.SYSTEM, as_of_ms=now_ms)
    gap_minutes = Sourced[int](
        value=sum(g.missing_minutes for g in open_gaps),
        provenance=Provenance.SYSTEM,
        as_of_ms=now_ms,
    )
    if snapshot is None:
        return StreamsHealthResponse(
            generated_at=to_iso(now_ms),
            running=Sourced[bool](value=False, provenance=Provenance.SYSTEM, as_of_ms=now_ms),
            streams=Sourced[list[StreamStatus]].unavailable(NOT_STARTED),
            connections=Sourced[list[ConnectionStatus]].unavailable(NOT_STARTED),
            clock_offset_ms=Sourced[int].unavailable(NOT_STARTED),
            rest_weight=Sourced[RestWeights].unavailable(NOT_STARTED),
            rest_banned=Sourced[dict[str, bool]].unavailable(NOT_STARTED),
            open_gaps=gap_count,
            open_gap_minutes=gap_minutes,
            demo_detector=detector,
        )
    running = snapshot.running
    at = snapshot.as_of_ms
    usage = supervisor.rest_usage() if supervisor is not None else None
    return StreamsHealthResponse(
        generated_at=to_iso(now_ms),
        running=Sourced[bool](value=running, provenance=Provenance.SYSTEM, as_of_ms=now_ms),
        streams=_system(list(snapshot.streams), running, at, at),
        connections=_system(list(snapshot.connections), running, at, at),
        clock_offset_ms=snapshot.clock_offset_ms,
        rest_weight=(
            _system(usage, running, at, at)
            if usage is not None
            else Sourced[RestWeights].unavailable("REST weight limits not loaded (exchangeInfo)")
        ),
        rest_banned=_system(dict(snapshot.rest_banned), running, at, at),
        open_gaps=gap_count,
        open_gap_minutes=gap_minutes,
        demo_detector=detector,
    )


# ---------------------------------------------------------------------- gaps
def gap_registry(store: EventStore, now_ms: int) -> GapRegistry:
    """Projection of the persisted gap events (independent of whether monitoring runs)."""

    def no_emit(_: object) -> Event:
        raise RuntimeError("read-only gap projection")

    registry = GapRegistry(no_emit, lambda: now_ms)
    registry.load(store.read(types=[EventType.GAP_DETECTED, EventType.GAP_RESOLVED]))
    return registry


def gaps_response(registry: GapRegistry, now_ms: int, limit: int = 500) -> GapsResponse:
    records = sorted(registry.gaps(), key=lambda g: g.detected_at_ms, reverse=True)[:limit]
    out = [
        GapOut(
            gap_id=g.gap_id,
            market=g.market,
            symbol=g.symbol,
            interval=str(g.interval),
            start=to_iso(g.from_ms),
            end=to_iso(g.to_ms),
            from_ms=g.from_ms,
            to_ms=g.to_ms,
            minutes=g.total_minutes,
            missing_minutes=g.missing_minutes,
            missing_ranges_ms=list(g.missing.ranges()),
            cause=str(g.cause),
            detected_at=to_iso(g.detected_at_ms),
            resolution=str(g.resolution),
            resolved_at=None if g.resolved_at_ms is None else to_iso(g.resolved_at_ms),
        )
        for g in records
    ]
    return GapsResponse(
        generated_at=to_iso(now_ms),
        gaps=Sourced[list[GapOut]](value=out, provenance=Provenance.SYSTEM, as_of_ms=now_ms),
    )


# ---------------------------------------------------------------------- bars
def bars_response(
    bars: LiveBarStore,
    market: Market,
    symbol: str,
    from_ms: int,
    to_ms: int,
    now_ms: int,
) -> BarsResponse:
    stored = bars.range(market, symbol, from_ms, to_ms)
    truncated = len(stored) > MAX_BARS
    stored = stored[-MAX_BARS:]
    items = [
        BarOut(
            open_time_ms=s.bar.open_time_ms,
            close_time_ms=s.bar.close_time_ms,
            open=s.bar.open,
            high=s.bar.high,
            low=s.bar.low,
            close=s.bar.close,
            volume=s.bar.volume,
            source=str(s.bar.source),
            observed_session_id=s.observed_session_id,
            provenance=(
                Provenance.RECOVERED
                if s.bar.source is BarSource.REST_RECOVERED
                else Provenance.HISTORICAL
            ),
        )
        for s in stored
    ]
    as_of = items[-1].close_time_ms if items else now_ms
    return BarsResponse(
        generated_at=to_iso(now_ms),
        market=market,
        symbol=symbol,
        interval="1m",
        from_ms=from_ms,
        to_ms=to_ms,
        bars=Sourced[list[BarOut]](value=items, provenance=Provenance.HISTORICAL, as_of_ms=as_of),
        truncated=truncated,
    )


# ---------------------------------------------------------------------- demo signals
def _demo_signal(e: Event) -> DemoSignalOut | None:
    p = e.payload
    if p.get("strategy_id") != DEMO_STRATEGY_ID:
        return None
    try:
        return DemoSignalOut(
            signal_id=str(p["signal_id"]),
            seq=e.seq,
            created_at=to_iso(int(p["created_at_ms"])),
            decision_time=to_iso(int(p["decision_time_ms"])),
            market=Market(p["market"]),
            symbol=str(p["symbol"]),
            timeframe=str(p["timeframe"]),
            side=str(p["side"]),
            stage=str(p["stage"]),
            ref_price=float(p["ref_price"]),
            move=float(p.get("move", 0.0)),
            threshold=float(p.get("threshold", 0.0)),
            strategy_id=str(p["strategy_id"]),
            evidence_grade=str(p["evidence_grade"]),
            run_mode=str(p["run_mode"]),
            rationale=str(p.get("rationale", "")),
        )
    except (KeyError, TypeError, ValueError):
        log.warning("malformed demo signal event", extra={"seq": e.seq})
        return None


def signals_response(store: EventStore, now_ms: int, limit: int = 200) -> SignalsResponse:
    events = store.read(types=[EventType.SIGNAL_CREATED], limit=limit, descending=True)
    items = [s for s in (_demo_signal(e) for e in events) if s is not None]
    return SignalsResponse(
        generated_at=to_iso(now_ms),
        signals=Sourced[list[DemoSignalOut]](
            value=items, provenance=Provenance.DEMO, as_of_ms=now_ms
        ),
    )


# ---------------------------------------------------------------------- notifications
def notification_of(e: Event) -> NotificationOut | None:
    p = e.payload
    market = str(p["market"]) if "market" in p else None
    symbol = str(p["symbol"]) if "symbol" in p else None
    where = f"{market}:{symbol}" if market and symbol else ""
    severity: Severity = "info"
    provenance = Provenance.SYSTEM
    if e.type is EventType.STREAM_STATE_CHANGED:
        to = str(p.get("to"))
        severity = "info" if to in ("HEALTHY", "CONNECTING") else "warning"
        title = f"{p.get('kind', 'stream')} {where}: {p.get('from')} → {to}"
        detail = str(p.get("reason") or "")
    elif e.type is EventType.GAP_DETECTED:
        severity = "warning"
        title = f"Gap detected {where}: {p.get('minutes')} min ({p.get('cause')})"
        detail = f"{to_iso(int(p['from_ms']))} → {to_iso(int(p['to_ms']))}"
    elif e.type is EventType.GAP_RESOLVED:
        done = bool(p.get("fully_resolved"))
        title = f"Gap {'resolved' if done else 'partly recovered'} {where}: {p.get('minutes')} min"
        detail = f"source {p.get('source')}; {p.get('remaining_minutes')} min still missing"
    elif e.type is EventType.SESSION_CRASH_DETECTED:
        severity = "error"
        title = "Previous session did not shut down cleanly"
        detail = f"last alive {to_iso(int(p['last_alive_ms']))}"
    elif e.type is EventType.SIGNAL_CREATED:
        if p.get("strategy_id") != DEMO_STRATEGY_ID:
            return None
        provenance = Provenance.DEMO
        title = f"DEMO WATCH {where} {p.get('side')}"
        detail = str(p.get("rationale", ""))
    elif e.type is EventType.CLOCK_SKEW_CHANGED:
        severity = "warning" if p.get("exceeded") else "info"
        title = f"Clock offset {p.get('offset_ms')} ms"
        detail = f"threshold {p.get('threshold_ms')} ms"
    elif e.type is EventType.BAR_CONFLICT:
        severity = "warning"
        title = f"Bar conflict {where}"
        detail = f"kept {p.get('kept')} ({p.get('rule')})"
    else:
        return None
    return NotificationOut(
        id=f"e{e.seq}",
        seq=e.seq,
        ts=to_iso(e.ts_ms),
        kind=str(e.type),
        severity=severity,
        title=title,
        detail=detail,
        market=market,
        symbol=symbol,
        provenance=provenance,
    )


def notifications_after(
    store: EventStore, after_seq: int, limit: int = 200
) -> list[NotificationOut]:
    events = store.read(after_seq=after_seq, types=list(NOTIFICATION_TYPES), limit=limit)
    return [n for n in (notification_of(e) for e in events) if n is not None]


def notifications_response(
    store: EventStore, now_ms: int, limit: int = 200, extra: Iterable[NotificationOut] = ()
) -> NotificationsResponse:
    events = store.read(types=list(NOTIFICATION_TYPES), limit=limit, descending=True)
    items = [n for n in (notification_of(e) for e in events) if n is not None]
    items = sorted([*items, *extra], key=lambda n: n.ts, reverse=True)[:limit]
    head_seq, _ = store.head
    return NotificationsResponse(
        generated_at=to_iso(now_ms),
        notifications=Sourced[list[NotificationOut]](
            value=items, provenance=Provenance.SYSTEM, as_of_ms=now_ms
        ),
        head_seq=head_seq,
    )
