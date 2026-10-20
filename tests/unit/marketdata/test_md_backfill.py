"""Startup backfill: tagging inside/outside observation intervals, gaps, idempotent re-run."""

from __future__ import annotations

import asyncio

from md_helpers import MIN, T0, FakeFetcher, Sink, kline, rest_kline

from tsclaude.contracts.events import EventType
from tsclaude.contracts.market import BarSource, Market
from tsclaude.contracts.timeutil import ManualClock
from tsclaude.marketdata.backfill import (
    Backfiller,
    BackfillSettings,
    ObservationIndex,
    current_minute_open,
)
from tsclaude.marketdata.convert import bar_from_ws
from tsclaude.quality.gaps import GapRegistry
from tsclaude.runtime.sessions import ObservationInterval
from tsclaude.store.livebars import LiveBarStore

M, SYM = Market.USDM, "ETHUSDT"
END = T0 + 60 * MIN  # 60-minute window [T0, END)
SETTINGS = BackfillSettings(lookback_ms=60 * MIN, max_backfill_ms=120 * MIN)
# observed from minute 20 (mid-minute start) to minute 40 (ends mid-minute 39)
OBS = [ObservationInterval(T0 + 20 * MIN + 30_000, T0 + 39 * MIN + 10_000, "MON1", "user")]


def _fetcher(skip: set[int] | None = None) -> FakeFetcher:
    f = FakeFetcher()
    for i in range(70):
        if skip and i in skip:
            continue
        f.put(M, SYM, rest_kline(T0 + i * MIN))
    return f


def _source(bars: LiveBarStore, i: int) -> BarSource | None:
    s = bars.get(M, SYM, T0 + i * MIN)
    return None if s is None else s.bar.source


def test_observation_index() -> None:
    idx = ObservationIndex(OBS, now_ms=END)
    assert not idx.observed(T0 + 19 * MIN)
    assert idx.observed(T0 + 20 * MIN)  # interval starts inside this minute
    assert idx.observed(T0 + 39 * MIN)  # interval ends inside this minute
    assert not idx.observed(T0 + 40 * MIN)
    open_ended = ObservationIndex([ObservationInterval(T0, None, "M", None)], now_ms=T0 + 5 * MIN)
    assert open_ended.observed(T0 + 5 * MIN)
    assert not open_ended.observed(T0 + 6 * MIN)
    assert current_minute_open(T0 + 59_999) == T0


def test_initial_backfill_tags_by_observation(
    bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock
) -> None:
    fetch = _fetcher()
    bf = Backfiller(bars, gaps, fetch, sink, clock.now_ms, SETTINGS)
    res = asyncio.run(bf.run(M, SYM, END, OBS))
    assert bars.count() == 60
    assert _source(bars, 19) is BarSource.REST_BACKFILL
    assert _source(bars, 20) is BarSource.REST_RECOVERED
    assert _source(bars, 39) is BarSource.REST_RECOVERED
    assert _source(bars, 40) is BarSource.REST_BACKFILL
    assert bars.get(M, SYM, END) is None  # the forming minute is never backfilled
    assert (res.inserted_recovered, res.inserted_backfill) == (20, 40)
    assert sink.of(EventType.GAP_DETECTED) == []  # initial load is not a gap
    done = sink.of(EventType.BACKFILL_COMPLETED)
    assert len(done) == 1
    assert done[0].payload["requested_minutes"] == 60


def test_rerun_is_idempotent(
    bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock
) -> None:
    bf = Backfiller(bars, gaps, _fetcher(), sink, clock.now_ms, SETTINGS)
    asyncio.run(bf.run(M, SYM, END, OBS))
    before = bars.count()
    fetch2 = _fetcher()
    res = asyncio.run(
        Backfiller(bars, gaps, fetch2, sink, clock.now_ms, SETTINGS).run(M, SYM, END, OBS)
    )
    assert bars.count() == before
    assert res.requested_minutes == 0
    assert res.inserted == 0
    assert fetch2.calls == []
    assert sink.of(EventType.GAP_DETECTED) == []


