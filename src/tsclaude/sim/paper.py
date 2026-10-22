"""Paper trading on live bars through the one simulator (ADR-0005, SIMULATION §1, §5-§7).

`PaperSession` is the deterministic core shared by live sessions, restarts and replays:

- books ``baseline`` (every CONFIRMED signal that passes the risk engine; drawdown kill switch),
  ``shadow`` (the same signals without the risk gate: hypothetical outcomes of vetoed signals) and
  ``benchmark`` (equal-weight buy-and-hold of the spot universe, bought at the first observed
  open after each symbol's first observed bar);
- the risk engine as the baseline book's hook, fed with market views by a provider (live: the
  `MarketStateTracker`; replay: the recorded views);
- the signal lifecycle, driven by strategy *intents* (`SignalRecord` / `Confirm` /
  `Invalidate`) and by the baseline book's simulator events.

Every state change goes into the journal as one atomic **batch** per step (`sim.journal`): the input
entry (bar or funding or manual reset, plus the intents and risk views consumed) followed by every
simulator and lifecycle event the step produced. `replay` re-runs the inputs of a journal through
a `HistoricalClock` and verifies that each batch is reproduced exactly;
`PaperTradingService.restore` uses it on restart, then resolves the downtime over backfilled
bars (no entries for unobserved minutes) and resumes live.

Fail closed: a step that raises, or a journal append that fails, puts the service in ``FAULTED``
(no further bars processed, so no entries) until it is restored from its journal.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from functools import partial
from typing import Any, Protocol

from tsclaude.contracts.events import NewEvent
from tsclaude.contracts.market import Bar, BarSource, Market
from tsclaude.contracts.signals import EntryRuleKind, RunMode, Side, SignalRecord, SignalStatus
from tsclaude.contracts.timeutil import MS_PER_DAY, MS_PER_MINUTE
from tsclaude.marketdata.bars import BarClosed
from tsclaude.marketdata.models import MarketSnapshot
from tsclaude.risk.config import RiskConfig
from tsclaude.risk.engine import RiskEngine
from tsclaude.risk.hook import EngineRiskHook, RecordedViews, ViewProvider, tracker_provider
from tsclaude.risk.market import MarketStateTracker
from tsclaude.signals.lifecycle import (
    PAPER_STATUS_BOOK,
    SHADOW_BOOK,
    LifecycleError,
    SignalLifecycle,
)
from tsclaude.sim.book import Book, BookConfig, allow_all
from tsclaude.sim.clock import Clock, HistoricalClock, order_key
from tsclaude.sim.costs import CostModel
from tsclaude.sim.engine import Simulator, SimView, ledger_hash
from tsclaude.sim.journal import (
    SESSION_AGGREGATE_ID,
    SIM_AGGREGATE,
    STEP_TYPE,
    Batch,
    EventStoreJournal,
    Journal,
    JournalEntry,
)
from tsclaude.sim.live import DEFAULT_SETTLE_MS, LiveClock
from tsclaude.sim.money import ONE, dstr, floor_to
from tsclaude.sim.orders import EntryRequest, ExitRequest, InstrumentSpec
from tsclaude.store.eventstore import EventStore

BASELINE = PAPER_STATUS_BOOK
SHADOW = SHADOW_BOOK
BENCHMARK = "benchmark"
PAPER_VERSION = "paper-v1"


# ---------------------------------------------------------------- strategy intents
@dataclass(frozen=True, slots=True)
class Confirm:
    """WATCH -> CONFIRMED: the entry rule of an existing watch signal fired at this bar."""

    signal_id: str


@dataclass(frozen=True, slots=True)
class Invalidate:
    """The signal's invalidation rule hit: cancel its entry, or exit its position at next open."""

    signal_id: str
    reason: str = "invalidation"


type Intent = SignalRecord | Confirm | Invalidate


