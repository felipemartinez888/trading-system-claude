"""Startup backfill of closed 1m bars over REST (no network library; the fetcher is injected).

For each (market, symbol), up to `end_ms` (the open time of the current, still-forming minute, by
server time):

- Window: the last `lookback` (default 3 days) **plus** everything since the last stored bar if
  that is older (capped at `max_backfill`; older missing history is registered as a `downtime`
  gap for the M2 archive importer). The whole window is compared against stored bars, not just
  "after the last bar", so a minute lost by a crash between two stored bars is found too.
- Holes after the first stored bar are *gaps in our record*: they are registered
  (`GAP_DETECTED`) before fetching — crash-safe — and closed by `GAP_RESOLVED` for what REST
  returns. Minutes before any stored history are an initial load, not a gap; only those REST does
  not return are registered.
- Open gaps already in the registry inside the window are retried as part of the same fetch.
- Tagging: a minute that overlaps an observation interval (the app was monitoring) is
  `rest_recovered`; otherwise `rest_backfill`. Gap cause follows the same split: `unknown` inside
  observation, `downtime` outside.
- Idempotent: a second run finds no missing minutes and writes nothing; identical rows are no-ops.
- One `BACKFILL_COMPLETED` event per (market, symbol).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from tsclaude.contracts.events import AggregateType, EventType, NewEvent
from tsclaude.contracts.market import BarSource, Market
from tsclaude.contracts.timeutil import MS_PER_DAY, MS_PER_MINUTE
from tsclaude.marketdata.bars import KlineFetcher
from tsclaude.marketdata.convert import bar_from_rest, bar_payload
from tsclaude.marketdata.types import EventSink, symbol_id
from tsclaude.quality.gaps import GapCause, GapRegistry, RangeSet, minutes_to_ranges
from tsclaude.runtime.sessions import ObservationInterval
from tsclaude.store.livebars import LiveBarStore, WriteOutcome

log = logging.getLogger(__name__)

STEP = MS_PER_MINUTE


def current_minute_open(server_now_ms: int) -> int:
    """Open time of the (still forming) minute containing `server_now_ms`."""
    return server_now_ms - server_now_ms % STEP


def live_watch_from(server_now_ms: int, grace_ms: int) -> int:
    """First minute owned by the live bar builder rather than startup backfill.

    Backfill takes only minutes whose watermark deadline (`open + 60 s + grace`) has passed;
    a minute that closed less than `grace` ago stays with the builder, which waits for its WS
    close or recovers it at the deadline.
    """
    return current_minute_open(server_now_ms - grace_ms - 1)


@dataclass(frozen=True, slots=True)
class BackfillSettings:
    lookback_ms: int = 3 * MS_PER_DAY
    max_backfill_ms: int = 30 * MS_PER_DAY


@dataclass
class BackfillResult:
    market: Market
    symbol: str
    from_ms: int
    to_ms: int
    requested_minutes: int = 0
    inserted_backfill: int = 0
    inserted_recovered: int = 0
    duplicates: int = 0
    conflicts: int = 0
    invalid: int = 0
    gaps_opened_minutes: int = 0
    gaps_resolved_minutes: int = 0
    remaining_open_minutes: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def inserted(self) -> int:
        return self.inserted_backfill + self.inserted_recovered


class ObservationIndex:
    """Which 1m minutes lie entirely inside an observation interval (the app was monitoring)?

    Lead decision (M1-T1 review N1): minute `m` is observed only if
    `start_ms <= m` and `close_time = m + 60 s - 1 < end_ms`. A minute only partly covered (the
    monitoring started or stopped inside it) is not observed. An open interval ends at `now_ms`.
    The set is kept as 1m-aligned ranges of open times, so long windows stay cheap.
    """

    def __init__(self, intervals: Sequence[ObservationInterval], now_ms: int) -> None:
        self._set = RangeSet()
        for iv in intervals:
            end = iv.end_ms if iv.end_ms is not None else now_ms
            lo = iv.start_ms + (-iv.start_ms) % STEP  # first open time >= start
            hi = end - end % STEP  # open times m with m + STEP <= end
            self._set.add(lo, hi)

    def observed(self, open_time_ms: int) -> bool:
        return self._set.contains(open_time_ms)

    def split(self, lo: int, hi: int) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
        """[lo, hi) -> (observed ranges, unobserved ranges)."""
        return self._set.intersect(lo, hi), self._set.subtract_from(lo, hi)


class Backfiller:
    def __init__(
        self,
        store: LiveBarStore,
        gaps: GapRegistry,
        fetch: KlineFetcher,
        emit: EventSink,
        now_ms: Callable[[], int],
        settings: BackfillSettings | None = None,
    ) -> None:
        self._store = store
        self._gaps = gaps
        self._fetch = fetch
        self._emit = emit
        self._now = now_ms
        self._settings = settings or BackfillSettings()

    async def run(
        self,
        market: Market,
        symbol: str,
        end_ms: int,
        intervals: Sequence[ObservationInterval],
    ) -> BackfillResult:
        if end_ms % STEP:
            raise ValueError("end_ms must be a 1m open time (the current forming minute)")
        s = self._settings
        obs = ObservationIndex(intervals, self._now())
        start = end_ms - s.lookback_ms
        floor = end_ms - s.max_backfill_ms
        last = self._store.last(market, symbol)
        if last is not None and last.bar.open_time_ms + STEP < start:
            start = max(last.bar.open_time_ms + STEP, floor)
        start -= start % STEP
        result = BackfillResult(market, symbol, start, end_ms)

        # History older than max_backfill since the last stored bar: register, do not fetch.
        if last is not None and last.bar.open_time_ms + STEP < floor:
            self._open(
                market,
                symbol,
                last.bar.open_time_ms + STEP,
                floor,
                obs,
                result,
                detail="older than max_backfill_days; left for the archive importer",
            )

        stored = self._store.open_times(market, symbol, start, end_ms)
        first = self._store.first_open_time(market, symbol)
        wanted = RangeSet()
        for m in range(start, end_ms, STEP):
            if m not in stored:
                wanted.add(m, m + STEP)
        # Open registry gaps inside the window are fetched too (they are unstored by definition).
        for lo, hi in self._gaps.missing_ranges(market, symbol):
            for a, b in RangeSet([(lo, hi)]).intersect(start, end_ms):
                wanted.add(a, b)
        result.requested_minutes = wanted.total() // STEP

        # Holes in our record (after the first stored bar) are gaps: register before fetching.
        if first is not None:
            for lo, hi in wanted.intersect(first, end_ms):
                self._open(market, symbol, lo, hi, obs, result, detail="found by startup backfill")

        returned: set[int] = set()
        for lo, hi in wanted.ranges():
            try:
                rows = await self._fetch(market, symbol, lo, hi)
            except Exception as exc:  # leave the range missing; it is (or becomes) a gap
                msg = f"{lo}-{hi}: {exc!r}"
                result.errors.append(msg)
                log.warning(
                    "backfill fetch failed",
                    extra={"market": str(market), "symbol": symbol, "error": msg},
                )
                continue
            for row in rows:
                if not lo <= row.open_time_ms < hi or row.close_time_ms >= end_ms:
                    continue
                source = (
                    BarSource.REST_RECOVERED
                    if obs.observed(row.open_time_ms)
                    else BarSource.REST_BACKFILL
                )
                try:
                    bar = bar_from_rest(market, symbol, row, source, self._now())
                except ValueError as exc:
                    result.invalid += 1
                    log.error(
                        "invalid REST kline rejected in backfill",
                        extra={
                            "market": str(market),
                            "symbol": symbol,
                            "open_time_ms": row.open_time_ms,
                            "error": str(exc),
                        },
                    )
                    continue
                wr = self._store.write(bar, None)
                if wr.outcome is WriteOutcome.INSERTED:
                    if source is BarSource.REST_RECOVERED:
                        result.inserted_recovered += 1
                    else:
                        result.inserted_backfill += 1
                elif wr.outcome is WriteOutcome.DUPLICATE:
                    result.duplicates += 1
                else:
                    result.conflicts += 1
                    existing = wr.existing
                    self._emit(
                        NewEvent(
                            type=EventType.BAR_CONFLICT,
                            aggregate_type=AggregateType.MARKET_DATA,
                            aggregate_id=symbol_id(market, symbol),
                            ts_ms=self._now(),
                            payload={
                                "market": str(market),
                                "symbol": symbol,
                                "open_time_ms": bar.open_time_ms,
                                "stored": None if existing is None else bar_payload(existing.bar),
                                "incoming": bar_payload(bar),
                                "kept": "stored",
                                "rule": "backfill never replaces a stored bar",
                            },
                        )
                    )
                returned.add(row.open_time_ms)

        if returned:
            result.gaps_resolved_minutes = self._gaps.resolve(
                market, symbol, returned, "startup_backfill"
            )
        # Initial-load minutes REST did not return (holes were registered above).
        not_returned = [
            m
            for lo, hi in wanted.ranges()
            for m in range(lo, hi, STEP)
            if m not in returned and (first is None or m < first)
        ]
        for lo, hi in minutes_to_ranges(not_returned):
            self._open(market, symbol, lo, hi, obs, result, detail="not returned by REST")

        result.remaining_open_minutes = sum(
            g.missing_minutes for g in self._gaps.open_gaps(market, symbol)
        )
        self._emit(
            NewEvent(
                type=EventType.BACKFILL_COMPLETED,
                aggregate_type=AggregateType.MARKET_DATA,
                aggregate_id=symbol_id(market, symbol),
                ts_ms=self._now(),
                payload={
                    "market": str(market),
                    "symbol": symbol,
                    "from_ms": result.from_ms,
                    "to_ms": result.to_ms,
                    "requested_minutes": result.requested_minutes,
                    "inserted_rest_backfill": result.inserted_backfill,
                    "inserted_rest_recovered": result.inserted_recovered,
                    "duplicates": result.duplicates,
                    "conflicts": result.conflicts,
                    "invalid": result.invalid,
                    "gaps_opened_minutes": result.gaps_opened_minutes,
                    "gaps_resolved_minutes": result.gaps_resolved_minutes,
                    "remaining_open_minutes": result.remaining_open_minutes,
                    "errors": result.errors[:20],
                },
            )
        )
        return result

    def _open(
        self,
        market: Market,
        symbol: str,
        lo: int,
        hi: int,
        obs: ObservationIndex,
        result: BackfillResult,
        detail: str,
    ) -> None:
        observed, unobserved = obs.split(lo, hi)
        for ranges, cause in ((observed, GapCause.UNKNOWN), (unobserved, GapCause.DOWNTIME)):
            if not ranges:
                continue
            for rec in self._gaps.open_ranges(market, symbol, ranges, cause, detail=detail):
                result.gaps_opened_minutes += rec.total_minutes
