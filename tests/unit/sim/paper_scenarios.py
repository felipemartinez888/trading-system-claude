"""Seeded random paper scenarios and the property checks shared by the seeded unit tests (run
offline) and the hypothesis properties (`tests/property/sim/test_paper_properties.py`, CI)."""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal as D
from types import SimpleNamespace
from typing import Literal

from paper_helpers import BTC, ETH, ScriptedStrategy, config, drive, new_service, record, sid
from risk_helpers import snapshot

from tsclaude.contracts.market import Bar, BarSource, Market
from tsclaude.contracts.signals import EntryRuleKind, Side, SignalStatus
from tsclaude.marketdata.bars import BarClosed
from tsclaude.risk.config import RiskConfig
from tsclaude.risk.engine import RiskEngine, RiskStatus
from tsclaude.risk.market import MarketView, SymbolMarket
from tsclaude.runtime.paper_runtime import PaperRuntime
from tsclaude.sim.clock import HistoricalClock, order_key
from tsclaude.sim.costs import CostModel
from tsclaude.sim.journal import JournalEntry, MemoryJournal
from tsclaude.sim.live import LiveClock
from tsclaude.sim.paper import (
    Confirm,
    Intent,
    Invalidate,
    PaperTradingService,
    ServiceState,
    observation_anchor_ms,
    replay,
)
from tsclaude.sim.testing import bar, minute

SYMBOLS = (BTC, ETH)


def random_bars(
    rng: random.Random, n: int, gap_prob: float = 0.0
) -> dict[tuple[Market, str], list[Bar]]:
    out: dict[tuple[Market, str], list[Bar]] = {}
    for key in SYMBOLS:
        price = 10_000  # ticks of 0.1
        seq: list[Bar] = []
        for i in range(n):
            o = price + rng.randint(-3, 3)
            c = max(100, o + rng.randint(-25, 25))
            h = max(o, c) + rng.randint(0, 15)
            lo = min(o, c) - rng.randint(0, 15)
            price = c
            b = bar(i, D(o) / 10, D(h) / 10, D(lo) / 10, D(c) / 10, symbol=key[1], market=key[0])
            seq.append(b.model_copy(update={"source": BarSource.WS_LIVE}))
        out[key] = seq
    return out


def random_script(
    rng: random.Random, bars: dict[tuple[Market, str], list[Bar]], n: int
) -> ScriptedStrategy:
    script: dict[tuple[int, str], list[Intent]] = {}
    count = 0
    watches: list[tuple[str, int, str]] = []
    for i in range(1, n - 3):
        for key in SYMBOLS:
            r = rng.random()
            close = float(bars[key][i].close)
            if r < 0.18:
                count += 1
                sign = 1
                risk = rng.choice([3.0, 6.0, 12.0])
                kind = rng.choice(list(EntryRuleKind))
                price = None
                anchor = close
                if kind is EntryRuleKind.LIMIT:
                    price = round(close - rng.randint(0, 20) / 10, 1)
                    anchor = price
                elif kind is EntryRuleKind.STOP:
                    price = round(close + rng.randint(1, 20) / 10, 1)
                    anchor = price
                stage = SignalStatus.WATCH if rng.random() < 0.25 else SignalStatus.CONFIRMED
                rec = record(
                    count,
                    i,
                    symbol=key[1],
                    side=Side.LONG,
                    stage=stage,
                    ref=close,
                    stop=round(anchor - sign * risk, 1),
                    tp=round(anchor + sign * risk * rng.choice([1.0, 2.5, 4.0]), 1),
                    kind=kind,
                    price=price,
                    valid_bars=rng.randint(2, 7),
                )
                script.setdefault((minute(i), key[1]), []).append(rec)
                if stage is SignalStatus.WATCH:
                    watches.append((sid(count), i, key[1]))
            elif r < 0.22 and count:
                target = sid(rng.randint(1, count))
                script.setdefault((minute(i), key[1]), []).append(Invalidate(target))
        for wid, at, symbol in list(watches):
            if i > at and rng.random() < 0.3:
                script.setdefault((minute(i), symbol), []).append(Confirm(wid))
                watches.remove((wid, at, symbol))
    return ScriptedStrategy(script)


def run_random_session(seed: int, n: int = 40) -> tuple[PaperTradingService, MemoryJournal, dict]:
    rng = random.Random(seed)
    bars = random_bars(rng, n)
    strat = random_script(rng, bars, n)
    missing = {(i, k[1]) for i in range(1, n) for k in SYMBOLS if rng.random() < 0.03}
    svc, journal = new_service(strat)
    drive(svc, bars, list(range(n)), missing=missing)
    return svc, journal, {"bars": bars, "missing": missing, "strategy": strat}


# ---------------------------------------------------------------- property checks
def check_replay_parity(seed: int) -> list[str]:
    svc, journal, _ = run_random_session(seed)
    problems: list[str] = []
    if svc.fault is not None:
        problems.append(f"faulted: {svc.fault}")
    rep = replay(config(), journal.batches())
    if rep.ledger_hashes() != svc.ledger_hashes():
        problems.append("ledger hashes differ")
    if rep.fingerprint() != svc.session.fingerprint():
        problems.append("fingerprint differs")
    for name, book in svc.session.sim.books.items():
        report = book.ledger.reconcile()
        if not report.ok:
            problems.append(f"{name}: {report.problems}")
        if book.cash < 0:
            problems.append(f"{name}: negative cash")
    for s in svc.session.lifecycle.signals():
        if (
            s.status is SignalStatus.OPEN
            and svc.session.sim.books["baseline"].holder(Market(s.market), s.symbol) != s.signal_id
        ):
            problems.append(f"{s.signal_id} OPEN without a baseline position")
    return problems


