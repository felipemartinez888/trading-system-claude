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
simulator and lifecycle event the step produced. `replay` deterministically recomputes a session
from its journaled inputs (bars, strategy intents, risk market views, release times, funding,
resets) through a `HistoricalClock` and verifies that each batch is reproduced exactly. This
is replay parity in the sense of SIMULATION §1 for a *recorded* session: it does not re-run the
strategy and does not compare against bars from the lake;
`PaperTradingService.restore` uses it on restart, then resolves the downtime over backfilled
bars (no entries for unobserved minutes) and resumes live. The input entry of the last bar the
live clock released for a minute carries ``minute_closed: true`` (in the same atomic batch), so a
restore can tell an instrument the clock released the minute without (a gap the session passed)
from one whose bar was not processed yet. A downtime backfill marks the last bar of each minute
it resolves the same way, since the clock then skips past those minutes (M3-PB.4). Journals
without the mark restore as before, treating every key after the last processed one as possible
downtime.

Sessions and configuration changes: a journal belongs to one session (`session_id`, the aggregate
id of its input entries) and records the configuration hash in its first batch. A restore with a
different `risk.yaml` fails (`RestoreError`: recorded decisions would be recomputed differently).
To trade under a new configuration, start a **new session** in the same store --
``PaperTradingService.open(config, store, session_id="paper-<date>")`` -- after flattening or
abandoning the old one; the old session stays in the hash chain, read-only.

Fail closed: a step that raises, or a journal append that fails, puts the service in ``FAULTED``
(no further bars processed, so no entries) until it is restored from its journal.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from functools import partial
from typing import Any, Protocol

from tsclaude.contracts.events import NewEvent, canonical_json
from tsclaude.contracts.market import Bar, BarSource, Market
from tsclaude.contracts.signals import EntryRuleKind, RunMode, Side, SignalRecord, SignalStatus
from tsclaude.contracts.timeutil import MS_PER_DAY, MS_PER_MINUTE
from tsclaude.marketdata.bars import BarClosed
from tsclaude.marketdata.models import MarketSnapshot
from tsclaude.quality.clock import continuity_tolerance_ms, drift_allowance_ms
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


class ConfigurationMismatch(RestoreError):
    """The journal was recorded with another paper configuration than the one restoring it.

    Its first batch records the session's `PaperConfig.describe()`. A different current
    configuration (for example ``decision_latency_ms`` 0 -> 3000 after `configs/backtest.yaml`
    changed) can never reproduce the recorded batches. The restore is refused with the
    differing fields named, instead of the first diverging batch (operational-readiness review
    R-04). Still a `RestoreError`: callers fail closed exactly as before."""

    def __init__(self, differences: Sequence[str]) -> None:
        self.differences = tuple(differences)
        shown = "; ".join(self.differences[:6])
        more = len(self.differences) - 6
        super().__init__(
            "the session was recorded with a different paper configuration "
            f"({shown}{f'; {more} more' if more > 0 else ''})"
        )


_ABSENT = "<absent>"
_SHOWN_CHARS = 60


