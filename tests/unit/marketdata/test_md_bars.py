"""Bar builder with a ManualClock: closed vs partial, watermark, REST recovery, gaps, conflicts."""

from __future__ import annotations

import asyncio

from md_helpers import MIN, T0, FakeFetcher, Sink, kline, rest_kline

from tsclaude.contracts.events import EventType
from tsclaude.contracts.market import BarSource, Market
from tsclaude.contracts.timeutil import ManualClock
from tsclaude.marketdata.bars import BarBuilder, BarClosed, SymbolBarBuilder
from tsclaude.quality.gaps import GapCause, GapRegistry
from tsclaude.store.livebars import LiveBarStore

M, SYM = Market.SPOT, "BTCUSDT"


def _builder(
    bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock, **kw: object
) -> tuple[SymbolBarBuilder, list[BarClosed]]:
    published: list[BarClosed] = []
    b = SymbolBarBuilder(
        market=M,
        symbol=SYM,
        watch_from_ms=T0,
        store=bars,
        gaps=gaps,
        emit=sink,
        now_ms=clock.now_ms,
        server_now_ms=clock.now_ms,
        session_id="SESSION1",
        grace_ms=5_000,
        on_bar=published.append,
        **kw,  # type: ignore[arg-type]
    )
    return b, published


def test_closed_kline_becomes_ws_live_bar(
    bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock
) -> None:
    b, published = _builder(bars, gaps, sink, clock)
    clock.set(T0 + MIN + 50)
    b.on_kline(kline(T0))
    stored = bars.get(M, SYM, T0)
    assert stored is not None
    assert stored.bar.source is BarSource.WS_LIVE
    assert stored.observed_session_id == "SESSION1"
    assert stored.bar.ingested_at_ms == T0 + MIN + 50
    assert [p.bar.open_time_ms for p in published] == [T0]
    assert b.cursor_ms == T0 + MIN
    assert sink.events == []


def test_partial_never_persisted_or_published(
    bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock
) -> None:
    b, published = _builder(bars, gaps, sink, clock)
    b.on_kline(kline(T0, closed=False, event_ms=T0 + 1_000, close=100.1))
    b.on_kline(kline(T0, closed=False, event_ms=T0 + 2_000, close=100.2))
    b.on_kline(kline(T0, closed=False, event_ms=T0 + 1_500, close=100.15))  # out of order
    p = b.partial()
    assert p is not None
    assert p.close == 100.2
    assert bars.count() == 0
    assert published == []
    # closing the minute clears the partial; late forming updates for it are ignored
    b.on_kline(kline(T0))
    assert b.partial() is None
    b.on_kline(kline(T0, closed=False, event_ms=T0 + 59_000))
    assert b.partial() is None
    b.on_kline(kline(T0 + MIN, closed=False, event_ms=T0 + MIN + 10))
    nxt = b.partial()
    assert nxt is not None
    assert nxt.open_time_ms == T0 + MIN
    assert bars.count() == 1
    assert b.stats.ws_partial == 5


def test_identical_duplicate_ignored(
    bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock
) -> None:
    b, published = _builder(bars, gaps, sink, clock)
    b.on_kline(kline(T0))
    b.on_kline(kline(T0))  # e.g. old and new connection during a recycle
    assert bars.count() == 1
    assert len(published) == 1
    assert b.stats.duplicates == 1
    assert sink.events == []


def test_differing_duplicate_records_conflict(
    bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock
) -> None:
    b, _ = _builder(bars, gaps, sink, clock)
    b.on_kline(kline(T0, close=100.5))
    b.on_kline(kline(T0, close=100.6))
    conflicts = sink.of(EventType.BAR_CONFLICT)
    assert len(conflicts) == 1
    p = conflicts[0].payload
    assert p["stored"]["close"] == 100.5
    assert p["incoming"]["close"] == 100.6
    assert p["kept"] == "stored"
    stored = bars.get(M, SYM, T0)
    assert stored is not None
    assert stored.bar.close == 100.5
    assert b.stats.conflicts == 1