@dataclass
class Restart:
    """A session whose journal write failed at `fail_at`, restored from the durable journal and
    restarted at `restart_ms` (the crash instant plus the delay)."""

    ref: PaperTradingService  # the uninterrupted session
    ref_journal: MemoryJournal
    restored: PaperTradingService
    journal: MemoryJournal
    strategy: ScriptedStrategy
    bars: dict[tuple[Market, str], list[Bar]]
    missing: set[tuple[int, str]]
    fail_at: int
    durable: int  # batches in the journal at the restore
    crash_ms: int  # wall time of the poll whose write failed (the failed batch's release time)
    restart_ms: int
    closed_through_ms: int | None
    last_key: tuple[int, str, str] | None
    exposed: set[tuple[Market, str]]  # instruments with a position or resting entry at the restore
    calls_at_restore: int  # strategy calls before the restart
    first_live: int | None = None  # restart after downtime: first minute the subscription delivers
    backfill: dict | None = None  # restart after downtime: the runtime's backfill attempt, if any
    observe_from_ms: int | None = None  # restart after downtime: the service's observation anchor
    #: restart after downtime with a startup wait (`reconnect(delivery=...)`): the wall time of the
    #: service's first poll; the anchor is fixed there, where the runtime starts monitoring
    first_poll_ms: int | None = None


def restore_after_crash(seed: int, fail_batch: int, restart_delay_ms: int = 0) -> Restart | str:
    """Run the session of `seed` with a journal write failing at `fail_batch`, and restore it; the
    returned restart has not received anything yet. A string says why there is no restart."""
    ref, ref_journal, ctx = run_random_session(seed)
    fail_at = fail_batch % max(len(ref_journal.batches()), 1)
    rng = random.Random(seed)
    bars = random_bars(rng, 40)
    strat = random_script(rng, bars, 40)
    crash: list[int | None] = []

    def fail(i: int, batch: Sequence[JournalEntry]) -> bool:
        if i != fail_at:
            return False
        crash.append(batch[0].payload.get("released_at_ms"))
        return True

    journal = MemoryJournal(fail_when=fail)
    svc, _ = new_service(strat, journal=journal)
    drive(svc, bars, list(range(40)), missing=ctx["missing"])
    if svc.fault is None:
        return "journal failure was not detected"
    if not crash or crash[0] is None:
        return "crash instant unknown"
    journal.fail_when = None
    durable = len(journal.batches())
    restored = PaperTradingService.restore(config(), journal, strategy=strat, tracker=svc.tracker)
    return Restart(
        ref=ref,
        ref_journal=ref_journal,
        restored=restored,
        journal=journal,
        strategy=strat,
        bars=bars,
        missing=ctx["missing"],
        fail_at=fail_at,
        durable=durable,
        crash_ms=crash[0],
        restart_ms=crash[0] + restart_delay_ms,
        closed_through_ms=restored.session.closed_through_ms,
        last_key=restored.session.sim.last_key,
        exposed={k for book in restored.session.sim.books.values() for k in book.exposed()},
        calls_at_restore=len(strat.calls),
    )


BarStore = Callable[[Market, str, int, int], Sequence[Bar]]
#: how a restarted consumer hands a queued backlog to the service (`reconnect`, M3-PB.3)
Delivery = Literal["per_update", "drained"]


def bar_store(r: Restart) -> BarStore:
    """The live bar store the runtime backfills from: every bar that existed (one the feed never
    delivered is not in it either), read by open time in ``[start_ms, end_ms)``."""

    def read(market: Market, symbol: str, start_ms: int, end_ms: int) -> list[Bar]:
        return [
            b
            for i, b in enumerate(r.bars[(market, symbol)])
            if (i, symbol) not in r.missing and start_ms <= b.open_time_ms < end_ms
        ]

    return read


def redeliver(r: Restart) -> None:
    """Instant restart: the subscription re-delivers every minute from the restored position,
    never polled before the crash instant (zero downtime)."""
    last = r.restored.session.last_minute_ms
    start = 0 if last is None else (last - minute(0)) // 60_000
    drive(r.restored, r.bars, list(range(start, 40)), missing=r.missing, not_before_ms=r.restart_ms)


def reconnect(
    r: Restart,
    store: BarStore | None = None,
    *,
    gated: bool = True,
    delivery: Delivery | None = None,
    startup_wait_ms: int = 0,
) -> None:
    """Restart after downtime, as the paper runtime does it. The service was disconnected from the
    crash to the restart: a new subscription delivers only the bars that close at or after the
    restart (as live bars), polled from the restart on. The first poll that finds a gap leaves the
    service AWAITING_BACKFILL, and the runtime's one backfill attempt per start
    (`PaperRuntime._attempt_backfill`: the live bar store, `validate_backfill`, completeness, then
    `resume_after_downtime`, all unobserved) resolves it or leaves the service blocked.

    Before its first poll the runtime fixes the observation anchor (P-1): the first minute open
    after the restart in exchange time, `observation_anchor_ms`. The harness clock is the exchange
    clock, so the round is exact (offset 0, no round trip) and only the reading resolution bounds
    it; live bars opening before the anchor are processed unobserved. `gated=False` models the
    service before the P-1 fix (observed by source only); it exists so that the checks can be
    shown to report P-1 should it come back.

    `delivery` (M3-PB.3, default None: the model above, unchanged) models how production consumes
    the subscription after a startup wait: the service first polls at
    ``r.restart_ms + startup_wait_ms`` (`r.first_poll_ms`), where the runtime starts monitoring and
    fixes the anchor. Live bars whose minute closed before that first poll are queued, in
    subscription order (per minute ETH's bar before BTC's, as `drive`), and delivered at the first
    poll's wall time:

    - ``"per_update"``: the consumer before M3-PB.3. One queued bar is offered, then the service
      polls; then the next bar, and so on. A queued minute past its settle deadline is released
      with its first bar only and the rest of it is late.
    - ``"drained"``: the consumer after M3-PB.3. Every queued bar is offered, then one poll.

    In both, the runtime's backfill attempt follows the poll that finds the gap (`_run`), and once
    the queue is empty the consumer polls again (its next `get` times out). The market snapshots
    of the queued minutes are the ones `drive` gives (taken before each close), recorded before
    the first poll in both variants, so that the variants differ in delivery only. Later minutes
    arrive on time, as in the default model, never polled before the first poll."""
    if delivery is None and startup_wait_ms:
        raise ValueError("startup_wait_ms needs a delivery mode")
    if startup_wait_ms < 0:
        raise ValueError("startup_wait_ms must be >= 0")
    first_poll = r.restart_ms + startup_wait_ms
    if delivery is not None:
        r.first_poll_ms = first_poll
    if gated:
        r.observe_from_ms = observation_anchor_ms(first_poll, 0, 0)
        r.restored.observe_from(r.observe_from_ms)
    live = [i for i in range(40) if minute(i) + 59_999 >= r.restart_ms]
    r.first_live = live[0] if live else None
    runtime = SimpleNamespace(
        settings=SimpleNamespace(backfill_from_live_bars=True),
        _bars=store or bar_store(r),
        backfill=None,
    )

    def attempt_backfill(now: int) -> bool:
        if r.restored.state is ServiceState.AWAITING_BACKFILL and runtime.backfill is None:
            PaperRuntime._attempt_backfill(runtime, r.restored, now)  # type: ignore[arg-type]
            r.backfill = runtime.backfill
            return True
        return False

    if delivery is not None:
        queued = [i for i in live if minute(i) + 59_999 < first_poll]
        live = live[len(queued) :]
        for i in queued:
            close = minute(i) + 59_999
            r.restored.on_snapshot(snapshot(close - 200, symbols=(BTC, ETH)))
        bars = [
            r.bars[key][i] for i in queued for key in (ETH, BTC) if (i, key[1]) not in r.missing
        ]
        deliver_backlog(r.restored, bars, first_poll, delivery, attempt_backfill)
    for i in live:
        drive(r.restored, r.bars, [i], missing=r.missing, not_before_ms=first_poll)
        now = max(minute(i) + 59_999 + 1_500, first_poll)
        if attempt_backfill(now):
            r.restored.poll(now)