class LiveStrategy(Protocol):
    """Called once per closed, *observed* 1m bar, after the books processed it."""

    def on_bar(self, bar: Bar, view: SimView) -> Sequence[Intent]: ...


def intent_payload(intent: Intent) -> dict[str, Any]:
    if isinstance(intent, SignalRecord):
        return {"kind": "signal", "record": intent.model_dump(mode="json")}
    if isinstance(intent, Confirm):
        return {"kind": "confirm", "signal_id": intent.signal_id}
    if isinstance(intent, Invalidate):
        return {"kind": "invalidate", "signal_id": intent.signal_id, "reason": intent.reason}
    raise TypeError(f"not a strategy intent: {type(intent).__name__}")


def intent_from_payload(p: Mapping[str, Any]) -> Intent:
    kind = p["kind"]
    if kind == "signal":
        record: SignalRecord = SignalRecord.model_validate(p["record"])
        return record
    if kind == "confirm":
        return Confirm(str(p["signal_id"]))
    if kind == "invalidate":
        return Invalidate(str(p["signal_id"]), str(p["reason"]))
    raise ValueError(f"unknown intent kind {kind!r}")


# ---------------------------------------------------------------- configuration
@dataclass(frozen=True)
class PaperConfig:
    instruments: tuple[InstrumentSpec, ...]
    starting_equity: Decimal = Decimal(10_000)
    cost_model: CostModel = field(default_factory=CostModel)
    risk: RiskConfig = field(default_factory=RiskConfig)
    decision_latency_ms: int = 0
    shadow: bool = True
    benchmark: bool = True

    def __post_init__(self) -> None:
        if not self.instruments:
            raise ValueError("paper trading needs instruments")

    @property
    def spot_instruments(self) -> tuple[InstrumentSpec, ...]:
        return tuple(s for s in self.instruments if s.market is Market.SPOT)

    def baseline_config(self) -> BookConfig:
        return BookConfig(risk_per_trade=self.risk.max_risk_per_trade)

    def benchmark_config(self) -> BookConfig:
        n = len(self.spot_instruments)
        return BookConfig(
            risk_per_trade=Decimal("0.99"),
            max_notional_fraction=floor_to(ONE / n, Decimal("0.00000001")),
        )

    def describe(self) -> dict[str, Any]:
        return {
            "paper_version": PAPER_VERSION,
            "starting_equity": dstr(self.starting_equity),
            "decision_latency_ms": self.decision_latency_ms,
            "risk_config_version": self.risk.version,
            "risk_config_hash": self.risk.config_hash,
            "costs": self.cost_model.describe(),
            "books": [
                BASELINE,
                *([SHADOW] if self.shadow else []),
                *([BENCHMARK] if self.benchmark and self.spot_instruments else []),
            ],
            "instruments": [s.to_payload() for s in self.instruments],
        }


class RestoreError(RuntimeError):
    """A journal could not be replayed into exactly the recorded state (fail closed)."""


