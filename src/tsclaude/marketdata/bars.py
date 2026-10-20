"""Bar builder: kline events -> closed canonical 1m bars with provenance (DATA_ARCHITECTURE §3).

One `SymbolBarBuilder` per (market, symbol); `BarBuilder` routes messages to them.

Watermark rule. A minute `[t, t+60 s)` with `t >= watch_from_ms` is accepted as closed when
1. its WS kline arrives with `x == true` -> `Bar(source=ws_live)`, or
2. server time passes `t + 60 s + grace` without (1) -> REST fetch; if returned ->
   `Bar(source=rest_recovered)` plus a `BAR_LATE` event; if not -> `GAP_DETECTED`.
Minutes before `watch_from_ms` belong to startup backfill, not to the builder.

Duplicates. Identical re-deliveries (e.g. two connections during a planned recycle) are counted and
ignored. A differing bar for a stored minute emits `BAR_CONFLICT` carrying both versions; the
WS-closed version is kept (see `store.livebars`).

Partial candles. The latest forming kline is kept in memory only (`partial()`), for display as
`PARTIAL`. It is never written to storage and never published as a bar.

`observed_session_id` is set only on `ws_live` bars: those are the only bars actually *seen live*.
Recovered bars are inside observation coverage (a fact recorded by the session intervals) but
were not seen on the stream.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from tsclaude.contracts.events import AggregateType, EventType, NewEvent
from tsclaude.contracts.market import Bar, BarSource, Interval, Market
from tsclaude.contracts.timeutil import MS_PER_MINUTE
from tsclaude.marketdata.convert import bar_from_rest, bar_from_ws, bar_payload
from tsclaude.marketdata.types import EventSink, KlineUpdate, RestKline, symbol_id
from tsclaude.quality.gaps import GapCause, GapRegistry
from tsclaude.store.livebars import LiveBarStore, WriteOutcome

log = logging.getLogger(__name__)

STEP = MS_PER_MINUTE

type KlineFetcher = Callable[[Market, str, int, int], Awaitable[list[RestKline]]]
"""Fetch 1m REST klines with `start_ms <= open_time < end_ms` (pagination is the fetcher's job)."""

type CauseFor = Callable[[Market, str], GapCause]


@dataclass(frozen=True, slots=True)
class BarClosed:
    """Published to consumers when a closed bar is stored (or replaces a non-WS version)."""

    bar: Bar
    replaced: bool = False  # True: a WS-closed bar replaced a REST version of the same minute


type BarListener = Callable[[BarClosed], None]


@dataclass
class BuilderStats:
    ws_closed: int = 0
    ws_partial: int = 0
    duplicates: int = 0
    conflicts: int = 0
    late_recovered: int = 0
    gap_minutes: int = 0
    invalid: int = 0
    ignored: int = 0
    rest_errors: int = 0
    last_closed_open_ms: int | None = None
    last_closed_at_ms: int | None = None


@dataclass
class SymbolBarBuilder:
    market: Market
    symbol: str
    watch_from_ms: int
    store: LiveBarStore
    gaps: GapRegistry
    emit: EventSink
    now_ms: Callable[[], int]  # local wall clock (ingested_at)
    server_now_ms: Callable[[], int]  # corrected server time (watermark)
    session_id: str | None
    grace_ms: int = 5_000
    on_bar: BarListener | None = None
    cause_for: CauseFor | None = None
    stats: BuilderStats = field(default_factory=BuilderStats)
    _cursor: int = 0
    _settled: set[int] = field(default_factory=set)
    _partial: KlineUpdate | None = None

    def __post_init__(self) -> None:
        if self.watch_from_ms % STEP != 0:
            raise ValueError("watch_from_ms must be a 1m open time")
        self._cursor = self.watch_from_ms

    # ------------------------------------------------------------------ queries
    def partial(self) -> KlineUpdate | None:
        return self._partial

    def deadline_ms(self, open_time_ms: int) -> int:
        return open_time_ms + STEP + self.grace_ms

    @property
    def cursor_ms(self) -> int:
        """Earliest minute (>= watch_from) not yet settled (closed, recovered or registered gap)."""
        return self._cursor

    def due_minutes(self) -> list[int]:
        """Minutes whose watermark deadline has passed without a closed bar."""
        now = self.server_now_ms()
        out: list[int] = []
        m = self._cursor
        while now > self.deadline_ms(m):
            if m not in self._settled:
                out.append(m)
            m += STEP
        return out

    def caught_up(self) -> bool:
        """No minute is overdue and no open gap remains for this symbol."""
        return not self.due_minutes() and not self.gaps.open_minutes(self.market, self.symbol)

    # ------------------------------------------------------------------ inputs
    def on_kline(self, k: KlineUpdate) -> None:
        if k.market is not self.market or k.symbol != self.symbol:
            raise ValueError("kline routed to the wrong builder")
        if k.interval is not Interval.M1:
            self.stats.ignored += 1
            return
        if not k.is_closed:
            self._update_partial(k)
            return
        try:
            bar = bar_from_ws(k, self.now_ms())
        except ValueError as exc:
            self.stats.invalid += 1
            log.error(
                "invalid closed kline rejected",
                extra={"market": str(self.market), "symbol": self.symbol,
                       "open_time_ms": k.open_time_ms, "error": str(exc)},
            )
            return
        self.stats.ws_closed += 1
        if self._partial is not None and self._partial.open_time_ms <= k.open_time_ms:
            self._partial = None
        self._store(bar, self.session_id)
        self._settle(bar.open_time_ms)
        if self.gaps.is_missing(self.market, self.symbol, bar.open_time_ms):
            self.gaps.resolve(self.market, self.symbol, [bar.open_time_ms], str(BarSource.WS_LIVE))

    def _update_partial(self, k: KlineUpdate) -> None:
        self.stats.ws_partial += 1
        last = self.stats.last_closed_open_ms
        if last is not None and k.open_time_ms <= last:
            return  # forming update for a minute that is already closed
        p = self._partial
        if p is None or k.open_time_ms > p.open_time_ms or (
            k.open_time_ms == p.open_time_ms and k.event_time_ms >= p.event_time_ms
        ):
            self._partial = k

    def _settle(self, open_time_ms: int) -> None:
        if open_time_ms < self._cursor:
            return
        self._settled.add(open_time_ms)
        while self._cursor in self._settled:
            self._settled.discard(self._cursor)
            self._cursor += STEP

    def _store(self, bar: Bar, session_id: str | None) -> WriteOutcome:
        result = self.store.write(bar, session_id)
        if result.outcome is WriteOutcome.DUPLICATE:
            self.stats.duplicates += 1
            return result.outcome
        if result.outcome in (WriteOutcome.CONFLICT, WriteOutcome.REPLACED):
            self.stats.conflicts += 1
            assert result.existing is not None  # noqa: S101 - store guarantees it
            kept = bar if result.outcome is WriteOutcome.REPLACED else result.existing.bar
            log.warning(
                "bar conflict",
                extra={"market": str(bar.market), "symbol": bar.symbol,
                       "open_time_ms": bar.open_time_ms, "kept": str(kept.source)},
            )
            self.emit(
                NewEvent(
                    type=EventType.BAR_CONFLICT,
                    aggregate_type=AggregateType.MARKET_DATA,
                    aggregate_id=symbol_id(bar.market, bar.symbol),
                    ts_ms=self.now_ms(),
                    payload={
                        "market": str(bar.market),
                        "symbol": bar.symbol,
                        "open_time_ms": bar.open_time_ms,
                        "stored": bar_payload(result.existing.bar),
                        "incoming": bar_payload(bar),
                        "kept": "incoming" if result.outcome is WriteOutcome.REPLACED else "stored",
                        "rule": "ws_live wins over rest; otherwise first stored is kept",
                    },
                )
            )
        if result.outcome in (WriteOutcome.INSERTED, WriteOutcome.REPLACED):
            last = self.stats.last_closed_open_ms
            if last is None or bar.open_time_ms > last:
                self.stats.last_closed_open_ms = bar.open_time_ms
                self.stats.last_closed_at_ms = self.now_ms()
            if self.on_bar is not None:
                self.on_bar(BarClosed(bar, replaced=result.outcome is WriteOutcome.REPLACED))
        return result.outcome

    # ------------------------------------------------------------------ watermark
    async def check_watermark(self, fetch: KlineFetcher) -> list[int]:
        """Recover overdue minutes via REST; register the rest as gaps. Returns overdue minutes."""
        due = self.due_minutes()
        if not due:
            return []
        fetched: dict[int, RestKline] = {}
        error: str | None = None
        try:
            rows = await fetch(self.market, self.symbol, due[0], due[-1] + STEP)
            server_now = self.server_now_ms()
            fetched = {r.open_time_ms: r for r in rows if r.close_time_ms < server_now}
        except Exception as exc:  # a REST failure must become a visible gap, never a crash
            self.stats.rest_errors += 1
            error = repr(exc)
            log.warning(
                "REST recovery failed",
                extra={"market": str(self.market), "symbol": self.symbol, "error": error},
            )
        missing: list[int] = []
        for m in due:
            if m in self._settled or m < self._cursor:
                continue  # a WS close arrived while we were waiting for REST
            row = fetched.get(m)
            if row is not None and self._recover(row, m):
                self._settle(m)
                continue
            missing.append(m)
        if missing:
            cause = self.cause_for(self.market, self.symbol) if self.cause_for else GapCause.UNKNOWN
            self.gaps.open(self.market, self.symbol, missing, cause, detail=error)
            self.stats.gap_minutes += len(missing)
            for m in missing:
                self._settle(m)
        return due

    def _recover(self, row: RestKline, open_time_ms: int) -> bool:
        try:
            bar = bar_from_rest(
                self.market, self.symbol, row, BarSource.REST_RECOVERED, self.now_ms()
            )
        except ValueError as exc:
            self.stats.invalid += 1
            log.error(
                "invalid REST kline rejected",
                extra={"market": str(self.market), "symbol": self.symbol,
                       "open_time_ms": open_time_ms, "error": str(exc)},
            )
            return False
        outcome = self._store(bar, None)
        if outcome is WriteOutcome.INSERTED:
            self.stats.late_recovered += 1
            now = self.now_ms()
            self.emit(
                NewEvent(
                    type=EventType.BAR_LATE,
                    aggregate_type=AggregateType.MARKET_DATA,
                    aggregate_id=symbol_id(self.market, self.symbol),
                    ts_ms=now,
                    payload={
                        "market": str(self.market),
                        "symbol": self.symbol,
                        "open_time_ms": open_time_ms,
                        "deadline_ms": self.deadline_ms(open_time_ms),
                        "server_now_ms": self.server_now_ms(),
                        "recovered_at_ms": now,
                        "source": str(BarSource.REST_RECOVERED),
                    },
                )
            )
        return True

    async def retry_gaps(self, fetch: KlineFetcher) -> int:
        """Try to recover open gap minutes for this symbol. Returns minutes recovered."""
        open_minutes = sorted(self.gaps.open_minutes(self.market, self.symbol))
        if not open_minutes:
            return 0
        for g in self.gaps.open_gaps(self.market, self.symbol):
            g.attempts += 1
        try:
            rows = await fetch(self.market, self.symbol, open_minutes[0], open_minutes[-1] + STEP)
        except Exception as exc:
            self.stats.rest_errors += 1
            log.warning(
                "gap retry failed",
                extra={"market": str(self.market), "symbol": self.symbol, "error": repr(exc)},
            )
            return 0
        wanted = set(open_minutes)
        server_now = self.server_now_ms()
        recovered: list[int] = []
        for row in rows:
            if row.open_time_ms not in wanted or row.close_time_ms >= server_now:
                continue
            try:
                bar = bar_from_rest(
                    self.market, self.symbol, row, BarSource.REST_RECOVERED, self.now_ms()
                )
            except ValueError:
                self.stats.invalid += 1
                continue
            outcome = self._store(bar, None)
            if outcome is WriteOutcome.INSERTED:
                self.stats.late_recovered += 1
            # Whatever the outcome, a closed bar is now stored for this minute.
            recovered.append(row.open_time_ms)
        return self.gaps.resolve(
            self.market, self.symbol, recovered, str(BarSource.REST_RECOVERED)
        )


class BarBuilder:
    """Routes kline updates to per-(market, symbol) builders."""

    def __init__(self) -> None:
        self._builders: dict[tuple[Market, str], SymbolBarBuilder] = {}
        self.unrouted = 0

    def add(self, builder: SymbolBarBuilder) -> None:
        key = (builder.market, builder.symbol)
        if key in self._builders:
            raise ValueError(f"builder for {key} already exists")
        self._builders[key] = builder

    def get(self, market: Market, symbol: str) -> SymbolBarBuilder | None:
        return self._builders.get((market, symbol))

    def all(self) -> list[SymbolBarBuilder]:
        return list(self._builders.values())

    def on_kline(self, k: KlineUpdate) -> bool:
        b = self._builders.get((k.market, k.symbol))
        if b is None:
            self.unrouted += 1
            return False
        b.on_kline(k)
        return True