def deliver_backlog(
    service: PaperTradingService,
    bars: Sequence[Bar],
    now_ms: int,
    delivery: Delivery,
    after_poll: Callable[[int], object] = lambda now: None,
) -> None:
    """Hand a queued backlog (`bars`, in subscription order) to `service` at wall time `now_ms`,
    as the consumer does (`reconnect`): ``"per_update"`` offers one bar per poll, ``"drained"``
    offers them all before one poll. `after_poll` runs after each poll (the runtime's backfill
    attempt); a last poll follows the backlog (the consumer's next `get` finds the queue empty)."""
    if delivery == "per_update":
        steps = [[b] for b in bars]
    elif delivery == "drained":
        steps = [list(bars)] if bars else []
    else:
        raise ValueError(f"unknown delivery {delivery!r}")
    for step in steps:
        for b in step:
            service.on_update(BarClosed(b))
        service.poll(now_ms)
        after_poll(now_ms)
    service.poll(now_ms)


def check_restore_equivalence(seed: int, fail_batch: int, restart_delay_ms: int = 0) -> list[str]:
    """A journal write fails at `fail_batch`; the service is restored from the durable journal and
    restarted `restart_delay_ms` after the crash instant (the wall time of the poll whose write
    failed: the failed batch's ``released_at_ms``). The restored service is never polled before
    the restart, since a restart cannot act before it happened. Before M3-PB.2 the re-delivery
    polled at the original wall times, some of them before the crash (causality audit F1).

    - Instant restart (delay 0, `redeliver`): the journal (bars, order, release times, marks,
      events) and the ledgers must equal the uninterrupted session's.
    - Restart after downtime (`reconnect`): real downtime changes fills, so ledgers are not
      compared; the recovery must be causal (`restart_causality`) and end as production recovery
      does (`recovery_outcome`): RUNNING after a valid, complete backfill, or blocked in
      AWAITING_BACKFILL with nothing processed when the backfill cannot be complete."""
    r = restore_after_crash(seed, fail_batch, restart_delay_ms)
    if isinstance(r, str):
        return [r]
    problems = []
    if restart_delay_ms == 0:
        redeliver(r)
        if r.restored.fault is not None:
            problems.append(f"restored service faulted: {r.restored.fault}")
        if r.restored.ledger_hashes() != r.ref.ledger_hashes():
            problems.append(f"ledgers differ after restore at batch {r.fail_at}")
        if _canonical(r.journal.batches()) != _canonical(r.ref_journal.batches()):
            problems.append(f"journal differs after restore at batch {r.fail_at}")
        if r.restored.state is ServiceState.AWAITING_BACKFILL:
            problems.append(f"awaiting backfill: {sorted(r.restored.awaiting_missing)}")
        problems += restart_causality(
            r.journal.batches(), r.durable, r.restart_ms, r.closed_through_ms
        )
    else:
        reconnect(r)
        problems += restart_causality(
            r.journal.batches(),
            r.durable,
            r.restart_ms,
            r.closed_through_ms,
            live_from_ms=r.restart_ms,
            down_from_ms=r.crash_ms,
        )
        problems += recovery_outcome(r)
    return problems


def check_backlog_delivery(
    seed: int,
    fail_batch: int,
    restart_delay_ms: int,
    startup_wait_ms: int,
    delivery: Delivery = "drained",
) -> list[str]:
    """Restart after downtime whose first poll comes `startup_wait_ms` after the restart, the
    queued backlog handed over as `delivery` (`reconnect`). No bar the new subscription delivered
    may be late: each one is released, or still buffered while the service awaits its backfill.
    Holds for ``"drained"`` (M3-PB.3); ``"per_update"`` loses the second bar of every queued minute
    already past its settle deadline once the first one has been polled."""
    r = restore_after_crash(seed, fail_batch, restart_delay_ms)
    if isinstance(r, str):
        return [r]
    reconnect(r, delivery=delivery, startup_wait_ms=startup_wait_ms)
    clock = r.restored.clock
    problems = []
    if r.restored.fault is not None:
        problems.append(f"restored service faulted: {r.restored.fault}")
    if clock.late:
        problems.append(f"late bars: {[order_key(b) for b in clock.late][:5]}")
    buffered = sum(len(slot) for slot in clock._buffer.values())
    if clock.stats.offered != clock.stats.released + buffered:
        problems.append(
            f"offered {clock.stats.offered}, released {clock.stats.released}, buffered {buffered}"
        )
    return problems