# ---------------------------------------------------------------- the source adapter
class _PaperSource:
    """Simulator signal source: strategy intents (live) or recorded intents (replay), translated
    into lifecycle events and book requests; plus the benchmark's buy-and-hold entries."""

    def __init__(self, session: PaperSession) -> None:
        self.session = session
        self.strategy: LiveStrategy | None = None
        self._recorded: list[dict[str, Any]] | None = None
        self._recorded_errors: list[str] = []
        self.intents: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.lifecycle_events: list[NewEvent] = []
        self._bench_bought: set[tuple[Market, str]] = set()

    def begin(
        self, recorded: list[dict[str, Any]] | None, recorded_errors: Sequence[str] = ()
    ) -> None:
        """Start a step. Replay passes the recorded intents and the recorded strategy errors (an
        exception of the live strategy is an input fact; intent rejections are recomputed)."""
        self._recorded = recorded
        self._recorded_errors = [e for e in recorded_errors if e.startswith("strategy_error:")]
        self.intents, self.errors, self.lifecycle_events = [], [], []

    def _strategy_intents(self, bar: Bar, view: SimView) -> list[Intent]:
        if self._recorded is not None:
            self.errors.extend(self._recorded_errors)
            return [intent_from_payload(p) for p in self._recorded]
        if self.strategy is None:
            return []
        try:
            return list(self.strategy.on_bar(bar, view))
        except Exception as exc:  # fail closed: no intents from a broken strategy on this bar
            self.errors.append(f"strategy_error:{type(exc).__name__}")
            return []

    def on_bar(self, bar: Bar, view: SimView) -> Sequence[EntryRequest | ExitRequest]:
        lc = self.session.lifecycle
        ts = bar.close_time_ms
        self.lifecycle_events.extend(lc.expire_due(ts, market=str(bar.market), symbol=bar.symbol))
        requests: list[EntryRequest | ExitRequest] = []
        for intent in self._strategy_intents(bar, view):
            try:
                self.intents.append(intent_payload(intent))
                requests.extend(self._translate(intent, bar))
            except (LifecycleError, ValueError, TypeError) as exc:
                self.errors.append(f"intent_rejected:{type(exc).__name__}:{exc}")
        requests.extend(self._benchmark(bar))
        return requests

    def _translate(self, intent: Intent, bar: Bar) -> list[EntryRequest | ExitRequest]:
        lc = self.session.lifecycle
        ts = bar.close_time_ms
        books = self.session.signal_books
        if isinstance(intent, SignalRecord):
            if intent.decision_time_ms != ts:
                raise ValueError(f"decision_time {intent.decision_time_ms} != bar close {ts}")
            if (intent.market, intent.symbol) != (bar.market, bar.symbol):
                raise ValueError("signal for another instrument than the bar")
            self.lifecycle_events.append(lc.create(intent, ts))
            if intent.stage is SignalStatus.CONFIRMED:
                return [EntryRequest.from_signal(intent, books=books)]
            return []
        state = lc.get(intent.signal_id)
        if state is None:
            raise LifecycleError(f"unknown signal {intent.signal_id}")
        record = SignalRecord.model_validate(state.record)
        if isinstance(intent, Confirm):
            self.lifecycle_events.append(
                lc.transition(intent.signal_id, SignalStatus.CONFIRMED, ts, "entry_rule_met")
            )
            return [EntryRequest.from_signal(record, books=books)]
        # Invalidate: only books where this signal holds the order / position are told to exit
        holders = tuple(
            name
            for name in books
            if self.session.sim.books[name].holder(record.market, record.symbol) == intent.signal_id
        )
        if state.status is not SignalStatus.OPEN:
            self.lifecycle_events.append(
                lc.transition(intent.signal_id, SignalStatus.INVALIDATED, ts, intent.reason)
            )
        if not holders:
            return []
        return [
            ExitRequest(f"invalidate:{intent.signal_id}", record.market, record.symbol, holders)
        ]

    def _benchmark(self, bar: Bar) -> list[EntryRequest]:
        if not self.session.has_benchmark or bar.market is not Market.SPOT:
            return []
        key = (bar.market, bar.symbol)
        if key in self._bench_bought:
            return []
        spec = self.session.sim.books[BENCHMARK].instruments.get(key)
        if spec is None:
            return []
        self._bench_bought.add(key)
        return [
            EntryRequest(
                f"benchmark:{bar.symbol}",
                Market.SPOT,
                bar.symbol,
                Side.LONG,
                EntryRuleKind.MARKET_NEXT_OPEN,
                stop_price=spec.tick_size,  # buy-and-hold: no stop within reach
                valid_until_ms=bar.close_time_ms + MS_PER_DAY,
                books=(BENCHMARK,),
            )
        ]


