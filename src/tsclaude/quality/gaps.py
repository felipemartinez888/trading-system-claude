"""Gap registry (DATA_ARCHITECTURE §4): known missing closed 1m bars, open or resolved.

A gap is a contiguous range of 1m open times `[from_ms, to_ms)` for one (market, symbol) that we
do not have as a stored closed bar. Gaps are never deleted: each one is opened by a `GAP_DETECTED`
event and narrowed or closed by `GAP_RESOLVED` events (one per recovery batch, carrying the exact
ranges recovered). The registry is a projection of those events, so it survives restarts via
`load()`.

Only `recovered` is an automatic resolution. A minute Binance never returns stays an open gap
(`pending`) and visible forever; automatic retries stop after a configured number of attempts, but
startup backfill tries again. Marking a gap `unrecoverable` is a later, explicit decision (e.g. the
M2 archive importer confirming the exchange has no data) and is not done in M1.

Missing minutes are kept as sorted disjoint ranges, so a multi-week downtime gap costs O(1) memory.
"""

from __future__ import annotations

import bisect
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from tsclaude.contracts.events import AggregateType, Event, EventType, NewEvent
from tsclaude.contracts.ids import new_ulid
from tsclaude.contracts.market import Interval, Market
from tsclaude.contracts.timeutil import MS_PER_MINUTE
from tsclaude.marketdata.types import EventSink, symbol_id

log = logging.getLogger(__name__)

STEP = MS_PER_MINUTE

type Ranges = list[tuple[int, int]]


class GapCause(StrEnum):
    DOWNTIME = "downtime"  # app was not observing (found by backfill)
    DISCONNECT = "disconnect"  # observing, the stream was down and REST did not return the bar
    EXCHANGE = "exchange"  # known exchange outage (set explicitly, never guessed)
    UNKNOWN = "unknown"  # observing, stream up, bar never closed and REST did not return it


class GapResolution(StrEnum):
    PENDING = "pending"
    RECOVERED = "recovered"
    UNRECOVERABLE = "unrecoverable"  # reserved for an explicit later decision; not set in M1


def minutes_to_ranges(minutes: Iterable[int]) -> Ranges:
    """1m open times -> sorted contiguous [from_ms, to_ms) ranges."""
    ranges: Ranges = []
    for m in sorted(set(minutes)):
        if m % STEP != 0:
            raise ValueError("minutes must be aligned 1m open times")
        if ranges and ranges[-1][1] == m:
            ranges[-1] = (ranges[-1][0], m + STEP)
        else:
            ranges.append((m, m + STEP))
    return ranges


def range_minutes(from_ms: int, to_ms: int) -> range:
    return range(from_ms, to_ms, STEP)


class RangeSet:
    """Sorted, disjoint, merged half-open integer ranges (1m-aligned in practice)."""

    def __init__(self, ranges: Iterable[tuple[int, int]] = ()) -> None:
        self._r: Ranges = []
        for lo, hi in ranges:
            self.add(lo, hi)

    def __bool__(self) -> bool:
        return bool(self._r)

    def ranges(self) -> Ranges:
        return list(self._r)

    def total(self) -> int:
        return sum(hi - lo for lo, hi in self._r)

    def contains(self, x: int) -> bool:
        i = bisect.bisect_right(self._r, (x, float("inf"))) - 1
        return i >= 0 and self._r[i][0] <= x < self._r[i][1]

    def add(self, lo: int, hi: int) -> None:
        if hi <= lo:
            return
        out: Ranges = []
        placed = False
        for a, b in self._r:
            if b < lo:
                out.append((a, b))
            elif a > hi:
                if not placed:
                    out.append((lo, hi))
                    placed = True
                out.append((a, b))
            else:
                lo, hi = min(lo, a), max(hi, b)
        if not placed:
            out.append((lo, hi))
        out.sort()
        self._r = out

    def intersect(self, lo: int, hi: int) -> Ranges:
        return [(max(a, lo), min(b, hi)) for a, b in self._r if a < hi and b > lo]

    def remove(self, lo: int, hi: int) -> Ranges:
        """Remove [lo, hi); return the sub-ranges that were actually present."""
        removed = self.intersect(lo, hi)
        if not removed:
            return []
        out: Ranges = []
        for a, b in self._r:
            if b <= lo or a >= hi:
                out.append((a, b))
                continue
            if a < lo:
                out.append((a, lo))
            if b > hi:
                out.append((hi, b))
        self._r = out
        return removed

    def subtract_from(self, lo: int, hi: int) -> Ranges:
        """Parts of [lo, hi) NOT in this set."""
        out: Ranges = []
        cur = lo
        for a, b in self.intersect(lo, hi):
            if a > cur:
                out.append((cur, a))
            cur = max(cur, b)
        if cur < hi:
            out.append((cur, hi))
        return out