def _canonical(batches: Sequence[Sequence[JournalEntry]]) -> list[object]:
    return [e.canonical() for b in batches for e in b]


def _ceil_minute(ms: int) -> int:
    return -(-ms // 60_000) * 60_000


# events that only a strategy decision on an observed bar produces
_DECISIONS = ("signal_created", "sim_order_submitted", "sim_exit_requested", "sim_request_rejected")


def restart_causality(
    batches: Sequence[Sequence[JournalEntry]],
    durable: int,
    restart_ms: int,
    closed_through_ms: int | None,
    *,
    live_from_ms: int | None = None,
    down_from_ms: int | None = None,
) -> list[str]:
    """Causality of what a restored service journaled after the first `durable` batches.

    - Bars: once each and in clock order over the whole journal. After the restore: none of a
      minute closed before the crash; a bar the live clock released is released after its close,
      at or after the restart, in nondecreasing time. With `live_from_ms` (a restart after
      downtime) a bar that closed before it is processed only unobserved, from the backfill, and
      every backfilled bar comes before the first live one.
    - Decisions: an unobserved bar produces no signal, order, exit request or rejected request.
      An order submitted after the restore is executable no earlier than the restart, the minute
      after its release (`not_before_ms`) and the open after its decision; a market exit
      requested after the restore is due no earlier than that minute either, unless the position
      already had that due time.
    - Fills: an entry only after its submission, not before its earliest execution, never on an
      unobserved bar, and never on a bar whose minute overlaps the downtime
      ``[down_from_ms, live_from_ms)``: at an open inside it, or anywhere in the minute the crash
      interrupted, at its open included. The order was not on any exchange during the downtime
      (SIMULATION §7 as `book.py` applies it), and L-02 does not count a partly monitored minute
      as observed. A bar's fills are journaled with the bar, at its release after its close, so
      no fill on such a minute can be durable before the crash; fills journaled before the
      restore are not judged here. A stop-loss, take-profit or market exit not before its
      position's entry fill, and a take-profit in the entry's own bar only after an entry at the
      open (L-04). Resting SL/TP and a market exit already due are resolved over the downtime path
      by design (SIMULATION §7), so they are not restricted to the restart."""
    problems: list[str] = []
    prev_key: tuple[int, str, str] | None = None
    prev_release = restart_ms
    seen_live = False
    earliest: dict[str, int] = {}
    entry_fill: dict[str, tuple[int, bool]] = {}  # position prefix -> (bar open, at the open)
    exit_due: dict[tuple[str, str, str], int] = {}
    for idx, batch in enumerate(batches):
        head = batch[0].payload
        after = idx >= durable
        observed = head.get("observed", True)
        released = head.get("released_at_ms")
        if head.get("kind") == "bar":
            b = Bar.model_validate(head["bar"])
            key = order_key(b)
            if after:
                if released is not None:
                    if released <= b.close_time_ms:
                        problems.append(f"{key} released at {released}, before its close")
                    if released < prev_release:
                        problems.append(f"{key} released at {released}, before {prev_release}")
                    else:
                        prev_release = released
                elif live_from_ms is None:
                    problems.append(f"{key} processed without a release time")
                if closed_through_ms is not None and b.open_time_ms < closed_through_ms:
                    problems.append(f"{key} processed in a minute closed before the crash")
                if live_from_ms is not None:
                    if b.close_time_ms < live_from_ms and observed:
                        problems.append(f"{key} closed during the downtime but processed observed")
                    if observed:
                        seen_live = True
                    elif seen_live:
                        problems.append(f"{key} backfilled after a live bar")
            if prev_key is not None and key <= prev_key:
                problems.append(f"{key} processed again or out of clock order")
            prev_key = key
        floor = None
        if after and released is not None:
            floor = max(restart_ms, _ceil_minute(released))
        for e in batch[1:]:
            p = e.payload
            if not observed and e.type in _DECISIONS:
                problems.append(f"{e.type} on the unobserved bar of batch {idx}")
            if e.type == "sim_order_submitted":
                earliest[p["order_id"]] = p["earliest_exec_ms"]
                if after and p["earliest_exec_ms"] < restart_ms:
                    problems.append(f"{p['order_id']} executable before the restart")
                if floor is not None and p["earliest_exec_ms"] < floor:
                    problems.append(f"{p['order_id']} executable before its release minute")
                if p["earliest_exec_ms"] <= p["decision_time_ms"]:
                    problems.append(f"{p['order_id']} executable at or before its decision")
            elif e.type == "sim_exit_requested" and p.get("action") == "market_exit":
                pos = (e.aggregate_id, p["market"], p["symbol"])
                due = p["exit_due_ms"]
                if floor is not None and due < floor and exit_due.get(pos) != due:
                    problems.append(f"{pos} market exit due before its release minute")
                exit_due[pos] = due
            elif e.type == "sim_order_filled":
                oid, role, at = p["order_id"], p["role"], p["bar_open_ms"]
                prefix = oid.rsplit(":", 1)[0]
                if role == "entry":
                    due = earliest.get(oid)
                    if due is None:
                        problems.append(f"{oid} filled without a submission")
                    elif at < due:
                        problems.append(f"{oid} filled before its earliest execution")
                    if not observed:
                        problems.append(f"{oid} filled on an unobserved bar")
                    if after and live_from_ms is not None and down_from_ms is not None:
                        if down_from_ms <= at < live_from_ms:
                            problems.append(f"{oid} filled at an open during the downtime")
                        elif at < down_from_ms < at + 60_000:
                            problems.append(f"{oid} filled in the minute the crash interrupted")
                    entry_fill[prefix] = (at, bool(p.get("at_open")))
                else:
                    entry = entry_fill.get(prefix)
                    if entry is None or at < entry[0]:
                        problems.append(f"{oid} filled before its position's entry")
                    elif role == "take_profit" and at == entry[0] and not entry[1]:
                        problems.append(f"{oid} take-profit in the bar of an intrabar entry")
    return problems


def _processed(
    batches: Sequence[Sequence[JournalEntry]],
) -> list[tuple[tuple[int, str, str], bool]]:
    return [
        (order_key(Bar.model_validate(b[0].payload["bar"])), b[0].payload["observed"])
        for b in batches
        if b[0].payload.get("kind") == "bar"
    ]


def recovery_outcome(r: Restart, store: BarStore | None = None) -> list[str]:
    """What a restart after downtime processed, and the state it ended in.

    - Observation (P-1, L-02 as clarified by the owner): the service observes from the anchor
      production fixes at the restart (`observation_anchor_ms`). A bar is processed observed only
      if it opens at or after that anchor; the bars before it (the downtime, and the live minute
      the restart interrupted) are processed only unobserved, and the strategy never sees one.
    - Inputs: only bars that exist, after the journal (after its last key and outside minutes
      closed before the crash). Those that closed before the restart are downtime bars. With the
      service RUNNING, every live bar the uninterrupted session released is processed, and every
      downtime bar of an instrument exposed at the restore (one with nothing to resolve may be
      left out, as `missing_before` allows).
    - State: never FAULTED. AWAITING_BACKFILL only with nothing processed after the restore; with
      a valid and complete store (every instrument's bar for every downtime minute after the
      journal, a minute without any bar counting as missing; no duplicate, no bar outside the
      requested range) the service must be RUNNING."""
    problems: list[str] = []
    svc = r.restored
    read = store or bar_store(r)
    if svc.state is ServiceState.FAULTED:
        return [f"restored service faulted: {svc.fault}"]
    # the runtime fixes the anchor at its first poll: the restart, or after a startup wait
    first_poll = r.restart_ms if r.first_poll_ms is None else r.first_poll_ms
    anchor = observation_anchor_ms(first_poll, 0, 0)
    if svc.observing_from_ms != anchor:
        problems.append(f"observing from {svc.observing_from_ms}, not from the anchor {anchor}")
    after = _processed(r.journal.batches()[r.durable :])
    keys = {k for k, _ in after}

    def eligible(key: tuple[int, str, str]) -> bool:
        return (r.last_key is None or key > r.last_key) and (
            r.closed_through_ms is None or key[0] >= r.closed_through_ms
        )

    existing = {
        order_key(b): b
        for key in SYMBOLS
        for i, b in enumerate(r.bars[key])
        if (i, key[1]) not in r.missing and eligible(order_key(b))
    }
    for k, observed in after:
        b = existing.get(k)
        if b is None:
            problems.append(f"{k} processed but not a bar after the journal")
        elif observed != (b.open_time_ms >= anchor):
            problems.append(f"{k} processed {'observed' if observed else 'unobserved'}")
    downtime = {k for k, b in existing.items() if b.close_time_ms < r.restart_ms}
    unobserved = {k for k, b in existing.items() if b.open_time_ms < anchor}
    seen = {(k[0], k[2]) for k in unobserved} & set(r.strategy.calls[r.calls_at_restore :])
    if seen:
        problems.append(f"the strategy saw downtime bars {sorted(seen)}")
    # complete: the store has every instrument's bar for every downtime minute after the journal
    complete = True
    if r.first_live is not None and r.last_key is not None:
        start, end = r.last_key[0], minute(r.first_live)
        for market, symbol in SYMBOLS:
            rows = list(read(market, symbol, start, end))
            have = {b.open_time_ms for b in rows}
            if len(have) != len(rows) or not all(start <= m < end for m in have):
                complete = False  # an invalid store: the runtime refuses it whole
            for m in range(start, end, 60_000):
                if eligible((m, str(market), symbol)) and m not in have:
                    complete = False
    if svc.state is ServiceState.AWAITING_BACKFILL:
        if after:
            problems.append(f"AWAITING_BACKFILL after processing {len(after)} bars")
        if complete:
            problems.append("AWAITING_BACKFILL although the bar store has every downtime bar")
    elif r.first_live is not None and svc.state is ServiceState.RUNNING:
        live = {
            k
            for k, _ in _processed(r.ref_journal.batches())
            if k in existing and existing[k].close_time_ms >= r.restart_ms
        }
        if lost := sorted(live - keys):
            problems.append(f"live bars not processed: {lost[:5]}")
        needed = {k for k in downtime if (Market(k[1]), k[2]) in r.exposed}
        if lost := sorted(needed - keys):
            problems.append(f"downtime bars of exposed instruments not processed: {lost[:5]}")
    return problems


def check_live_clock_order(
    seed: int, settle_ms: int = 30_000, start_ms: int | None = None
) -> list[str]:
    """Any arrival order in which each bar arrives before its minute's deadline releases exactly
    the historical clock order of all delivered bars."""
    rng = random.Random(seed)
    n = 12
    keys = [BTC, ETH, (Market.USDM, "BTCUSDT")]
    arrivals: list[tuple[int, Bar]] = []
    delivered: list[Bar] = []
    for i in range(n):
        for key in keys:
            if rng.random() < 0.1:
                continue
            b = bar(i, 100, 101, 99, 100, symbol=key[1], market=key[0])
            close = b.close_time_ms
            arrivals.append((close + 1 + rng.randint(0, settle_ms - 1), b))
            delivered.append(b)
    arrivals.sort(key=lambda t: t[0])
    clock = LiveClock(keys, settle_ms=settle_ms, start_ms=start_ms)
    released: list[Bar] = []
    for t, b in arrivals:
        if clock.offer(b) != "buffered":
            return [f"unexpected offer result for {order_key(b)}"]
        released.extend(clock.release(t))
    released.extend(clock.release(minute(n + 10)))
    expected = list(HistoricalClock(delivered))
    return [] if released == expected and not clock.late else ["release order differs"]


def check_risk_engine_fail_closed(seed: int) -> list[str]:
    """Deterministic, and never PASSED when any market input is missing or after the cutoff."""
    import sys
    from dataclasses import replace
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "risk"))
    from risk_helpers import DECISION, check, view

    rng = random.Random(seed)
    engine = RiskEngine(RiskConfig(), CostModel())
    base = view()
    sym = base.symbols[(Market.SPOT, "BTCUSDT")]
    field = rng.choice(
        ["spread_bps", "last_bar_close_ms", "last_close", "offset", "as_of", "stream", "none"]
    )
    v: MarketView | None
    if field == "none":
        v = None
    elif field == "offset":
        v = replace(base, clock_offset_ms=None)
    elif field == "as_of":
        v = replace(base, as_of_ms=DECISION + rng.randint(1, 10_000))
    elif field == "stream":
        v = view(stream_count=0)
    else:
        broken: SymbolMarket = replace(sym, **{field: None})
        v = MarketView(base.as_of_ms, base.clock_offset_ms, {(Market.SPOT, "BTCUSDT"): broken})
    equity = D(rng.randint(5_000, 20_000))
    ck = check(equity=equity, peak_equity=equity, start_of_day_equity=equity)
    if engine.evaluate(ck, base).status is not RiskStatus.PASSED:
        return ["the complete baseline view must pass"]
    first, second = engine.evaluate(ck, v), engine.evaluate(ck, v)
    problems = [] if first == second else ["non-deterministic verdict"]
    if first.status is RiskStatus.PASSED:
        problems.append(f"passed with missing {field}")
    return problems