# ---------------------------------------------------------------- the session
class PaperSession:
    def __init__(
        self,
        config: PaperConfig,
        provider: ViewProvider,
        clock: Clock,
        strategy: LiveStrategy | None = None,
    ) -> None:
        self.config = config
        self.engine = RiskEngine(config.risk, config.cost_model)
        self.hook = EngineRiskHook(self.engine, provider)
        books = [
            Book(
                BASELINE,
                config.starting_equity,
                config.cost_model,
                self.hook,
                instruments=config.instruments,
                config=config.baseline_config(),
                kill_switch_drawdown=config.risk.drawdown_kill_switch,
            )
        ]
        if config.shadow:
            books.append(
                Book(
                    SHADOW,
                    config.starting_equity,
                    config.cost_model,
                    allow_all,
                    instruments=config.instruments,
                    config=config.baseline_config(),
                )
            )
        self.has_benchmark = config.benchmark and bool(config.spot_instruments)
        if self.has_benchmark:
            books.append(
                Book(
                    BENCHMARK,
                    config.starting_equity,
                    config.cost_model,
                    allow_all,
                    instruments=config.spot_instruments,
                    config=config.benchmark_config(),
                )
            )
        self.signal_books: tuple[str, ...] = (BASELINE, SHADOW) if config.shadow else (BASELINE,)
        self.lifecycle = SignalLifecycle()
        self.source = _PaperSource(self)
        self.source.strategy = strategy
        self.sim = Simulator(
            clock, books, self.source, decision_latency_ms=config.decision_latency_ms
        )
        self._cursor = 0
        self.batches = 0

    # ------------------------------------------------------------ wiring
    def go_live(self, provider: ViewProvider, clock: Clock, strategy: LiveStrategy | None) -> None:
        self.hook.provider = provider
        self.sim.clock = clock
        self.source.strategy = strategy

    @property
    def last_minute_ms(self) -> int | None:
        key = self.sim.last_key
        return None if key is None else key[0]

    # ------------------------------------------------------------ steps
    def _collect(self, head: dict[str, Any], ts_ms: int, extra: Sequence[NewEvent] = ()) -> Batch:
        new = self.sim.events[self._cursor :]
        self._cursor = len(self.sim.events)
        lifecycle = [*self.source.lifecycle_events, *self.lifecycle.transitions_from_sim(new)]
        lifecycle.extend(extra)
        if self.batches == 0:
            head = {**head, "session": self.config.describe()}
        head = {
            **head,
            "intents": list(self.source.intents),
            "intent_errors": list(self.source.errors),
            "risk": self.hook.drain(),
        }
        self.source.begin(None)
        batch: Batch = [JournalEntry(STEP_TYPE, SIM_AGGREGATE, SESSION_AGGREGATE_ID, ts_ms, head)]
        for e in new:
            batch.append(
                JournalEntry(
                    e["type"],
                    SIM_AGGREGATE,
                    e["aggregate_id"],
                    e["ts_ms"],
                    {**e["payload"], "sim_seq": e["seq"]},
                )
            )
        batch.extend(JournalEntry.from_event(ev) for ev in lifecycle)
        self.batches += 1
        return batch

    def step_bar(
        self,
        bar: Bar,
        *,
        observed: bool,
        recorded: list[dict[str, Any]] | None = None,
        recorded_errors: Sequence[str] = (),
        released_at_ms: int | None = None,
    ) -> Batch:
        """`released_at_ms`: wall time the live clock released the bar (None if unknown); with
        the bar close it gives the decision latency, journaled as ``release_latency_ms``."""
        self.source.begin(recorded, recorded_errors)
        self.sim.step(bar, observed=observed)
        extra: list[NewEvent] = []
        if not observed:  # WATCH signals whose window passed while we were not watching
            extra = self.lifecycle.expire_due(
                bar.close_time_ms, market=str(bar.market), symbol=bar.symbol, downtime=True
            )
        head = {
            "kind": "bar",
            "bar": bar.model_dump(mode="json"),
            "observed": observed,
            "released_at_ms": released_at_ms,
            "release_latency_ms": (
                None if released_at_ms is None else released_at_ms - bar.close_time_ms
            ),
        }
        return self._collect(head, bar.close_time_ms, extra)

    def funding(
        self, market: Market, symbol: str, ts_ms: int, rate: Decimal, mark_price: Decimal
    ) -> Batch:
        """Funding at a funding timestamp for every book holding the USD-M position."""
        self.source.begin(None)
        applied: list[str] = []
        skipped: list[str] = []
        for name, book in self.sim.books.items():
            if book.position_qty(market, symbol) == 0:
                continue
            try:
                self.sim.apply_funding_rate(name, market, symbol, ts_ms, rate, mark_price)
                applied.append(name)
            except ValueError as exc:
                skipped.append(f"{name}:{exc}")
        head = {
            "kind": "funding",
            "market": str(market),
            "symbol": symbol,
            "funding_ts_ms": ts_ms,
            "rate": dstr(rate),
            "mark_price": dstr(mark_price),
            "applied": applied,
            "skipped": skipped,
        }
        return self._collect(head, ts_ms)

    def reset_halt(self, book: str, ts_ms: int, operator: str, note: str = "") -> Batch:
        self.source.begin(None)
        self.sim.reset_halt(book, ts_ms, operator, note)
        head = {"kind": "reset", "book": book, "reset_ts_ms": ts_ms, "operator": operator}
        head["note"] = note
        return self._collect(head, ts_ms)

    # ------------------------------------------------------------ results
    def ledger_hashes(self) -> dict[str, str]:
        return {name: ledger_hash(b) for name, b in self.sim.books.items()}

    def fingerprint(self) -> str:
        return self.sim.result().fingerprint


