"""The simulator loop: one engine for backtests, replays and paper trading (ADR-0005).

Per closed 1m bar (bars arrive in global order, see `clock`):

1. Every book processes the bar for its (market, symbol): exits of positions held from before the
   bar, then entries whose earliest execution time has come (`Book.on_bar`).
2. The bar is delivered to the signal source, which may return `EntryRequest`s / `ExitRequest`s.
   Their decision time is stamped as the bar's ``close_time_ms``; their earliest execution is the
   first 1m open at or after ``close_time_ms + 1 + decision_latency_ms`` -- never the bar they were
   computed from.
3. When the first bar of a later minute arrives (or at `finish`), every book is marked to market at
   the previous minute's close. Marking is therefore independent of how the stream is chunked.

Downtime resolution (SIMULATION §7). ``step(bar, observed=False)`` processes a backfilled bar of a
period the app did not observe: open positions resolve their resting stop-loss / take-profit / due
exits on it (trades tagged ``resolved_from_backfill``), resting entries can only expire, and the
signal source is **not** called, so no entry is ever created for an unobserved minute. Every switch
between observed and unobserved processing is an event (`SimEventType.OBSERVATION_CHANGED`).

Events
------
Every state change is an event dict with the fields of `contracts.events.NewEvent` plus a running
``seq``: ``{"seq", "ts_ms", "type", "aggregate_type", "aggregate_id", "payload"}``. Payload values
are JSON scalars (Decimals as plain strings). The run **fingerprint** is SHA-256 over the canonical
JSON (`contracts.events.canonical_json`) of the full event list. `clock.mode` is deliberately not
part of any event, so a live session replayed through `HistoricalClock` reproduces the fingerprint
byte-for-byte; the mode travels separately as the provenance label of the result.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from tsclaude.contracts.events import canonical_json
from tsclaude.contracts.market import Bar, Interval, Market
from tsclaude.contracts.provenance import Provenance
from tsclaude.contracts.signals import RunMode
from tsclaude.contracts.timeutil import MS_PER_MINUTE
from tsclaude.sim.book import Book, SimEventType
from tsclaude.sim.clock import MODE_PROVENANCE, Clock, ClockError, order_key
from tsclaude.sim.orders import EntryRequest, ExitRequest

SIM_ENGINE_VERSION = "sim-v0"
AGGREGATE_TYPE = "sim"  # contract change requested: AggregateType.SIM
RUN_AGGREGATE_ID = "run"

type SimEvent = dict[str, Any]
type Request = EntryRequest | ExitRequest


class SimView(Protocol):
    """Read-only state a signal source may consult (only the past: everything up to this close)."""

    def position_qty(self, book: str, market: Market, symbol: str) -> Decimal: ...

    def has_pending(self, book: str, market: Market, symbol: str) -> bool: ...

    def equity(self, book: str) -> Decimal: ...


class SignalSource(Protocol):
    """Strategy adapter. Receives each closed 1m bar once, after the books processed it."""

    def on_bar(self, bar: Bar, view: SimView) -> Sequence[Request]: ...


class NullSignalSource:
    def on_bar(self, bar: Bar, view: SimView) -> Sequence[Request]:
        return ()


def earliest_execution_ms(decision_time_ms: int, decision_latency_ms: int) -> int:
    """First 1m open strictly after the decision time plus latency."""
    t = decision_time_ms + 1 + decision_latency_ms
    return -(-t // MS_PER_MINUTE) * MS_PER_MINUTE


def fingerprint(events: Sequence[SimEvent]) -> str:
    return hashlib.sha256(canonical_json(list(events)).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SimResult:
    mode: RunMode
    provenance: Provenance
    events: tuple[SimEvent, ...]
    fingerprint: str
    ledger_hashes: Mapping[str, str]
    summaries: Mapping[str, dict[str, Any]]


class _View:
    def __init__(self, books: Mapping[str, Book]) -> None:
        self._books = books

    def position_qty(self, book: str, market: Market, symbol: str) -> Decimal:
        return self._books[book].position_qty(market, symbol)

    def has_pending(self, book: str, market: Market, symbol: str) -> bool:
        return self._books[book].has_pending(market, symbol)

    def equity(self, book: str) -> Decimal:
        return self._books[book].equity()


class Simulator:
    def __init__(
        self,
        clock: Clock,
        books: Sequence[Book],
        signal_source: SignalSource | None = None,
        *,
        decision_latency_ms: int = 0,
    ) -> None:
        if not books:
            raise ValueError("at least one book is required")
        names = [b.name for b in books]
        if len(set(names)) != len(names):
            raise ValueError("book names must be unique")
        if decision_latency_ms < 0:
            raise ValueError("decision_latency_ms must be >= 0")
        self.clock = clock
        self.books: dict[str, Book] = {b.name: b for b in books}
        self.source: SignalSource = signal_source or NullSignalSource()
        self.decision_latency_ms = decision_latency_ms
        self._view = _View(self.books)
        self._events: list[SimEvent] = []
        self._last_key: tuple[int, str, str] | None = None
        self._last_open: dict[tuple[Market, str], int] = {}
        self._minute: int | None = None
        self._last_close_ms: int | None = None
        self._started = False
        self._finished = False
        self._observed = True

    # ------------------------------------------------------------ events
    def _append(
        self, kind: SimEventType, ts_ms: int, aggregate_id: str, payload: dict[str, Any]
    ) -> None:
        self._events.append(
            {
                "seq": len(self._events) + 1,
                "ts_ms": ts_ms,
                "type": str(kind),
                "aggregate_type": AGGREGATE_TYPE,
                "aggregate_id": aggregate_id,
                "payload": payload,
            }
        )

    def _drain_books(self) -> None:
        for name in self.books:  # insertion order = constructor order, deterministic
            for kind, ts, payload in self.books[name].drain():
                self._append(kind, ts, name, payload)

    @property
    def events(self) -> tuple[SimEvent, ...]:
        return tuple(self._events)

    # ------------------------------------------------------------ driving
    def run(self) -> SimResult:
        """Consume the whole clock and finish the run."""
        for bar in self.clock:
            self.step(bar)
        return self.finish()

    def feed(self, bars: Iterable[Bar], *, observed: bool = True) -> None:
        """Process a chunk of bars (live sessions, chunked replays)."""
        for bar in bars:
            self.step(bar, observed=observed)

    def last_open(self, market: Market, symbol: str) -> int | None:
        """Open time of the latest processed bar of (market, symbol), if any."""
        return self._last_open.get((market, symbol))

    @property
    def last_key(self) -> tuple[int, str, str] | None:
        """Delivery key of the latest processed bar (restart: where to resume)."""
        return self._last_key

    def _set_observed(self, observed: bool, ts_ms: int) -> None:
        if observed == self._observed:
            return
        self._observed = observed
        for book in self.books.values():
            book.observed = observed
        self._append(
            SimEventType.OBSERVATION_CHANGED,
            ts_ms,
            RUN_AGGREGATE_ID,
            {"observed": observed, "reason": "live" if observed else "downtime_backfill"},
        )

    def _start(self, ts_ms: int) -> None:
        self._started = True
        self._append(
            SimEventType.RUN_STARTED,
            ts_ms,
            RUN_AGGREGATE_ID,
            {
                "engine_version": SIM_ENGINE_VERSION,
                "decision_latency_ms": self.decision_latency_ms,
                "books": [b.describe() for b in self.books.values()],
            },
        )
        for book in self.books.values():
            book.open_capital(ts_ms)
        self._drain_books()

    def _flush_minute(self) -> None:
        if self._minute is None:
            return
        close_ms = self._minute + MS_PER_MINUTE - 1
        for book in self.books.values():
            book.mark(close_ms)
        self._minute = None

    def step(self, bar: Bar, *, observed: bool = True, not_before_ms: int | None = None) -> None:
        """Process one closed 1m bar.

        `not_before_ms` (live sessions): the wall time the decision on this bar actually became
        possible (the bar's release). Requests from this bar then execute no earlier than the first
        1m open at or after it, on top of `decision_latency_ms` -- a live decision never fills at
        a price that existed before it was made. Journaled, so replays are deterministic.
        """
        if self._finished:
            raise ClockError("simulator already finished")
        if bar.interval is not Interval.M1:
            raise ClockError("the simulator consumes closed 1m bars only")
        key = order_key(bar)
        if self._last_key is not None and key <= self._last_key:
            raise ClockError(f"bar out of order or duplicated: {key} after {self._last_key}")
        self._last_key = key
        if not self._started:
            self._start(bar.open_time_ms)
        if self._minute is not None and bar.open_time_ms != self._minute:
            self._flush_minute()
        self._minute = bar.open_time_ms
        self._last_close_ms = bar.close_time_ms
        self._set_observed(observed, bar.open_time_ms)

        sym = (bar.market, bar.symbol)
        prev = self._last_open.get(sym)
        if prev is not None and bar.open_time_ms - prev > MS_PER_MINUTE:
            self._append(
                SimEventType.BAR_GAP,
                bar.open_time_ms,
                RUN_AGGREGATE_ID,
                {
                    "market": str(bar.market),
                    "symbol": bar.symbol,
                    "after_open_ms": prev,
                    "next_open_ms": bar.open_time_ms,
                    "missing_bars": (bar.open_time_ms - prev) // MS_PER_MINUTE - 1,
                },
            )
        self._last_open[sym] = bar.open_time_ms

        for book in self.books.values():
            book.on_bar(bar)
        self._drain_books()
        if not observed:
            return  # unobserved minute: the signal source never sees it (no entries)

        try:
            requests = list(self.source.on_bar(bar, self._view))
        except Exception as exc:  # fail closed: a broken source produces no orders for this bar
            self._append(
                SimEventType.SOURCE_ERROR,
                bar.close_time_ms,
                RUN_AGGREGATE_ID,
                {"error": type(exc).__name__, "bar_open_ms": bar.open_time_ms},
            )
            requests = []
        decision = bar.close_time_ms
        earliest = earliest_execution_ms(decision, self.decision_latency_ms)
        if not_before_ms is not None:
            earliest = max(earliest, -(-not_before_ms // MS_PER_MINUTE) * MS_PER_MINUTE)
        for req in requests:
            self._route(req, decision, earliest)
        self._drain_books()

    def _route(self, req: Request, decision: int, earliest: int) -> None:
        valid = isinstance(req, EntryRequest | ExitRequest)
        targets = list(self.books) if not valid or req.books is None else list(req.books)
        unknown = [t for t in targets if t not in self.books]
        if unknown or not valid:
            self._append(
                SimEventType.REQUEST_REJECTED,
                decision,
                RUN_AGGREGATE_ID,
                {
                    "request_id": getattr(req, "request_id", None),
                    "reason": "invalid_request" if not valid else "unknown_book",
                },
            )
            return
        for name in targets:
            book = self.books[name]
            if isinstance(req, EntryRequest):
                book.submit(req, decision, earliest)
            else:
                book.request_exit(req.market, req.symbol, req.request_id, decision, earliest)

    def apply_funding(
        self, book: str, market: Market, symbol: str, ts_ms: int, amount: Decimal
    ) -> None:
        """Funding cash flow at a funding timestamp (interface; rates are the caller's concern)."""
        self.books[book].apply_funding(market, symbol, ts_ms, amount)
        self._drain_books()

    def apply_funding_rate(
        self, book: str, market: Market, symbol: str, ts_ms: int, rate: Decimal, mark_price: Decimal
    ) -> Decimal:
        """Funding from a rate (longs pay when rate > 0); returns the signed amount posted."""
        amount = self.books[book].apply_funding_rate(market, symbol, ts_ms, rate, mark_price)
        self._drain_books()
        return amount

    def halt(self, book: str, ts_ms: int, reason: str) -> None:
        """Halt a book (manual or external kill switch); see `Book.halt`."""
        self.books[book].halt(ts_ms, reason)
        self._drain_books()

    def reset_halt(
        self,
        book: str,
        ts_ms: int,
        operator: str,
        note: str = "",
        requested_at_ms: int | None = None,
    ) -> None:
        """Manual reset of a halted book (logged as an event)."""
        self.books[book].reset_halt(ts_ms, operator, note, requested_at_ms)
        self._drain_books()

    def finish(self) -> SimResult:
        if not self._finished:
            self._flush_minute()
            end = self._last_close_ms if self._last_close_ms is not None else 0
            if not self._started:
                self._start(end)
            for book in self.books.values():
                book.finish(end)
            self._drain_books()
            summaries = {name: book.summary() for name, book in self.books.items()}
            self._append(
                SimEventType.RUN_COMPLETED,
                end,
                RUN_AGGREGATE_ID,
                {"books": [summaries[n] for n in self.books]},
            )
            self._finished = True
        return self.result()

    def result(self) -> SimResult:
        mode = self.clock.mode
        return SimResult(
            mode=mode,
            provenance=MODE_PROVENANCE[mode],
            events=self.events,
            fingerprint=fingerprint(self._events),
            ledger_hashes={name: ledger_hash(book) for name, book in self.books.items()},
            summaries={name: book.summary() for name, book in self.books.items()},
        )


def ledger_hash(book: Book) -> str:
    """SHA-256 over the canonical JSON of a book's ledger entries (paper-ledger parity hash)."""
    entries = [tx.to_payload() for tx in book.ledger.entries]
    return hashlib.sha256(canonical_json(entries).encode("utf-8")).hexdigest()