def seeds(n: int) -> Sequence[int]:
    return list(range(n))


# ---------------------------------------------------------------- recovery outcomes (OD-2)
# Three mutually exclusive outcomes of a restart (owner decision OD-2, docs/tasks/M3-PB.md):
#
# - RECOVERED: the existing requirements pass, unchanged (`check_restore_equivalence`: strict
#   journal and ledger equality for an instant restart; causality and `recovery_outcome` after
#   downtime), and the independent oracle finds the evidence a resume needs.
# - SAFE_BLOCK: the service is blocked in AWAITING_BACKFILL, the oracle proves from the harness
#   ground truth that no store could have held the evidence a resume needs (insufficient
#   evidence: E), and every safety invariant S1-S7 holds. A safe block is NOT a successful
#   recovery, NOT replay equivalence and NOT availability: the session stays stopped.
# - FAIL: anything else (an unsafe continuation, a divergence, a journal mutation while blocked,
#   a block although the evidence exists, a fault, a failed invariant).
#
# The oracle never calls production's missing-data logic (`missing_before`, `_contiguous`,
# `awaiting_missing`, `_attempt_backfill`, `validate_backfill`): it derives the evidence a resume
# needs from the durable journal's own records (bars, `minute_closed` marks, book events) and from
# the harness ground truth (every bar that existed, the restart instant).