def _flat(obj: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(obj, Mapping):
        out: dict[str, Any] = {}
        for key in sorted(obj):
            out.update(_flat(obj[key], f"{prefix}{key}."))
        return out
    if (
        isinstance(obj, list)
        and obj
        and all(isinstance(e, Mapping) and "market" in e and "symbol" in e for e in obj)
    ):  # instruments: keyed by market:symbol, so one added or dropped names itself
        keys = [f"{e['market']}:{e['symbol']}" for e in obj]
        if len(set(keys)) == len(keys):
            keyed: dict[str, Any] = {}
            for key, e in zip(keys, obj, strict=True):
                keyed.update(_flat(e, f"{prefix}{key}."))
            return keyed
    return {prefix.rstrip("."): obj}


def _show(value: Any) -> str:
    text = value if isinstance(value, str) else canonical_json(value)
    return text if len(text) <= _SHOWN_CHARS else text[: _SHOWN_CHARS - 3] + "..."


def configuration_differences(recorded: Any, current: Any) -> list[str]:
    """``key: recorded -> current`` for every differing leaf of two `describe()` payloads, in
    their journaled (canonical JSON) form; instruments are keyed by ``market:symbol`` and long
    values are shortened. Empty exactly when the canonical forms are equal: a difference of order
    or JSON type only is still reported."""
    if canonical_json(recorded) == canonical_json(current):
        return []
    a = _flat(json.loads(canonical_json(recorded)))
    b = _flat(json.loads(canonical_json(current)))
    out = []
    for key in sorted(set(a) | set(b)):
        va, vb = a.get(key, _ABSENT), b.get(key, _ABSENT)
        if canonical_json(va) != canonical_json(vb):
            out.append(f"{key}: {_show(va)} -> {_show(vb)}")
    return out or ["the recorded configuration differs in order or JSON type only"]


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
        session_id: str = SESSION_AGGREGATE_ID,
    ) -> None:
        self.config = config
        self.session_id = session_id
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
        #: every minute before this one was closed by the session (journaled ``minute_closed``
        #: marks: a live clock release, or a downtime backfill the clock skipped past); None if no
        #: step recorded a close, e.g. a journal written before the mark existed
        self.closed_through_ms: int | None = None

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
        batch: Batch = [JournalEntry(STEP_TYPE, SIM_AGGREGATE, self.session_id, ts_ms, head)]
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
        minute_closed: bool = False,
    ) -> Batch:
        """`released_at_ms`: wall time the live clock released the bar (None if unknown); with
        the bar close it gives the decision latency, journaled as ``release_latency_ms``.

        `minute_closed`: the session closed this bar's minute with it -- the live clock released
        it as the last one of its minute (complete, or past its deadline with the missing
        instruments a gap), or it is the last bar of a minute of a downtime backfill that the
        clock then skipped past (`resume_after_downtime`) -- so no other bar of that minute will
        be processed. Journaled as ``minute_closed: true`` only when set, so batches without it
        keep their recorded form."""
        if self.closed_through_ms is not None and bar.open_time_ms < self.closed_through_ms:
            raise ValueError(f"bar {order_key(bar)} is in a minute already closed")
        self.source.begin(recorded, recorded_errors)
        self.sim.step(bar, observed=observed, not_before_ms=released_at_ms)
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
        if minute_closed:
            head["minute_closed"] = True
            self.closed_through_ms = bar.open_time_ms + MS_PER_MINUTE
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

    def reset_halt(
        self,
        book: str,
        ts_ms: int,
        operator: str,
        note: str = "",
        requested_at_ms: int | None = None,
    ) -> Batch:
        """`ts_ms`: simulator time of the reset (last processed close); `requested_at_ms`: the
        operator's wall-clock time, recorded in the input entry and the reset event."""
        self.source.begin(None)
        self.sim.reset_halt(book, ts_ms, operator, note, requested_at_ms)
        head = {"kind": "reset", "book": book, "reset_ts_ms": ts_ms, "operator": operator}
        head["note"] = note
        head["requested_at_ms"] = requested_at_ms
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
    session_id: str | None = None,
) -> PaperSession:
    """Deterministic recomputation of a session from its journaled inputs, through a
    `HistoricalClock`; every batch must be reproduced exactly.

    Bars, strategy intents, risk market views, release times, funding and resets come from the
    journal; fills, ledger, risk decisions and lifecycle are recomputed. The strategy itself is
    not re-run and the bars are not re-read from the lake.
    """
    if verify and batches:
        recorded = batches[0][0].payload.get("session")
        if recorded is not None:
            differences = configuration_differences(recorded, config.describe())
            if differences:
                raise ConfigurationMismatch(differences)
    views = RecordedViews()
    clock = HistoricalClock(_bars_of(batches), mode=mode)
    if session_id is None:
        session_id = batches[0][0].aggregate_id if batches else SESSION_AGGREGATE_ID
    session = PaperSession(config, views, clock, session_id=session_id)
    bars = iter(clock)
    for i, batch in enumerate(batches):
        head = batch[0].payload
        views.extend(head.get("risk", []))
        kind = head["kind"]
        if kind == "bar":
            bar = next(bars)
            if bar.model_dump(mode="json") != head["bar"]:
                raise RestoreError(f"batch {i}: clock order differs from the journal")
            through = session.closed_through_ms
            if through is not None and bar.open_time_ms < through:
                raise RestoreError(f"batch {i}: bar after its minute was recorded closed")
            out = session.step_bar(
                bar,
                observed=bool(head["observed"]),
                recorded=head["intents"],
                recorded_errors=head["intent_errors"],
                released_at_ms=head.get("released_at_ms"),
                minute_closed=head.get("minute_closed") is True,
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
                str(head["book"]),
                int(head["reset_ts_ms"]),
                str(head["operator"]),
                head["note"],
                head.get("requested_at_ms"),
            )
        else:
            raise RestoreError(f"batch {i}: unknown input kind {kind!r}")
        if views.remaining:
            raise RestoreError(f"batch {i}: {views.remaining} recorded risk checks not replayed")
        if verify and [e.canonical() for e in out] != [e.canonical() for e in batch]:
            raise RestoreError(f"batch {i} ({kind}) diverged from the journal")
    return session