def test_ws_close_after_rest_recovery_replaces_and_records_conflict(
    bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock
) -> None:
    b, published = _builder(bars, gaps, sink, clock)
    fetch = FakeFetcher()
    fetch.put(M, SYM, rest_kline(T0, close=100.4))
    clock.set(T0 + MIN + 5_001)
    asyncio.run(b.check_watermark(fetch))
    b.on_kline(kline(T0, close=100.5))  # very late WS close that differs
    stored = bars.get(M, SYM, T0)
    assert stored is not None
    assert stored.bar.source is BarSource.WS_LIVE
    assert stored.bar.close == 100.5
    ev = sink.of(EventType.BAR_CONFLICT)
    assert len(ev) == 1
    assert ev[0].payload["kept"] == "incoming"
    assert [p.replaced for p in published] == [False, True]


def test_watermark_waits_for_grace(
    bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock
) -> None:
    b, _ = _builder(bars, gaps, sink, clock)
    fetch = FakeFetcher()
    clock.set(T0 + MIN + 5_000)  # exactly at the deadline: not yet due
    assert asyncio.run(b.check_watermark(fetch)) == []
    assert fetch.calls == []
    clock.advance(1)
    assert asyncio.run(b.check_watermark(fetch)) == [T0]


def test_late_close_recovered_via_rest(
    bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock
) -> None:
    b, published = _builder(bars, gaps, sink, clock)
    fetch = FakeFetcher()
    fetch.put(M, SYM, rest_kline(T0))
    fetch.put(M, SYM, rest_kline(T0 + MIN))  # still forming at fetch time: must be ignored
    clock.set(T0 + MIN + 6_000)
    due = asyncio.run(b.check_watermark(fetch))
    assert due == [T0]
    assert fetch.calls == [(M, SYM, T0, T0 + MIN)]
    stored = bars.get(M, SYM, T0)
    assert stored is not None
    assert stored.bar.source is BarSource.REST_RECOVERED
    assert stored.observed_session_id is None  # not seen live
    assert bars.get(M, SYM, T0 + MIN) is None
    late = sink.of(EventType.BAR_LATE)
    assert len(late) == 1
    assert late[0].payload["open_time_ms"] == T0
    assert late[0].payload["deadline_ms"] == T0 + MIN + 5_000
    assert [p.bar.source for p in published] == [BarSource.REST_RECOVERED]
    assert sink.of(EventType.GAP_DETECTED) == []
    # the WS close arriving afterwards (identical) is a silent duplicate
    b.on_kline(kline(T0))
    assert b.stats.duplicates == 1
    assert b.caught_up()


def test_missing_in_rest_becomes_gap_then_retry_resolves(
    bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock
) -> None:
    b, _ = _builder(bars, gaps, sink, clock, cause_for=lambda _m, _s: GapCause.DISCONNECT)
    fetch = FakeFetcher()
    fetch.put(M, SYM, rest_kline(T0 + MIN))
    clock.set(T0 + 3 * MIN + 5_001)  # minutes T0, T0+1, T0+2 overdue
    due = asyncio.run(b.check_watermark(fetch))
    assert due == [T0, T0 + MIN, T0 + 2 * MIN]
    det = sink.of(EventType.GAP_DETECTED)
    assert [(e.payload["from_ms"], e.payload["to_ms"]) for e in det] == [
        (T0, T0 + MIN),
        (T0 + 2 * MIN, T0 + 3 * MIN),
    ]
    assert all(e.payload["cause"] == "disconnect" for e in det)
    assert b.cursor_ms == T0 + 3 * MIN  # gaps are settled (registered), not re-detected
    assert asyncio.run(b.check_watermark(fetch)) == []
    assert not b.caught_up()  # gaps still have retries left

    fetch.put(M, SYM, rest_kline(T0))
    assert asyncio.run(b.retry_gaps(fetch)) == 0  # not in the same instant it was detected
    clock.advance(1_000)
    assert asyncio.run(b.retry_gaps(fetch)) == 1
    resolved = sink.of(EventType.GAP_RESOLVED)
    assert len(resolved) == 1
    assert resolved[0].payload["ranges"] == [[T0, T0 + MIN]]
    stored = bars.get(M, SYM, T0)
    assert stored is not None
    assert stored.bar.source is BarSource.REST_RECOVERED
    assert gaps.missing_ranges(M, SYM) == [(T0 + 2 * MIN, T0 + 3 * MIN)]