RECOVERED = "RECOVERED"
SAFE_BLOCK = "SAFE_BLOCK"
FAIL = "FAIL"
#: SAFE_BLOCK reasons: a required minute where no instrument has a bar (a genuinely empty minute,
#: which nothing persisted can tell from lost data), or a required bar of one exposed instrument
#: that never existed while another instrument has a bar in that minute
EMPTY_MINUTE = "EMPTY_MINUTE"
INSTRUMENT_GAP = "INSTRUMENT_GAP"
#: the only reasons a backfill refusal may give for a safe block (completeness, not validity)
_COMPLETENESS_REFUSALS = ("minute(s) missing from the backfill", "minute before the first live")
#: extra polls after the recovery, to check that a block is stable (S7)
_STABILITY_POLLS_MS = (60_000, 600_000, 86_400_000)


@dataclass(frozen=True)
class RecoveryEvidence:
    """What the oracle derives for a restart, independently of production's missing-data logic.

    `required`: (minute open, symbol) bars a resume needs; symbol None means "a bar of any
    instrument" (the minute before the first live minute). `absent`: the required items with no
    bar in the ground truth. `feasible`: nothing required is absent."""

    last_minute_ms: int | None  # the last minute with a bar in the durable journal
    closed_through_ms: int | None  # from the journal's `minute_closed` marks
    clock_start_ms: int | None  # the first minute the restored service may still process
    first_live_ms: int | None  # the first minute the restarted feed delivers a processable bar
    exposed: frozenset[tuple[Market, str]]  # from the journal's book events
    required: frozenset[tuple[int, str | None]]
    absent: frozenset[tuple[int, str | None]]

    @property
    def feasible(self) -> bool:
        return not self.absent


@dataclass(frozen=True)
class RestartOutcome:
    kind: str  # RECOVERED, SAFE_BLOCK or FAIL
    reason: str | None  # SAFE_BLOCK: EMPTY_MINUTE or INSTRUMENT_GAP
    problems: tuple[str, ...]
    evidence: RecoveryEvidence


#: outcomes of the gate cases (the restart properties and the delayed census) by group, printed at
#: the end of the test run and written as JSON to $TSC_RESTART_REPORT if set (tests/conftest.py)
OUTCOME_REPORT: dict[str, Counter[str]] = {}


def report_outcome(group: str, out: RestartOutcome) -> None:
    key = out.kind if out.reason is None else f"{out.kind} {out.reason}"
    OUTCOME_REPORT.setdefault(group, Counter())[key] += 1