def test_hole_after_crash_is_gap_then_resolved(
    bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock
) -> None:
    # live bars were stored up to minute 9, then a crash; restart at END
    for i in range(10):
        bars.write(bar_from_ws(kline(T0 + i * MIN, market=M, symbol=SYM), T0), "OLD")
    crash_obs = [ObservationInterval(T0, T0 + 10 * MIN, "MON0", "crash")]
    fetch = _fetcher(skip={30})  # REST lacks minute 30
    res = asyncio.run(
        Backfiller(bars, gaps, fetch, sink, clock.now_ms, SETTINGS).run(M, SYM, END, crash_obs)
    )
    assert _source(bars, 9) is BarSource.WS_LIVE  # untouched
    assert _source(bars, 10) is BarSource.REST_RECOVERED  # minute 10 overlaps the interval end
    assert _source(bars, 11) is BarSource.REST_BACKFILL  # app was down
    detected = sink.of(EventType.GAP_DETECTED)
    assert [(e.payload["from_ms"], e.payload["to_ms"], e.payload["cause"]) for e in detected] == [
        (T0 + 10 * MIN, T0 + 11 * MIN, "unknown"),
        (T0 + 11 * MIN, END, "downtime"),
    ]
    assert res.gaps_resolved_minutes == 49
    # the unobserved interval minus what REST returned stays an open gap
    assert gaps.missing_ranges(M, SYM) == [(T0 + 30 * MIN, T0 + 31 * MIN)]
    assert res.remaining_open_minutes == 1

    # a later run with REST now returning minute 30 resolves it
    res2 = asyncio.run(
        Backfiller(bars, gaps, _fetcher(), sink, clock.now_ms, SETTINGS).run(
            M, SYM, END + MIN, crash_obs
        )
    )
    # minute 60 closed since the first run: it is part of the downtime too (opened and resolved)
    assert res2.gaps_opened_minutes == 1
    assert res2.gaps_resolved_minutes == 2
    assert not gaps.has_open(M, SYM)
    assert _source(bars, 30) is BarSource.REST_BACKFILL


def test_initial_minutes_missing_in_rest_are_gaps(
    bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock
) -> None:
    fetch = _fetcher(skip={0, 1, 2, 25})
    asyncio.run(Backfiller(bars, gaps, fetch, sink, clock.now_ms, SETTINGS).run(M, SYM, END, OBS))
    assert gaps.missing_ranges(M, SYM) == [(T0, T0 + 3 * MIN), (T0 + 25 * MIN, T0 + 26 * MIN)]
    causes = {e.payload["from_ms"]: e.payload["cause"] for e in sink.of(EventType.GAP_DETECTED)}
    assert causes == {T0: "downtime", T0 + 25 * MIN: "unknown"}


def test_long_downtime_beyond_max_is_registered_not_fetched(
    bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock
) -> None:
    old = T0 - 1000 * MIN
    bars.write(bar_from_ws(kline(old, market=M, symbol=SYM), T0), "OLD")
    fetch = _fetcher()
    res = asyncio.run(
        Backfiller(bars, gaps, fetch, sink, clock.now_ms, SETTINGS).run(M, SYM, END, [])
    )
    floor = END - 120 * MIN
    assert all(start >= floor for _, _, start, _ in fetch.calls)
    assert res.from_ms == floor
    beyond = [e for e in sink.of(EventType.GAP_DETECTED) if e.payload["to_ms"] == floor]
    assert len(beyond) == 1
    assert beyond[0].payload["from_ms"] == old + MIN
    assert "archive" in beyond[0].payload["detail"]
    # the fake REST source has no rows before T0, so [floor, T0) stays missing as well
    assert gaps.missing_ranges(M, SYM) == [(old + MIN, T0)]


def test_fetch_error_leaves_registered_gap(
    bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock
) -> None:
    bars.write(bar_from_ws(kline(T0, market=M, symbol=SYM), T0), "OLD")
    fetch = FakeFetcher()
    fetch.fail = ConnectionError("offline")
    res = asyncio.run(
        Backfiller(bars, gaps, fetch, sink, clock.now_ms, SETTINGS).run(M, SYM, END, [])
    )
    assert res.errors
    assert gaps.missing_ranges(M, SYM) == [(T0 + MIN, END)]
    assert sink.of(EventType.BACKFILL_COMPLETED)[0].payload["remaining_open_minutes"] == 59