# ---------------------------------------------------------------- replay / restore
def _bars_of(batches: Sequence[Batch]) -> list[Bar]:
    return [
        Bar.model_validate(b[0].payload["bar"]) for b in batches if b[0].payload["kind"] == "bar"
    ]


def replay(
    config: PaperConfig,
    batches: Sequence[Batch],
    *,
    mode: RunMode = RunMode.REPLAY,
    verify: bool = True,
) -> PaperSession:
    """Re-run a journal's inputs through a `HistoricalClock`; verify every batch is reproduced.

    Strategy intents and risk market views come from the journal; everything else -- fills,
    ledger, risk decisions, lifecycle -- is recomputed.
    """
    views = RecordedViews()
    clock = HistoricalClock(_bars_of(batches), mode=mode)
    session = PaperSession(config, views, clock)
    bars = iter(clock)
    for i, batch in enumerate(batches):
        head = batch[0].payload
        views.extend(head.get("risk", []))
        kind = head["kind"]
        if kind == "bar":
            bar = next(bars)
            if bar.model_dump(mode="json") != head["bar"]:
                raise RestoreError(f"batch {i}: clock order differs from the journal")
            out = session.step_bar(
                bar,
                observed=bool(head["observed"]),
                recorded=head["intents"],
                recorded_errors=head["intent_errors"],
                released_at_ms=head.get("released_at_ms"),
            )
        elif kind == "funding":
            out = session.funding(
                Market(head["market"]),
                str(head["symbol"]),
                int(head["funding_ts_ms"]),
                Decimal(head["rate"]),
                Decimal(head["mark_price"]),
            )
        elif kind == "reset":
            out = session.reset_halt(
                str(head["book"]), int(head["reset_ts_ms"]), str(head["operator"]), head["note"]
            )
        else:
            raise RestoreError(f"batch {i}: unknown input kind {kind!r}")
        if views.remaining:
            raise RestoreError(f"batch {i}: {views.remaining} recorded risk checks not replayed")
        if verify and [e.canonical() for e in out] != [e.canonical() for e in batch]:
            raise RestoreError(f"batch {i} ({kind}) diverged from the journal")
    return session


