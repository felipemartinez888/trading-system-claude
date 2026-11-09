"""Startup backfill of closed 1m bars over REST (no network library; the fetcher is injected).

For each (market, symbol), up to `end_ms` (the open time of the current, still-forming minute, by
server time):

- Window: the last `lookback` **plus**, in the legacy mode (`extend_to_last_stored`), everything
  since the last stored bar if that is older (capped at `max_backfill`; older missing history is
  registered as a `downtime` gap for the M2 archive importer). The bounded startup catch-up turns
  the extension off: its window is exactly `lookback` and the older downtime is registered as a
  gap (loaded later by the on-demand history run, or left to the importer). The whole window is
  compared against stored bars, not just "after the last bar", so a minute lost by a crash between
  two stored bars is found too.
- Holes after the first stored bar are *gaps in our record*: they are registered
  (`GAP_DETECTED`) before fetching — crash-safe — and closed by `GAP_RESOLVED` for what REST
  returns. Minutes before any stored history are an initial load, not a gap; only those REST does
  not return are registered.
- Open gaps already in the registry inside the window are retried as part of the same run.
- Tagging: a minute that overlaps an observation interval (the app was monitoring) is
  `rest_recovered`; otherwise `rest_backfill`. Gap cause follows the same split: `unknown` inside
  observation, `downtime` outside.
- Idempotent: a second run finds no missing minutes and writes nothing; identical rows are no-ops.
- One `BACKFILL_COMPLETED` event per (market, symbol) and run, with its terminal `status`.

Execution (bounded, resumable, cancellable):

- The missing ranges are processed **newest first** in chunks of `chunk_minutes`. Newest first keeps
  the stored data a contiguous recent suffix, so an interrupted initial load leaves its remainder
  *before* the first stored bar (still an initial load, not a gap) and a warm-up reading the most
  recent minutes sees either enough contiguous history or too little (never a false "ready").
- One chunk at a time is in flight: fetch (with a timeout) -> validate -> **one** transaction that
  stores the chunk and its checkpoint (`BarSink`) -> resolve gaps -> publish progress. The awaited
  write is the backpressure; nothing is queued.
- Between chunks the run yields (`pause_s`), and it checks the stop request and its time budgets,
  so Stop is deterministic: the current chunk finishes, nothing is half-written.
- The checkpoint is advisory and verified against the stored bars at the start of every run; the
  stored bars (and the gap registry) always decide what is fetched.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from tsclaude.contracts.events import AggregateType, EventType, NewEvent
from tsclaude.contracts.market import Bar, BarSource, Market
from tsclaude.contracts.timeutil import MS_PER_DAY, MS_PER_MINUTE
from tsclaude.marketdata.bars import KlineFetcher
from tsclaude.marketdata.convert import bar_from_rest, bar_payload
from tsclaude.marketdata.types import EventSink, RestKline, symbol_id
from tsclaude.marketdata.writer import BarSink, InlineSink, WriterStuck
from tsclaude.quality.gaps import GapCause, GapRegistry, RangeSet, minutes_to_ranges
from tsclaude.runtime.sessions import ObservationInterval
from tsclaude.store.livebars import (
    CHECKPOINT_SCHEMA,
    Checkpoint,
    LiveBarStore,
    WriteOutcome,
    WriteResult,
)

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
    #: legacy rule: also cover everything since the last stored bar (capped at `max_backfill_ms`)
    extend_to_last_stored: bool = True


@dataclass(frozen=True, slots=True)
class RunLimits:
    """Workload limits of one run; the defaults are the production values of `BackfillRunConfig`."""

    chunk_minutes: int = 1000
    pause_s: float = 0.0
    chunk_timeout_s: float = 30.0
    max_consecutive_errors: int = 3
    pair_budget_s: float | None = None


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
    job: str = "startup"
    #: complete | partial (all chunks tried, some failed) | paused (a time budget ended the run) |
    #: cancelled (stop requested) | failed (consecutive chunk errors, or a stuck writer)
    status: str = "complete"
    chunks_done: int = 0
    chunks_total: int = 0
    checkpoint: str = "none"  # verdict on the checkpoint found at the start of the run

    @property
    def inserted(self) -> int:
        return self.inserted_backfill + self.inserted_recovered


@dataclass
class PairProgress:
    """Live progress of one (job, market, symbol); updated by the engine on the event loop."""

    market: Market
    symbol: str
    job: str
    state: str = "pending"  # pending | running | complete | paused | cancelled | failed
    window_from_ms: int = 0
    window_to_ms: int = 0
    cursor_ms: int = 0  # everything wanted in [cursor_ms, window_to_ms) is processed
    chunks_done: int = 0
    chunks_total: int = 0
    requested_minutes: int = 0
    rows_processed: int = 0
    inserted: int = 0
    errors: int = 0
    last_error: str | None = None
    checkpoint: str = "none"
    detail: str | None = None
    started_ms: int | None = None
    updated_ms: int | None = None


class ObservationIndex:
    """Which 1m minutes lie entirely inside an observation interval (the app was monitoring)?

    Lead decision L-02 (docs/DECISIONS_PENDING.md): minute `m` is observed only if
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
        *,
        sink: BarSink | None = None,
        limits: RunLimits | None = None,
        job: str = "startup",
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._store = store
        self._gaps = gaps
        self._fetch = fetch
        self._emit = emit
        self._now = now_ms
        self._settings = settings or BackfillSettings()
        self._sink: BarSink = sink if sink is not None else InlineSink(store)
        self._limits = limits or RunLimits()
        self._job = job
        self._mono = monotonic
        self._sleep = sleep

    async def run(
        self,
        market: Market,
        symbol: str,
        end_ms: int,
        intervals: Sequence[ObservationInterval],
        *,
        progress: PairProgress | None = None,
        should_stop: Callable[[], bool] | None = None,
        deadline: float | None = None,
    ) -> BackfillResult:
        """Backfill one pair. `deadline` is an absolute `monotonic()` time (the run's budget)."""
        if end_ms % STEP:
            raise ValueError("end_ms must be a 1m open time (the current forming minute)")
        s, lim = self._settings, self._limits
        obs = ObservationIndex(intervals, self._now())
        start = end_ms - s.lookback_ms
        floor = end_ms - s.max_backfill_ms
        last = self._store.last(market, symbol)
        if s.extend_to_last_stored and last is not None and last.bar.open_time_ms + STEP < start:
            start = max(last.bar.open_time_ms + STEP, floor)
        start -= start % STEP
        result = BackfillResult(market, symbol, start, end_ms, job=self._job)
        prog = progress if progress is not None else PairProgress(market, symbol, self._job)
        prog.state = "running"
        prog.window_from_ms, prog.window_to_ms, prog.cursor_ms = start, end_ms, end_ms
        prog.started_ms = prog.updated_ms = self._now()
        started = self._mono()
        deadlines = [started + lim.pair_budget_s] if lim.pair_budget_s is not None else []
        if deadline is not None:
            deadlines.append(deadline)
        cutoff = min(deadlines) if deadlines else None

        result.checkpoint = prog.checkpoint = self._verify_checkpoint(market, symbol)
        if result.checkpoint.startswith("mismatch"):
            log.warning(
                "ingest checkpoint does not match the stored bars; resuming from the stored bars",
                extra={
                    "market": str(market),
                    "symbol": symbol,
                    "job": self._job,
                    "verdict": result.checkpoint,
                },
            )

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
        # Bounded catch-up: the downtime older than the window is not loaded, only registered.
        if not s.extend_to_last_stored and last is not None:
            lo = max(last.bar.open_time_ms + STEP, floor)
            if lo < start:
                self._open(
                    market,
                    symbol,
                    lo,
                    start,
                    obs,
                    result,
                    detail="older than the startup catch-up; left for the history run",
                )

        stored = self._store.open_times(market, symbol, start, end_ms)
        first = self._store.first_open_time(market, symbol)
        wanted = RangeSet(
            minutes_to_ranges(m for m in range(start, end_ms, STEP) if m not in stored)
        )
        # Open registry gaps inside the window are fetched too (they are unstored by definition).
        for lo, hi in self._gaps.missing_ranges(market, symbol):
            for a, b in RangeSet([(lo, hi)]).intersect(start, end_ms):
                wanted.add(a, b)
        result.requested_minutes = prog.requested_minutes = wanted.total() // STEP

        # Holes in our record (after the first stored bar) are gaps: register before fetching.
        if first is not None:
            for lo, hi in wanted.intersect(first, end_ms):
                self._open(market, symbol, lo, hi, obs, result, detail="found by startup backfill")

        chunks = _chunks(wanted.ranges(), lim.chunk_minutes)
        result.chunks_total = prog.chunks_total = len(chunks)
        consecutive = 0
        rows_processed = 0
        cancelled: asyncio.CancelledError | None = None
        stuck = False
        try:
            for index, (lo, hi) in enumerate(chunks):
                if should_stop is not None and should_stop():
                    result.status, prog.detail = "cancelled", "stop requested"
                    break
                if cutoff is not None and self._mono() >= cutoff:
                    result.status, prog.detail = "paused", "time budget reached; resumable"
                    break
                if index and lim.pause_s > 0:
                    await self._sleep(lim.pause_s)
                    if should_stop is not None and should_stop():
                        result.status, prog.detail = "cancelled", "stop requested"
                        break
                error: str | None = None
                bars: list[Bar] = []
                try:
                    rows = await asyncio.wait_for(
                        self._fetch(market, symbol, lo, hi), lim.chunk_timeout_s
                    )
                    bars = self._convert(market, symbol, rows, lo, hi, end_ms, obs, result)
                    checkpoint = Checkpoint(
                        job=self._job,
                        market=market,
                        symbol=symbol,
                        window_from_ms=start,
                        window_to_ms=end_ms,
                        cursor_ms=lo,
                        rows_written=rows_processed + len(bars),
                        chunks_done=result.chunks_done + 1,
                        status="running",
                        updated_at_ms=self._now(),
                    )
                    outcomes = await self._sink.write(bars, checkpoint)
                except asyncio.CancelledError:
                    raise
                except WriterStuck as exc:
                    result.errors.append(f"{lo}-{hi}: {exc!r}")
                    prog.last_error = str(exc)
                    prog.errors += 1
                    stuck = True
                    result.status, prog.detail = "failed", "the writer is stuck; run abandoned"
                    break
                except Exception as exc:  # leave the chunk missing; it is (or becomes) a gap
                    error = f"{lo}-{hi}: {exc!r}"
                    result.errors.append(error)
                    prog.errors += 1
                    prog.last_error = error
                    log.warning(
                        "backfill chunk failed",
                        extra={"market": str(market), "symbol": symbol, "error": error},
                    )
                    outcomes = []
                    bars = []
                returned = self._tally(bars, outcomes, market, symbol, result)
                rows_processed += len(returned)
                if returned:
                    result.gaps_resolved_minutes += self._gaps.resolve(
                        market, symbol, returned, "startup_backfill"
                    )
                # Initial-load minutes of this chunk REST did not return (holes: registered above).
                missing = [
                    m
                    for m in range(lo, hi, STEP)
                    if m not in returned and (first is None or m < first)
                ]
                for a, b in minutes_to_ranges(missing):
                    self._open(market, symbol, a, b, obs, result, detail="not returned by REST")
                if error is None:
                    consecutive = 0
                    result.chunks_done += 1
                    prog.chunks_done = result.chunks_done
                    prog.cursor_ms = lo
                    prog.rows_processed = rows_processed
                    prog.inserted = result.inserted
                else:
                    consecutive += 1
                    if consecutive >= lim.max_consecutive_errors:
                        result.status = "failed"
                        prog.detail = f"{consecutive} consecutive chunk errors"
                        break
                prog.updated_ms = self._now()
        except asyncio.CancelledError as exc:
            cancelled = exc
            result.status, prog.detail = "cancelled", "task cancelled"

        if result.status == "complete" and result.errors:
            result.status = prog.detail = "partial"  # every chunk was tried; some failed
        if not stuck:
            await self._finish_checkpoint(
                market, symbol, start, end_ms, result, prog, rows_processed
            )
        result.remaining_open_minutes = sum(
            g.missing_minutes for g in self._gaps.open_gaps(market, symbol)
        )
        prog.state = result.status
        prog.updated_ms = self._now()
        self._emit(
            NewEvent(
                type=EventType.BACKFILL_COMPLETED,
                aggregate_type=AggregateType.MARKET_DATA,
                aggregate_id=symbol_id(market, symbol),
                ts_ms=self._now(),
                payload={
                    "market": str(market),
                    "symbol": symbol,
                    "job": self._job,
                    "status": result.status,
                    "from_ms": result.from_ms,
                    "to_ms": result.to_ms,
                    "requested_minutes": result.requested_minutes,
                    "chunks_done": result.chunks_done,
                    "chunks_total": result.chunks_total,
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
        if cancelled is not None:
            raise cancelled
        return result

    # ------------------------------------------------------------------ chunk helpers
    def _convert(
        self,
        market: Market,
        symbol: str,
        rows: Sequence[RestKline],
        lo: int,
        hi: int,
        end_ms: int,
        obs: ObservationIndex,
        result: BackfillResult,
    ) -> list[Bar]:
        bars: list[Bar] = []
        for row in rows:
            if not lo <= row.open_time_ms < hi or row.close_time_ms >= end_ms:
                continue
            source = (
                BarSource.REST_RECOVERED
                if obs.observed(row.open_time_ms)
                else BarSource.REST_BACKFILL
            )
            try:
                bars.append(bar_from_rest(market, symbol, row, source, self._now()))
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
        return bars

    def _tally(
        self,
        bars: Sequence[Bar],
        outcomes: Sequence[WriteResult],
        market: Market,
        symbol: str,
        result: BackfillResult,
    ) -> set[int]:
        """Count a committed chunk; emit `bar_conflict` for rows that differ from a stored bar."""
        returned: set[int] = set()
        for bar, wr in zip(bars, outcomes, strict=True):
            if wr.outcome is WriteOutcome.INSERTED:
                if bar.source is BarSource.REST_RECOVERED:
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
            returned.add(bar.open_time_ms)
        return returned

    def _verify_checkpoint(self, market: Market, symbol: str) -> str:
        """Compare the saved checkpoint with the stored bars. Never changes what is fetched (the
        stored bars and the gap registry decide that); it makes an inconsistency visible."""
        cp = self._store.get_checkpoint(self._job, market, symbol)
        if cp is None:
            return "none"
        if cp.schema_version != CHECKPOINT_SCHEMA:
            return "ignored:schema"
        if not cp.window_from_ms <= cp.cursor_ms <= cp.window_to_ms:
            return "ignored:invalid"
        have = self._store.open_times(market, symbol, cp.cursor_ms, cp.window_to_ms)
        known_gaps = RangeSet(self._gaps.missing_ranges(market, symbol))
        unexplained = sum(
            1
            for m in range(cp.cursor_ms, cp.window_to_ms, STEP)
            if m not in have and not known_gaps.contains(m)
        )
        return "ok" if unexplained == 0 else f"mismatch:{unexplained}_unexplained_missing_minutes"

    async def _finish_checkpoint(
        self,
        market: Market,
        symbol: str,
        start: int,
        end_ms: int,
        result: BackfillResult,
        prog: PairProgress,
        rows_processed: int,
    ) -> None:
        cp = Checkpoint(
            job=self._job,
            market=market,
            symbol=symbol,
            window_from_ms=start,
            window_to_ms=end_ms,
            cursor_ms=start if result.status == "complete" else prog.cursor_ms,
            rows_written=rows_processed,
            chunks_done=result.chunks_done,
            status=result.status,
            updated_at_ms=self._now(),
        )
        try:
            await self._sink.write([], cp)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("final ingest checkpoint was not stored", extra={"error": repr(exc)})
            result.errors.append(f"checkpoint: {exc!r}")

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


def _chunks(ranges: Sequence[tuple[int, int]], chunk_minutes: int) -> list[tuple[int, int]]:
    """Missing ranges split into chunks of at most `chunk_minutes`, newest first."""
    size = chunk_minutes * STEP
    out: list[tuple[int, int]] = []
    for lo, hi in sorted(ranges, reverse=True):
        top = hi
        while top > lo:
            bottom = max(lo, top - size)
            out.append((bottom, top))
            top = bottom
    return out