def journal_exposure(batches: Sequence[Sequence[JournalEntry]]) -> set[tuple[Market, str]]:
    """Instruments with an open position or a resting entry in any book after `batches`, from the
    journaled book events only (submitted entry orders until filled, cancelled, expired or
    rejected; opened positions until closed)."""
    pending: dict[str, tuple[str, Market, str]] = {}
    positions: set[tuple[str, Market, str]] = set()
    for batch in batches:
        for e in batch[1:]:
            p = e.payload
            if e.type == "sim_order_submitted" and p.get("role") == "entry":
                pending[p["order_id"]] = (p["book"], Market(p["market"]), p["request"]["symbol"])
            elif e.type in (
                "sim_order_filled",
                "sim_order_cancelled",
                "sim_order_expired",
                "sim_order_rejected",
            ):
                pending.pop(p["order_id"], None)
            elif e.type == "sim_position_opened":
                positions.add((p["book"], Market(p["market"]), p["symbol"]))
            elif e.type == "sim_position_closed":
                positions.discard((p["book"], Market(p["market"]), p["symbol"]))
    return {(m, s) for _b, m, s in [*pending.values(), *positions]}


def _journal_bars(batches: Sequence[Sequence[JournalEntry]]) -> list[tuple[Bar, bool]]:
    return [
        (Bar.model_validate(b[0].payload["bar"]), bool(b[0].payload.get("minute_closed")))
        for b in batches
        if b[0].payload.get("kind") == "bar"
    ]


def _index(ms: int) -> int:
    return (ms - minute(0)) // 60_000


def recovery_evidence(r: Restart, *, live_from_ms: int | None = None) -> RecoveryEvidence:
    """The evidence a resume of `r` needs, and what of it never existed (the oracle E).

    From the durable journal: the last minute with a bar, the last key, each instrument's last
    processed minute, the minutes closed by `minute_closed` marks, the exposure (book events).
    From the ground truth: which bars existed (`r.bars` without `r.missing`) and, after downtime
    (`live_from_ms`, the restart), which the new subscription delivers (those closing at or after
    the restart). The contract the oracle states (SIMULATION §7, M3-PB):

    - A minute closed before the crash stays closed; the restored service may process minutes
      from the one after the last closed minute (else from the last journaled minute, for the
      instruments not processed in it).
    - The first live minute is the first minute from there with a delivered bar not yet journaled.
    - A resume needs, for every exposed instrument, its bar for each minute before the first live
      one after its last processed minute, the last key and the closed minutes (this includes the
      rest of an unclosed last minute). If minutes lie between the last journaled minute and the
      first live one, it also needs a bar of some instrument in the minute just before the first
      live minute (the backfill must reach the live feed).

    The oracle restates these rules from the contract; it is independent of production's code,
    not of the contract: an error shared by the contract and production would not be seen."""
    durable = r.journal.batches()[: r.durable]
    done_bars = _journal_bars(durable)
    exposed = frozenset(journal_exposure(durable))
    if not done_bars:
        return RecoveryEvidence(None, None, None, None, exposed, frozenset(), frozenset())
    last_key = max(order_key(b) for b, _ in done_bars)
    last = last_key[0]
    marks = [b.open_time_ms + 60_000 for b, closed in done_bars if closed]
    closed_through = max(marks) if marks else None
    clock_start = last + 60_000 if closed_through == last + 60_000 else last
    done_last = {(b.market, b.symbol) for b, _ in done_bars if b.open_time_ms == last}
    last_open: dict[tuple[Market, str], int] = {}
    for b, _ in done_bars:
        k = (b.market, b.symbol)
        last_open[k] = max(last_open.get(k, b.open_time_ms), b.open_time_ms)

    def exists(i: int, key: tuple[Market, str]) -> bool:
        return 0 <= i < len(r.bars[key]) and (i, key[1]) not in r.missing

    first_live = None
    for i in range(_index(clock_start), len(r.bars[SYMBOLS[0]])):
        delivered = [
            k
            for k in SYMBOLS
            if exists(i, k)
            and (live_from_ms is None or r.bars[k][i].close_time_ms >= live_from_ms)
            and not (minute(i) == last and k in done_last)
        ]
        if delivered:
            first_live = minute(i)
            break
    required: set[tuple[int, str | None]] = set()
    if first_live is not None:
        for key in sorted(exposed, key=lambda k: (str(k[0]), k[1])):
            start = last_open[key] + 60_000 if key in last_open else first_live
            for m in range(start, first_live, 60_000):
                if (m, str(key[0]), key[1]) > last_key and (
                    closed_through is None or m >= closed_through
                ):
                    required.add((m, key[1]))
        if first_live > last + 60_000:
            required.add((first_live - 60_000, None))
    by_symbol = {k[1]: k for k in SYMBOLS}
    absent = {
        (m, s)
        for m, s in required
        if not (
            any(exists(_index(m), k) for k in SYMBOLS)
            if s is None
            else exists(_index(m), by_symbol[s])
        )
    }
    return RecoveryEvidence(
        last,
        closed_through,
        clock_start,
        first_live,
        exposed,
        frozenset(required),
        frozenset(absent),
    )


def _block_reason(r: Restart, ev: RecoveryEvidence) -> str:
    empty = any(all((_index(m), k[1]) in r.missing for k in SYMBOLS) for m, _ in ev.absent)
    return EMPTY_MINUTE if empty else INSTRUMENT_GAP