# ---------------------------------------------------------------- observation (P-1)
def clock_uncertainty_ms(max_rtt_ms: int) -> int:
    """An upper bound (ms) of the error of a `ClockMonitor` offset, from its round's largest round
    trip.

    A sample's server reading was taken between its local send and receive readings, so the true
    offset (server - local) exceeds the sample's estimate (server - midpoint) by less than half its
    round trip plus 1 ms (the three readings are whole, floored milliseconds). The round's offset
    is the median of its samples' estimates, rounded (0.5 ms): it is bounded by half the largest
    round trip of the round, not the median one. The reading of the instant being bounded adds
    less than 1 ms. In all, less than ``max_rtt_ms / 2 + 2.5``. Drift and clock discontinuities
    after the round are not included: `observation_bound` adds them."""
    if max_rtt_ms < 0:
        raise ValueError("max_rtt_ms must be >= 0")
    return (max_rtt_ms + 6) // 2


def observation_anchor_ms(
    local_now_ms: int, offset_ms: int, max_rtt_ms: int, *, allowance_ms: int = 0
) -> int:
    """The first minute open observed by a service that starts observing at `local_now_ms`.

    L-02 for the paper service: a minute counts as observed only if it lies entirely inside the
    monitoring interval, which starts when the runtime polls in real time. Bar opens are exchange
    time, so the start is converted with the measured offset and bounded above by its uncertainty
    plus `allowance_ms` (drift and wall-clock movement since the round: `observation_bound`; 0 for
    an exact clock); the anchor is the first minute open at or after that bound. A partly observed
    minute never counts; an anchor exactly at an open counts that minute."""
    if allowance_ms < 0:
        raise ValueError("allowance_ms must be >= 0")
    upper = local_now_ms + offset_ms + clock_uncertainty_ms(max_rtt_ms) + allowance_ms
    return -(-upper // MS_PER_MINUTE) * MS_PER_MINUTE


@dataclass(frozen=True, slots=True)
class ClockEvidence:
    """One clock round as a `MarketSnapshot` reports it (`clock_offset_ms`, `clock_max_rtt_ms`,
    `clock_continuity`, all from that round), measured when the snapshot was taken."""

    round_at_ms: int  # wall clock at the round's end
    offset_ms: int
    max_rtt_ms: int
    min_rtt_ms: int
    samples: int
    round_span_ms: int  # monotonic, first send to round end
    round_step_ms: int  # spread of wall - monotonic over the round's readings
    elapsed_ms: int  # monotonic, round end to the snapshot
    wall_elapsed_ms: int  # wall, round end to the snapshot


@dataclass(frozen=True, slots=True)
class ObservationVerdict:
    """The anchor a clock round gives, or why it gives none (`code` is stable, `problem` reads)."""

    anchor_ms: int | None
    code: str | None = None
    problem: str | None = None
    details: dict[str, int] = field(default_factory=dict)


def observation_bound(e: ClockEvidence, *, max_age_ms: int, skew_max_ms: int) -> ObservationVerdict:
    """The observation anchor for a snapshot's clock round, or fail closed (P-1, D-19; F-1, F-3).

    The bound, at the snapshot instant P (true time t is exchange time; W wall, M monotonic; every
    reading floored; r = `CLOCK_DRIFT_PPM`):
    - Assumed: each server stamp was taken while its request was in flight; during the round M
      runs within r of exchange time; from the round end R to the snapshot P one and the same
      clock, M or W, runs within r over the whole interval (the clock that satisfies it may not
      switch midway: M may stop in a suspend, or W may step or be slewed, but not one after the
      other); one exchange clock for spot and USD-M. r is an external assumption about the host's
      clocks: nothing here can verify it.
    - Not covered (residual risk NB-1): a wall step followed by a roughly compensating monotonic
      suspend (or the reverse) cancels out in the discontinuity check below and can give an anchor
      in a minute that had already started; so can anomalies of both clocks at once (a VM pause or
      live migration that freezes both, a slew shared by both). The checks below detect only a net
      wall/monotonic movement beyond the tolerance, a negative round trip and inconsistent evidence.
    - Round end R, in the monotonic frame (as `clock_uncertainty_ms` shows for the wall frame):
      some sample j has `phi(stamp_j) < median(x_i - mid_i(M)) + max_rtt(M) / 2 + 1`, with
      phi = t - M, and phi grows by at most `r / (1 - r)` per monotonic ms up to R.
    - Wall and monotonic readings of the round differ by `d_k = w_k - m_k`, spread S
      (`round_step_ms`): the monotonic median is at most the wall offset + 0.5 (rounding) + max d,
      the monotonic round trips at most the wall ones + S, and `m_R = w_R - d_R`. So
      `t(R) < w_R + offset + clock_uncertainty_ms(max_rtt) + 1.5 S + r / (1 - r) (span + 1)`.
    - R to P: with E the larger of the two elapsed times, the clock that ran within r over all of
      [R, P] gives `t(P) - t(R) < (E + 1) / (1 - r)`. A backward wall step or a monotonic pause
      can only make its own clock read less, and E takes the other one.
    - So `t(P) < w_R + E + offset + clock_uncertainty_ms(max_rtt) + 1 + ceil(1.5 S) +
      drift_allowance_ms(span + E + 2)`.

    No anchor when the evidence is inconsistent, a round trip is negative (the wall clock stepped
    back during a sample), the wall clock moved against the monotonic one beyond
    `continuity_tolerance_ms` during the round or since it (a step, a suspend, a fast slew), the
    round is older than `max_age_ms` on either clock, or the offset is beyond `skew_max_ms`."""
    if (
        e.samples < 1
        or e.min_rtt_ms > e.max_rtt_ms
        or e.round_span_ms < 0
        or e.round_step_ms < 0
        or e.elapsed_ms < 0
    ):
        return ObservationVerdict(
            None, "clock_evidence_inconsistent", "server clock evidence is inconsistent"
        )
    if e.min_rtt_ms < 0:
        return ObservationVerdict(
            None,
            "clock_rtt_negative",
            f"a server clock sample has a negative round trip ({e.min_rtt_ms} ms): "
            "the wall clock stepped back during the round",
        )
    tolerance = continuity_tolerance_ms(e.round_span_ms)
    if e.round_step_ms > tolerance:
        return ObservationVerdict(
            None,
            "clock_step_in_round",
            f"the wall clock moved {e.round_step_ms} ms against the monotonic clock during the "
            f"server clock round (tolerance {tolerance} ms)",
        )
    if e.wall_elapsed_ms < 0:
        return ObservationVerdict(
            None,
            "clock_round_future",
            f"server clock round {e.wall_elapsed_ms} ms old (0..{max_age_ms})",
        )
    moved = e.wall_elapsed_ms - e.elapsed_ms
    tolerance = continuity_tolerance_ms(e.elapsed_ms)
    if abs(moved) > tolerance:
        return ObservationVerdict(
            None,
            "clock_discontinuity",
            f"the wall clock moved {moved} ms against the monotonic clock since the server clock "
            f"round (tolerance {tolerance} ms): a step, a suspend or a fast slew",
        )
    age = max(e.elapsed_ms, e.wall_elapsed_ms)
    if age > max_age_ms:
        return ObservationVerdict(
            None, "clock_round_stale", f"server clock round {age} ms old (0..{max_age_ms})"
        )
    if abs(e.offset_ms) > skew_max_ms:
        return ObservationVerdict(
            None,
            "clock_offset_skew",
            f"server clock offset {e.offset_ms} ms beyond {skew_max_ms} ms",
        )
    elapsed = max(e.elapsed_ms, e.wall_elapsed_ms)
    drift = drift_allowance_ms(e.round_span_ms + elapsed + 2)
    step = (3 * e.round_step_ms + 1) // 2
    allowance = 1 + step + drift  # 1: the elapsed reading's resolution
    anchor = observation_anchor_ms(
        e.round_at_ms + elapsed, e.offset_ms, e.max_rtt_ms, allowance_ms=allowance
    )
    return ObservationVerdict(
        anchor,
        details={
            "offset_ms": e.offset_ms,
            "max_rtt_ms": e.max_rtt_ms,
            "uncertainty_ms": clock_uncertainty_ms(e.max_rtt_ms),
            "drift_ms": drift,
            "step_ms": step,
            "allowance_ms": allowance,
            "round_age_ms": elapsed,
        },
    )


# ---------------------------------------------------------------- the facade
class ServiceState(StrEnum):
    RUNNING = "RUNNING"
    #: restored, and the first live bar leaves a gap after the journal: live bars are buffered,
    #: not processed, until `resume_after_downtime` has resolved the gap (SIMULATION §7)
    AWAITING_BACKFILL = "AWAITING_BACKFILL"
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
        session_id: str = SESSION_AGGREGATE_ID,
        observation_gated: bool = False,
    ) -> None:
        self.config = config
        self.journal = journal
        self.observed_sources = observed_sources
        #: P-1 (L-02). Gated, a bar is observed only if it also opens at or after
        #: `observing_from_ms`, and no bar is observed before that anchor is set (fail closed).
        #: Without the gate a direct caller keeps continuous observation, an idealized model
        #: (tests, the instant-restart oracle); the paper runtime always gates.
        self.observation_gated = observation_gated
        self.observing_from_ms: int | None = None
        self.tracker = tracker or MarketStateTracker(
            median_window_ms=config.risk.spread.median_window_ms,
            min_samples=config.risk.spread.min_samples,
            max_offset_age_ms=config.risk.clock_offset_max_age_ms,
            lookback_minutes=config.risk.data_freshness.lookback_minutes,
        )
        self.clock = LiveClock(
            [s.key for s in config.instruments], settle_ms=settle_ms, start_ms=start_ms
        )
        provider = tracker_provider(self.tracker)
        if session is None:
            session = PaperSession(config, provider, self.clock, strategy, session_id)
        else:
            session.go_live(provider, self.clock, strategy)
        self.session = session
        self.state = ServiceState.RUNNING
        self.fault: str | None = None
        #: set by `restore`: live bars must continue the journal without a gap
        self._check_continuity = False
        #: while AWAITING_BACKFILL: "market:symbol" -> minute opens still missing
        self.awaiting_missing: dict[str, list[int]] = {}

    @classmethod
    def open(
        cls,
        config: PaperConfig,
        store: EventStore,
        session_id: str = SESSION_AGGREGATE_ID,
        **kw: Any,
    ) -> PaperTradingService:
        """The production path: journal on the hash-chained event store, restored from it after
        verifying the whole chain (a broken chain raises `JournalError`: fail closed)."""
        journal = EventStoreJournal(store, session_id)
        return cls.restore(config, journal, session_id=session_id, **kw)

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
        session_id = str(kw.pop("session_id", None) or SESSION_AGGREGATE_ID)
        session = replay(config, batches, mode=RunMode.LIVE, session_id=session_id)
        last = session.last_minute_ms
        done = {
            (Market(b[0].payload["bar"]["market"]), str(b[0].payload["bar"]["symbol"]))
            for b in batches
            if b[0].payload["kind"] == "bar" and b[0].payload["bar"]["open_time_ms"] == last
        }
        svc = cls(config, journal, session=session, start_ms=last, session_id=session_id, **kw)
        svc._check_continuity = last is not None
        if last is not None:
            # A minute the live clock closed before the restart stays closed: its missing
            # instruments were a gap the session passed, and a re-delivery of it is late. A
            # minute without a recorded close may still have bars to process (or lost ones).
            closed = session.closed_through_ms == last + MS_PER_MINUTE
            svc.clock = LiveClock(
                [s.key for s in config.instruments],
                settle_ms=svc.clock.settle_ms,
                start_ms=last + MS_PER_MINUTE if closed else last,
                processed=() if closed else done,
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

    def observe_from(self, minute_ms: int) -> None:
        """Set the observation anchor, once: bars opening before it are processed unobserved, as
        downtime is (SIMULATION §7); also gates a service built without the gate."""
        if minute_ms % MS_PER_MINUTE:
            raise ValueError("minute_ms must be a minute open")
        if self.observing_from_ms is not None and minute_ms != self.observing_from_ms:
            raise ValueError("the observation anchor is already set")
        self.observation_gated = True
        self.observing_from_ms = minute_ms

    def _observed(self, bar: Bar) -> bool:
        if bar.source not in self.observed_sources:
            return False
        if not self.observation_gated:
            return True
        return self.observing_from_ms is not None and bar.open_time_ms >= self.observing_from_ms

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
        if not self._contiguous():
            return 0
        done = 0
        released = self.clock.release(now_ms)
        for i, bar in enumerate(released):
            # the clock releases whole minutes: the last bar of a minute here closes it
            closes = i + 1 == len(released) or released[i + 1].open_time_ms != bar.open_time_ms
            step = partial(
                self.session.step_bar,
                bar,
                observed=self._observed(bar),
                released_at_ms=now_ms,
                minute_closed=closes,
            )
            if not self._process(step):
                break
            done += 1
        return done

    def _contiguous(self) -> bool:
        """After a restore: may live bars be processed (no unresolved gap after the journal)?"""
        if not self._check_continuity:
            return True
        first = self.clock.earliest_buffered()
        if first is None:
            return False  # nothing to process yet; decide when the first live bar is there
        missing = self.missing_before(first)
        last = self.session.last_minute_ms
        if last is not None and first > last + MS_PER_MINUTE:
            missing.setdefault("*", list(range(last + MS_PER_MINUTE, first, MS_PER_MINUTE)))
        if missing:
            self.state = ServiceState.AWAITING_BACKFILL
            self.awaiting_missing = missing
            return False
        self.awaiting_missing = {}
        self._check_continuity = False
        if self.state is ServiceState.AWAITING_BACKFILL:
            self.state = ServiceState.RUNNING
        return True

    def missing_before(self, first_live_ms: int, extra: Sequence[Bar] = ()) -> dict[str, list[int]]:
        """Minutes before `first_live_ms` that an exposed instrument (open position or resting
        entry in any book) has not processed, counting the bars in `extra` as processed."""
        have: dict[tuple[Market, str], set[int]] = {}
        for b in extra:
            have.setdefault((b.market, b.symbol), set()).add(b.open_time_ms)
        out: dict[str, list[int]] = {}
        sim = self.session.sim
        closed = sim.last_key  # the session already advanced past every key up to here
        through = self.session.closed_through_ms  # and the live clock closed every minute before
        keys = sorted(
            {k for book in sim.books.values() for k in book.exposed()},
            key=lambda k: (str(k[0]), k[1]),
        )
        for market, symbol in keys:
            last = sim.last_open(market, symbol)
            start = first_live_ms if last is None else last + MS_PER_MINUTE
            # A minute whose delivery key is not after the last processed key was passed while the
            # session was running (the instrument's bar never arrived: a recorded feed gap, which
            # the books and the risk engine already handled). It is not downtime and cannot be
            # backfilled, since bars at or before `last_key` are never processed again. The same
            # holds for a key after `last_key` in a minute the journal records as closed by the
            # live clock (`minute_closed`): the clock released that minute without the bar. Only
            # minutes after both can be unobserved downtime; without a recorded close (journals
            # written before the mark) a key after `last_key` stays downtime, failing closed.
            gaps = [
                m
                for m in range(start, first_live_ms, MS_PER_MINUTE)
                if m not in have.get((market, symbol), set())
                and (closed is None or (m, str(market), symbol) > closed)
                and (through is None or m >= through)
            ]
            if gaps:
                out[f"{market}:{symbol}"] = gaps
        return out

    def resume_after_downtime(self, bars: Sequence[Bar], now_ms: int | None = None) -> int:
        """Resolve open positions over backfilled 1m bars of the downtime (SIMULATION §7).

        Only bars after the last processed one, and outside any minute the journal records as
        closed by the live clock, are used, in clock order, all **unobserved**: no
        signal is computed and no entry fills on them; WATCH/CONFIRMED signals whose validity
        passed become EXPIRED (downtime). Call before feeding live bars of the new session.

        All or nothing: the backfill must give every exposed instrument (open position or resting
        entry) a bar for every minute up to the first buffered live minute (or, with none
        buffered yet, up to the last backfilled minute). Otherwise nothing is processed -- bars
        of one symbol cannot be added later behind another symbol's (clock order) -- the service
        stays AWAITING_BACKFILL, `awaiting_missing` names the symbols and minutes, and the
        return value is 0.

        The last bar of each backfilled minute is journaled ``minute_closed`` (the clock skips
        past these minutes, so a later bar of one is late): a restore after the backfill starts
        after them, as the running service does.
        """
        last = self.session.sim.last_key
        through = self.session.closed_through_ms
        # a bar of a minute the live clock closed is late, as it would be live: the session
        # passed that minute without it
        todo = sorted(
            (
                b
                for b in bars
                if (last is None or order_key(b) > last)
                and (through is None or b.open_time_ms >= through)
            ),
            key=order_key,
        )
        first_live = self.clock.earliest_buffered()
        end = (
            first_live
            if first_live is not None
            else (todo[-1].open_time_ms + MS_PER_MINUTE if todo else None)
        )
        if end is not None:
            missing = self.missing_before(end, todo)
            if missing:
                if self.state is not ServiceState.FAULTED:
                    self.state = ServiceState.AWAITING_BACKFILL
                self.awaiting_missing = missing
                return 0
        done = 0
        for i, bar in enumerate(todo):
            self.tracker.observe_bar(bar)  # backfilled bars are data for the lookback checks
            # The clock skips past every backfilled minute below (`skip_to`): the last bar of each
            # one closes it, journaled as for a live release (`poll`), so a restore after this
            # backfill does not reopen the minute (M3-PB.4). Still unobserved: no entry from it.
            closes = i + 1 == len(todo) or todo[i + 1].open_time_ms != bar.open_time_ms
            step = partial(
                self.session.step_bar,
                bar,
                observed=False,
                released_at_ms=now_ms,
                minute_closed=closes,
            )
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

    def reset_halt(self, book: str, operator: str, note: str = "", *, now_ms: int) -> bool:
        """Manual kill-switch reset, stamped at the last processed close; `now_ms` (the operator's
        wall-clock time) is recorded in the journal and in the reset event.

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
        return self._process(lambda: self.session.reset_halt(book, ts, operator, note, now_ms))

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
            "awaiting_missing": dict(self.awaiting_missing),
            "observation": {
                "gated": self.observation_gated,
                "observing_from_ms": self.observing_from_ms,
            },
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
        drain_max: int = 1000,
    ) -> None:
        """Consume a market-data `Subscription` until `stop` is set (or the service faults).

        Each iteration offers the awaited update and then every update already queued (up to
        `drain_max`, through `get_nowait` when the subscription has it), then snapshots and polls
        once, so a poll decides on every bar that had arrived by then: with a backlog, one update
        per poll would release a minute past its deadline with only its first bar and make the
        rest of that minute late (M3-PB.3). When the bound is reached older updates may still be
        queued, so that iteration neither snapshots nor polls (no minute is released incomplete
        because of the batch limit); it yields and continues."""
        if drain_max < 1:
            raise ValueError("drain_max must be > 0")
        get_nowait = getattr(subscription, "get_nowait", None)
        while not stop.is_set() and self.state is not ServiceState.FAULTED:
            update = await subscription.get(timeout=poll_s)
            if update is not None:
                self.on_update(update)
            drained = 0
            while get_nowait is not None and drained < drain_max:
                queued = get_nowait()
                if queued is None:
                    break
                self.on_update(queued)
                drained += 1
            if drained >= drain_max:
                await asyncio.sleep(0)
                continue
            self.on_snapshot(snapshot())
            self.poll(now_ms())