# ---------------------------------------------------------------- the facade
class ServiceState(StrEnum):
    RUNNING = "RUNNING"
    FAULTED = "FAULTED"  # a step or a journal write failed: nothing is processed until restore


class PaperTradingService:
    """Facade for the Platform task: feed it market-data updates and snapshots, poll it."""

    def __init__(
        self,
        config: PaperConfig,
        journal: Journal,
        *,
        strategy: LiveStrategy | None = None,
        tracker: MarketStateTracker | None = None,
        settle_ms: int = DEFAULT_SETTLE_MS,
        observed_sources: frozenset[BarSource] = frozenset({BarSource.WS_LIVE}),
        session: PaperSession | None = None,
        start_ms: int | None = None,
    ) -> None:
        self.config = config
        self.journal = journal
        self.observed_sources = observed_sources
        self.tracker = tracker or MarketStateTracker(
            median_window_ms=config.risk.spread.median_window_ms,
            min_samples=config.risk.spread.min_samples,
        )
        self.clock = LiveClock(
            [s.key for s in config.instruments], settle_ms=settle_ms, start_ms=start_ms
        )
        provider = tracker_provider(self.tracker)
        if session is None:
            session = PaperSession(config, provider, self.clock, strategy)
        else:
            session.go_live(provider, self.clock, strategy)
        self.session = session
        self.state = ServiceState.RUNNING
        self.fault: str | None = None

    @classmethod
    def open(cls, config: PaperConfig, store: EventStore, **kw: Any) -> PaperTradingService:
        """The production path: journal on the hash-chained event store, restored from it."""
        return cls.restore(config, EventStoreJournal(store), **kw)

    def latency_ms(self) -> list[int]:
        """Observed release latencies (bar close -> processed) of this session's live steps."""
        out: list[int] = []
        for batch in self.journal.batches():
            head = batch[0].payload
            if (
                head.get("kind") == "bar"
                and head.get("observed")
                and head.get("release_latency_ms") is not None
            ):
                out.append(int(head["release_latency_ms"]))
        return out

    @classmethod
    def restore(cls, config: PaperConfig, journal: Journal, **kw: Any) -> PaperTradingService:
        """Rebuild the session from its journal (verified replay), ready to resume."""
        batches = journal.batches()
        session = replay(config, batches, mode=RunMode.LIVE)
        last = session.last_minute_ms
        done = {
            (Market(b[0].payload["bar"]["market"]), str(b[0].payload["bar"]["symbol"]))
            for b in batches
            if b[0].payload["kind"] == "bar" and b[0].payload["bar"]["open_time_ms"] == last
        }
        svc = cls(config, journal, session=session, start_ms=last, **kw)
        if last is not None:
            svc.clock = LiveClock(
                [s.key for s in config.instruments],
                settle_ms=svc.clock.settle_ms,
                start_ms=last,
                processed=done,
            )
            session.sim.clock = svc.clock
        return svc

    # ------------------------------------------------------------ inputs
    def on_update(self, update: object) -> None:
        """A `Subscription` item: closed bars go to the clock and the tracker; events are ignored
        (stream health reaches the risk engine through snapshots)."""
        if isinstance(update, BarClosed):
            self.tracker.observe_bar(update.bar)
            self.clock.offer(update.bar, replaced=update.replaced)

    def on_snapshot(self, snapshot: MarketSnapshot) -> None:
        self.tracker.observe_snapshot(snapshot)

    def _observed(self, bar: Bar) -> bool:
        return bar.source in self.observed_sources

    def _process(self, step: Callable[[], Batch]) -> bool:
        if self.state is ServiceState.FAULTED:
            return False
        try:
            batch = step()
        except Exception as exc:  # the in-memory session can no longer be trusted
            self._set_fault(f"step_failed:{type(exc).__name__}:{exc}")
            return False
        try:
            self.journal.append(batch)
        except Exception as exc:  # state not durable: stop before anything else happens
            self._set_fault(f"journal_failed:{type(exc).__name__}:{exc}")
            return False
        return True

    def _set_fault(self, reason: str) -> None:
        self.state = ServiceState.FAULTED
        self.fault = reason

    def poll(self, now_ms: int) -> int:
        """Release and process every bar whose minute is complete or due; returns the count."""
        if self.state is ServiceState.FAULTED:
            return 0
        done = 0
        for bar in self.clock.release(now_ms):
            step = partial(
                self.session.step_bar, bar, observed=self._observed(bar), released_at_ms=now_ms
            )
            if not self._process(step):
                break
            done += 1
        return done

    def resume_after_downtime(self, bars: Sequence[Bar], now_ms: int | None = None) -> int:
        """Resolve open positions over backfilled 1m bars of the downtime (SIMULATION §7).

        Only bars after the last processed one are used, in clock order, all **unobserved**: no
        signal is computed and no entry fills on them; WATCH/CONFIRMED signals whose validity
        passed become EXPIRED (downtime). Call before feeding live bars of the new session.
        """
        last = self.session.sim.last_key
        todo = sorted(
            (b for b in bars if last is None or order_key(b) > last),
            key=order_key,
        )
        done = 0
        for bar in todo:
            step = partial(self.session.step_bar, bar, observed=False, released_at_ms=now_ms)
            if not self._process(step):
                break
            done += 1
        if todo:
            self.clock.skip_to(todo[-1].open_time_ms + MS_PER_MINUTE)
        return done

    def apply_funding_rate(
        self, market: Market, symbol: str, ts_ms: int, rate: Decimal, mark_price: Decimal
    ) -> bool:
        """Funding for every book holding the position; books where the timestamp is not valid
        (see `Book.apply_funding`) are skipped and listed in the journal entry."""
        return self._process(lambda: self.session.funding(market, symbol, ts_ms, rate, mark_price))

    def reset_halt(self, book: str, operator: str, note: str = "") -> bool:
        """Manual kill-switch reset, stamped at the last processed close.

        Operator errors (unknown book, book not halted, no operator name) raise `ValueError`
        without touching the session; only a failure while applying the reset faults it.
        """
        target = self.session.sim.books.get(book)
        if target is None:
            raise ValueError(f"unknown book {book!r}")
        if target.halted is None:
            raise ValueError(f"book {book} is not halted")
        if not operator:
            raise ValueError("a manual reset needs the operator's name")
        last = self.session.last_minute_ms
        ts = 0 if last is None else last + MS_PER_MINUTE - 1
        return self._process(lambda: self.session.reset_halt(book, ts, operator, note))

    # ------------------------------------------------------------ views
    def ledger_hashes(self) -> dict[str, str]:
        return self.session.ledger_hashes()

    def status(self) -> dict[str, Any]:
        books = {name: b.summary() for name, b in self.session.sim.books.items()}
        for name, b in self.session.sim.books.items():
            books[name]["halted"] = b.halted
        return {
            "state": str(self.state),
            "fault": self.fault,
            "mode": str(self.session.sim.clock.mode),
            "batches": self.session.batches,
            "last_minute_ms": self.session.last_minute_ms,
            "clock": vars(self.clock.stats).copy(),
            "books": books,
            "ledger_hashes": self.ledger_hashes(),
        }

    # ------------------------------------------------------------ async driver
    async def run(
        self,
        subscription: Any,
        snapshot: Callable[[], MarketSnapshot],
        now_ms: Callable[[], int],
        stop: asyncio.Event,
        *,
        poll_s: float = 1.0,
    ) -> None:
        """Consume a market-data `Subscription` until `stop` is set (or the service faults)."""
        while not stop.is_set() and self.state is ServiceState.RUNNING:
            update = await subscription.get(timeout=poll_s)
            if update is not None:
                self.on_update(update)
            self.on_snapshot(snapshot())
            self.poll(now_ms())