@dataclass
class GapRecord:
    gap_id: str
    market: Market
    symbol: str
    interval: Interval
    from_ms: int  # inclusive open time of the first missing minute
    to_ms: int  # exclusive
    cause: GapCause
    detected_at_ms: int
    missing: RangeSet = field(default_factory=RangeSet)
    resolved_at_ms: int | None = None
    attempts: int = 0  # automatic recovery attempts in this process (not persisted)

    @property
    def resolution(self) -> GapResolution:
        return GapResolution.PENDING if self.missing else GapResolution.RECOVERED

    @property
    def is_open(self) -> bool:
        return bool(self.missing)

    @property
    def total_minutes(self) -> int:
        return (self.to_ms - self.from_ms) // STEP

    @property
    def missing_minutes(self) -> int:
        return self.missing.total() // STEP


class GapRegistry:
    def __init__(
        self,
        emit: EventSink,
        now_ms: Callable[[], int],
        new_id: Callable[[int], str] = new_ulid,
    ) -> None:
        self._emit = emit
        self._now = now_ms
        self._new_id = new_id
        self._gaps: dict[str, GapRecord] = {}

    # ------------------------------------------------------------------ projection
    def load(self, events: Iterable[Event]) -> None:
        """Rebuild state from persisted GAP_DETECTED / GAP_RESOLVED events (no emission)."""
        for e in events:
            p = e.payload
            if e.type is EventType.GAP_DETECTED:
                lo, hi = int(p["from_ms"]), int(p["to_ms"])
                self._gaps[str(p["gap_id"])] = GapRecord(
                    gap_id=str(p["gap_id"]),
                    market=Market(p["market"]),
                    symbol=str(p["symbol"]),
                    interval=Interval(p.get("interval", "1m")),
                    from_ms=lo,
                    to_ms=hi,
                    cause=GapCause(p["cause"]),
                    detected_at_ms=int(p["detected_at_ms"]),
                    missing=RangeSet([(lo, hi)]),
                )
            elif e.type is EventType.GAP_RESOLVED:
                gap = self._gaps.get(str(p["gap_id"]))
                if gap is None:
                    log.warning("GAP_RESOLVED for unknown gap", extra={"gap_id": p["gap_id"]})
                    continue
                for lo, hi in p["ranges"]:
                    gap.missing.remove(int(lo), int(hi))
                if not gap.missing:
                    gap.resolved_at_ms = int(p.get("resolved_at_ms", e.ts_ms))

    # ------------------------------------------------------------------ queries
    def gaps(self, market: Market | None = None, symbol: str | None = None) -> list[GapRecord]:
        out = [
            g
            for g in self._gaps.values()
            if (market is None or g.market is market) and (symbol is None or g.symbol == symbol)
        ]
        return sorted(out, key=lambda g: (str(g.market), g.symbol, g.from_ms))

    def open_gaps(self, market: Market | None = None, symbol: str | None = None) -> list[GapRecord]:
        return [g for g in self.gaps(market, symbol) if g.is_open]

    def is_missing(self, market: Market, symbol: str, open_time_ms: int) -> bool:
        return any(g.missing.contains(open_time_ms) for g in self.open_gaps(market, symbol))

    def missing_ranges(self, market: Market, symbol: str) -> Ranges:
        union = RangeSet()
        for g in self.open_gaps(market, symbol):
            for lo, hi in g.missing.ranges():
                union.add(lo, hi)
        return union.ranges()

    def has_open(self, market: Market, symbol: str) -> bool:
        return bool(self.open_gaps(market, symbol))

    # ------------------------------------------------------------------ commands
    def open(
        self,
        market: Market,
        symbol: str,
        minutes: Iterable[int],
        cause: GapCause,
        detail: str | None = None,
    ) -> list[GapRecord]:
        return self.open_ranges(market, symbol, minutes_to_ranges(minutes), cause, detail)

    def open_ranges(
        self,
        market: Market,
        symbol: str,
        ranges: Iterable[tuple[int, int]],
        cause: GapCause,
        detail: str | None = None,
    ) -> list[GapRecord]:
        """Register missing ranges. Minutes already in an open gap are not registered twice."""
        known = RangeSet(self.missing_ranges(market, symbol))
        fresh = RangeSet()
        for lo, hi in ranges:
            if lo % STEP or hi % STEP:
                raise ValueError("gap ranges must be 1m-aligned")
            for a, b in known.subtract_from(lo, hi):
                fresh.add(a, b)
        created: list[GapRecord] = []
        now = self._now()
        for lo, hi in fresh.ranges():
            rec = GapRecord(
                gap_id=self._new_id(now),
                market=market,
                symbol=symbol,
                interval=Interval.M1,
                from_ms=lo,
                to_ms=hi,
                cause=cause,
                detected_at_ms=now,
                missing=RangeSet([(lo, hi)]),
            )
            self._gaps[rec.gap_id] = rec
            created.append(rec)
            log.warning(
                "gap detected",
                extra={
                    "market": str(market),
                    "symbol": symbol,
                    "from_ms": lo,
                    "to_ms": hi,
                    "cause": str(cause),
                },
            )
            self._emit(
                NewEvent(
                    type=EventType.GAP_DETECTED,
                    aggregate_type=AggregateType.MARKET_DATA,
                    aggregate_id=symbol_id(market, symbol),
                    ts_ms=now,
                    payload={
                        "gap_id": rec.gap_id,
                        "market": str(market),
                        "symbol": symbol,
                        "interval": str(Interval.M1),
                        "from_ms": lo,
                        "to_ms": hi,
                        "minutes": rec.total_minutes,
                        "cause": str(cause),
                        "detected_at_ms": now,
                        "resolution": str(GapResolution.PENDING),
                        "detail": detail,
                    },
                )
            )
        return created

    def resolve(self, market: Market, symbol: str, minutes: Iterable[int], source: str) -> int:
        """Mark minutes as recovered (closed bars now stored). Returns open minutes closed."""
        return self.resolve_ranges(market, symbol, minutes_to_ranges(minutes), source)

    def resolve_ranges(
        self, market: Market, symbol: str, ranges: Iterable[tuple[int, int]], source: str
    ) -> int:
        """Range form of `resolve`; one GAP_RESOLVED event per affected gap."""
        ranges = list(ranges)
        now = self._now()
        closed = 0
        for gap in self.open_gaps(market, symbol):
            hit = RangeSet()
            for lo, hi in ranges:
                for a, b in gap.missing.remove(lo, hi):
                    hit.add(a, b)
            if not hit:
                continue
            n = hit.total() // STEP
            closed += n
            if not gap.missing:
                gap.resolved_at_ms = now
            self._emit(
                NewEvent(
                    type=EventType.GAP_RESOLVED,
                    aggregate_type=AggregateType.MARKET_DATA,
                    aggregate_id=symbol_id(market, symbol),
                    ts_ms=now,
                    payload={
                        "gap_id": gap.gap_id,
                        "market": str(market),
                        "symbol": symbol,
                        "ranges": [[lo, hi] for lo, hi in hit.ranges()],
                        "minutes": n,
                        "resolution": str(GapResolution.RECOVERED),
                        "source": source,
                        "remaining_minutes": gap.missing_minutes,
                        "fully_resolved": not gap.missing,
                        "resolved_at_ms": now,
                    },
                )
            )
        return closed