def test_gap_retries_are_bounded_and_gap_stays_visible(
    bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock
) -> None:
    b, _ = _builder(bars, gaps, sink, clock, max_gap_attempts=3)
    fetch = FakeFetcher()
    clock.set(T0 + MIN + 5_001)
    asyncio.run(b.check_watermark(fetch))  # attempt 1
    for _ in range(5):
        clock.advance(1_000)
        asyncio.run(b.retry_gaps(fetch))
    assert len(fetch.calls) == 3  # 1 watermark + 2 retries
    assert b.caught_up()  # exhausted gap no longer blocks recovery...
    assert gaps.is_missing(M, SYM, T0)  # ...but remains registered and open


def test_rest_error_becomes_gap_not_crash(
    bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock
) -> None:
    b, _ = _builder(bars, gaps, sink, clock)
    fetch = FakeFetcher()
    fetch.fail = ConnectionError("offline")
    clock.set(T0 + MIN + 5_001)
    asyncio.run(b.check_watermark(fetch))
    det = sink.of(EventType.GAP_DETECTED)
    assert len(det) == 1
    assert "offline" in det[0].payload["detail"]
    assert b.stats.rest_errors == 1


def test_ws_close_resolves_open_gap(
    bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock
) -> None:
    b, _ = _builder(bars, gaps, sink, clock)
    clock.set(T0 + MIN + 5_001)
    asyncio.run(b.check_watermark(FakeFetcher()))
    b.on_kline(kline(T0))  # arrives after the gap was registered
    res = sink.of(EventType.GAP_RESOLVED)
    assert len(res) == 1
    assert res[0].payload["source"] == "ws_live"
    assert not gaps.has_open(M, SYM)


def test_ws_close_while_rest_in_flight_is_not_overwritten(
    bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock
) -> None:
    b, _ = _builder(bars, gaps, sink, clock)

    async def slow_fetch(market: Market, symbol: str, start: int, end: int) -> list:  # type: ignore[type-arg]
        b.on_kline(kline(T0))  # WS close lands during the REST call
        return [rest_kline(T0)]

    clock.set(T0 + MIN + 5_001)
    asyncio.run(b.check_watermark(slow_fetch))
    stored = bars.get(M, SYM, T0)
    assert stored is not None
    assert stored.bar.source is BarSource.WS_LIVE
    assert sink.of(EventType.BAR_LATE) == []
    assert sink.of(EventType.GAP_DETECTED) == []


def test_invalid_closed_kline_rejected_and_counted(
    bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock
) -> None:
    b, _ = _builder(bars, gaps, sink, clock)
    bad = kline(T0)
    bad = type(bad)(**{**{f: getattr(bad, f) for f in bad.__slots__}, "high": 50.0})
    b.on_kline(bad)
    assert b.stats.invalid == 1
    assert bars.count() == 0


def test_router(bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock) -> None:
    b, _ = _builder(bars, gaps, sink, clock)
    router = BarBuilder()
    router.add(b)
    assert router.on_kline(kline(T0))
    assert not router.on_kline(kline(T0, symbol="ETHUSDT"))
    assert router.unrouted == 1
    assert router.get(M, SYM) is b
