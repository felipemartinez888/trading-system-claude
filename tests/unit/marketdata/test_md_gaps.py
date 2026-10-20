from __future__ import annotations

from md_helpers import MIN, T0, Sink

from tsclaude.contracts.events import AggregateType, Event, EventType
from tsclaude.contracts.market import Market
from tsclaude.contracts.timeutil import ManualClock
from tsclaude.quality.gaps import (
    GapCause,
    GapRegistry,
    GapResolution,
    RangeSet,
    minutes_to_ranges,
)

S, U = Market.SPOT, Market.USDM


def _as_events(sink: Sink) -> list[Event]:
    return [
        Event(
            seq=i + 1,
            ts_ms=e.ts_ms,
            type=e.type,
            aggregate_type=e.aggregate_type,
            aggregate_id=e.aggregate_id,
            payload=e.payload,
            schema_version=1,
            prev_hash="0" * 64,
            hash=f"{i:064d}",
        )
        for i, e in enumerate(sink.events)
    ]


def test_rangeset_operations() -> None:
    r = RangeSet([(0, 10), (20, 30)])
    r.add(10, 20)
    assert r.ranges() == [(0, 30)]
    assert r.remove(5, 25) == [(5, 25)]
    assert r.ranges() == [(0, 5), (25, 30)]
    assert r.contains(4)
    assert not r.contains(5)
    assert r.subtract_from(0, 40) == [(5, 25), (30, 40)]
    assert r.total() == 10
    assert minutes_to_ranges([T0 + 2 * MIN, T0, T0 + MIN, T0 + 5 * MIN]) == [
        (T0, T0 + 3 * MIN),
        (T0 + 5 * MIN, T0 + 6 * MIN),
    ]


def test_open_partial_resolve_and_full_resolve(gaps: GapRegistry, sink: Sink) -> None:
    created = gaps.open(S, "BTCUSDT", [T0, T0 + MIN, T0 + 2 * MIN], GapCause.DISCONNECT)
    assert len(created) == 1
    g = created[0]
    assert (g.from_ms, g.to_ms, g.total_minutes) == (T0, T0 + 3 * MIN, 3)
    assert g.resolution is GapResolution.PENDING
    detected = sink.of(EventType.GAP_DETECTED)
    assert len(detected) == 1
    assert detected[0].aggregate_type is AggregateType.MARKET_DATA
    assert detected[0].aggregate_id == "spot:BTCUSDT"
    assert detected[0].payload["cause"] == "disconnect"

    # re-registering known minutes creates nothing
    assert gaps.open(S, "BTCUSDT", [T0 + MIN], GapCause.UNKNOWN) == []
    assert gaps.is_missing(S, "BTCUSDT", T0 + MIN)
    assert not gaps.is_missing(U, "BTCUSDT", T0 + MIN)

    assert gaps.resolve(S, "BTCUSDT", [T0 + MIN], "rest_recovered") == 1
    assert g.is_open
    assert g.missing.ranges() == [(T0, T0 + MIN), (T0 + 2 * MIN, T0 + 3 * MIN)]
    assert gaps.resolve(S, "BTCUSDT", [T0, T0 + 2 * MIN, T0 + 9 * MIN], "rest_recovered") == 2
    assert g.resolution is GapResolution.RECOVERED
    assert g.resolved_at_ms == T0
    resolved = sink.of(EventType.GAP_RESOLVED)
    assert [e.payload["fully_resolved"] for e in resolved] == [False, True]
    # the gap record is kept forever, only resolved
    assert len(gaps.gaps(S, "BTCUSDT")) == 1
    assert gaps.open_gaps() == []


def test_projection_rebuilds_from_events(gaps: GapRegistry, sink: Sink) -> None:
    gaps.open(S, "BTCUSDT", [T0, T0 + MIN], GapCause.DOWNTIME)
    gaps.open(U, "ETHUSDT", [T0 + 10 * MIN], GapCause.UNKNOWN)
    gaps.resolve(S, "BTCUSDT", [T0], "startup_backfill")

    clock = ManualClock(T0)
    rebuilt = GapRegistry(Sink(), clock.now_ms)
    rebuilt.load(_as_events(sink))
    assert rebuilt.missing_ranges(S, "BTCUSDT") == [(T0 + MIN, T0 + 2 * MIN)]
    assert rebuilt.missing_ranges(U, "ETHUSDT") == [(T0 + 10 * MIN, T0 + 11 * MIN)]
    assert len(rebuilt.gaps()) == 2


def test_long_downtime_range_is_cheap(gaps: GapRegistry) -> None:
    month = 30 * 24 * 60
    recs = gaps.open_ranges(S, "BTCUSDT", [(T0, T0 + month * MIN)], GapCause.DOWNTIME)
    assert recs[0].missing_minutes == month
    assert gaps.resolve_ranges(S, "BTCUSDT", [(T0, T0 + 60 * MIN)], "x") == 60
    assert recs[0].missing_minutes == month - 60