def classify_restart(
    r: Restart,
    strict: Sequence[str],
    *,
    instant: bool,
    stability: Sequence[tuple[int, int, bool]] = (),
) -> RestartOutcome:
    """Classify a recovered restart (`r`, after its re-delivery or reconnection, the runtime's
    backfill attempt and the stability polls). `strict`: what the existing check reported for the
    same case (`check_restore_equivalence`, unchanged). `stability`: (batches, strategy calls,
    blocked) after the recovery and after each stability poll."""
    ev = recovery_evidence(r, live_from_ms=None if instant else r.restart_ms)
    svc = r.restored
    batches = r.journal.batches()
    problems: list[str] = []
    if svc.state is ServiceState.FAULTED:
        return RestartOutcome(FAIL, None, (f"restored service faulted: {svc.fault}",), ev)
    if _canonical(batches[: r.durable]) != _canonical(r.ref_journal.batches()[: r.durable]):
        problems.append("the durable prefix is not the uninterrupted journal's prefix")
    if instant:
        problems += restart_causality(batches, r.durable, r.restart_ms, r.closed_through_ms)
    else:
        problems += restart_causality(
            batches,
            r.durable,
            r.restart_ms,
            r.closed_through_ms,
            live_from_ms=r.restart_ms,
            down_from_ms=r.crash_ms,
        )
    if svc.state is not ServiceState.AWAITING_BACKFILL:
        problems += strict
        if not ev.feasible:
            problems.append(
                f"resumed without the evidence a resume needs: {sorted(ev.absent, key=str)}"
            )
        return RestartOutcome(RECOVERED if not problems else FAIL, None, tuple(problems), ev)
    # AWAITING_BACKFILL: a safe block only with E and S1-S7
    if not instant:
        problems += strict  # the existing causal requirements after downtime still apply
    if ev.feasible:
        problems.append("E: blocked although the ground truth holds every bar a resume needs")
    if len(batches) != r.durable:
        problems.append(f"S1: {len(batches) - r.durable} batches journaled after the prefix")
    if len(r.strategy.calls) != r.calls_at_restore:
        problems.append("S2: the strategy was called after the restore")
    if svc.session.batches != r.durable:
        problems.append("S3: the session stepped past the durable prefix")
    if svc.ledger_hashes() != replay(config(), batches[: r.durable]).ledger_hashes():
        problems.append("S4: the ledgers are not the durable prefix's")
    if not svc.awaiting_missing:
        problems.append("S5: blocked without naming a missing minute")
    elif ev.last_minute_ms is not None and ev.first_live_ms is not None:
        assert ev.clock_start_ms is not None
        for key, minutes in svc.awaiting_missing.items():
            lo = ev.last_minute_ms + 60_000 if key == "*" else ev.clock_start_ms
            if any(not lo <= m < ev.first_live_ms for m in minutes):
                problems.append(f"S5: {key} names a minute outside the downtime: {minutes}")
        named = {m for minutes in svc.awaiting_missing.values() for m in minutes}
        if unnamed := sorted({m for m, _ in ev.absent} - named):
            problems.append(f"S5: the missing minutes {unnamed} are not named")
    bf = r.backfill
    if bf is None or bf.get("ok") or bf.get("processed"):
        problems.append(f"S6: no refused backfill attempt: {bf}")
    elif not bf.get("problems") or any(
        not any(c in p for c in _COMPLETENESS_REFUSALS) for p in bf["problems"]
    ):
        problems.append(f"S6: backfill refused for another reason: {bf.get('problems')}")
    if not stability:
        problems.append("S7: no stability polls")
    for n, calls, blocked in stability:
        if n != r.durable or calls != r.calls_at_restore or not blocked:
            problems.append(f"S7: the block was not stable ({n} batches, {calls} calls)")
            break
    if problems:
        return RestartOutcome(FAIL, None, tuple(problems), ev)
    return RestartOutcome(SAFE_BLOCK, _block_reason(r, ev), (), ev)


def runtime_backfill(r: Restart, now_ms: int, store: BarStore | None = None) -> None:
    """The paper runtime's one backfill attempt per start (`PaperRuntime._attempt_backfill`), as
    `reconnect` makes it, for an instant restart that blocked; then the poll that follows."""
    if r.restored.state is not ServiceState.AWAITING_BACKFILL or r.backfill is not None:
        return
    runtime = SimpleNamespace(
        settings=SimpleNamespace(backfill_from_live_bars=True),
        _bars=store or bar_store(r),
        backfill=None,
    )
    PaperRuntime._attempt_backfill(runtime, r.restored, now_ms)  # type: ignore[arg-type]
    r.backfill = runtime.backfill
    r.restored.poll(now_ms)


def _state(r: Restart) -> tuple[int, int, bool]:
    return (
        len(r.journal.batches()),
        len(r.strategy.calls),
        r.restored.state is ServiceState.AWAITING_BACKFILL,
    )


def stability_polls(r: Restart) -> list[tuple[int, int, bool]]:
    """Poll the recovered service later (S7): (batches, strategy calls, blocked) before and after
    each poll."""
    end = max(r.restart_ms, minute(40) + 1_500)
    out = [_state(r)]
    for dt in _STABILITY_POLLS_MS:
        r.restored.poll(end + dt)
        out.append(_state(r))
    return out


def restart_outcome(
    seed: int, fail_batch: int, restart_delay_ms: int = 0, store: BarStore | None = None
) -> RestartOutcome:
    """The OD-2 outcome of the restart `check_restore_equivalence` checks (same case, same
    production path), plus the runtime's backfill attempt after an instant restart that blocks
    (production makes one per start), and the stability polls. `store`: the live bar store the
    runtime backfills from (default: every bar that existed). After downtime with another store,
    the existing checks run against the store the runtime read (`recovery_outcome(r, store)`)."""
    r = restore_after_crash(seed, fail_batch, restart_delay_ms)
    if isinstance(r, str):
        raise AssertionError(r)
    if restart_delay_ms == 0:
        strict = check_restore_equivalence(seed, fail_batch)
        redeliver(r)
        runtime_backfill(r, minute(40) + 1_500, store)
        return classify_restart(r, strict, instant=True, stability=stability_polls(r))
    if store is None:
        return outcome_after_downtime(
            r, strict=check_restore_equivalence(seed, fail_batch, restart_delay_ms)
        )
    return outcome_after_downtime(r, store)


def outcome_after_downtime(
    r: Restart, store: BarStore | None = None, *, strict: Sequence[str] | None = None
) -> RestartOutcome:
    """Reconnect `r` after its downtime (`reconnect`, the production path), then classify it.
    `strict`: the existing checks' report if already computed for the same case; by default they
    run on `r` itself (`recovery_outcome` against the store the runtime read; the classifier adds
    `restart_causality`), so a test may change the ground truth of `r` (`r.missing`) first."""
    reconnect(r, store)
    if strict is None:
        strict = recovery_outcome(r, store)
    return classify_restart(r, strict, instant=False, stability=stability_polls(r))
