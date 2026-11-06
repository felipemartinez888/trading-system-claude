"""M3-PB.4: a downtime backfill closes the minutes it resolves, durably.

`PaperTradingService.resume_after_downtime` processes the backfilled bars (unobserved) and then
moves the live clock past the last backfilled minute (`LiveClock.skip_to`): every backfilled
minute is closed, and a later bar of one of them is late. Before M3-PB.4 nothing in the journal
recorded that close (the backfilled steps carried no ``minute_closed``), so after a second crash
`restore` reopened the last backfilled minute and processed a bar the uninterrupted service had
classified late (strategy called on it, a new journal batch). The backfill now marks the last bar
of each backfilled minute ``minute_closed`` (the M3-PB.1 key, no new key or kind), so the restored
clock starts after the backfill, as the uninterrupted one does.

Also here: the second-crash variants (a journal write failing inside the backfill or right after
it), the historical reading of journals written before the change, and the characterisation of
the two known empty-minute counterexamples of the instant-restart property (OD-1, not fixed).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import pytest
from paper_helpers import (
    BTC,
    ETH,
    ScriptedStrategy,
    config,
    drive,
    new_service,
    record,
    two_symbol_bars,
)
from paper_scenarios import (
    check_restore_equivalence,
    redeliver,
    restart_causality,
    restore_after_crash,
    run_random_session,
)

from tsclaude.contracts.market import Bar, Market
from tsclaude.marketdata.bars import BarClosed
from tsclaude.sim.journal import Batch, JournalEntry, MemoryJournal
from tsclaude.sim.paper import PaperTradingService, ServiceState, replay
from tsclaude.sim.testing import minute

M = 60_000
CFG = config(benchmark=False)
# BTC: a signal at minute 2 opens a position (exposed through the downtime); ETH is never exposed
SCRIPT = {(minute(2), "BTCUSDT"): [record(1, 2)]}
# events only a strategy decision produces (an order or an entry)
DECISIONS = ("signal_created", "sim_order_submitted", "sim_exit_requested", "sim_request_rejected")


def _canonical(batches: Sequence[Batch]) -> list[object]:
    return [e.canonical() for b in batches for e in b]


def _bar_heads(batches: Sequence[Batch]) -> list[dict]:
    return [b[0].payload for b in batches if b[0].payload["kind"] == "bar"]


def _rel(h: dict) -> tuple[str, int]:
    return (h["bar"]["symbol"], (h["bar"]["open_time_ms"] - minute(0)) // M)


def _copy(batches: Sequence[Batch]) -> MemoryJournal:
    out = MemoryJournal()
    for b in batches:
        out.append(b)
    return out


def _bar_key(batch: Sequence[JournalEntry]) -> tuple[str, int] | None:
    b = batch[0].payload.get("bar") or {}
    if not b:
        return None
    return (b["symbol"], (b["open_time_ms"] - minute(0)) // M)


@dataclass
class Downtime:
    """Minutes 0-5 live, a restart, live minute 9 buffered, downtime 6-8 backfilled (ETH 8 is not
    in the store: REST had not recovered it), so the service is RUNNING over the backfill."""

    svc: PaperTradingService
    journal: MemoryJournal
    strategy: ScriptedStrategy
    bars: dict[tuple[Market, str], list[Bar]]
    restored_at: int  # batches durable at the restart
    calls_at_restore: int
    backfilled: int  # return value of resume_after_downtime


BACKFILL = [(BTC, 6), (ETH, 6), (BTC, 7), (ETH, 7), (BTC, 8)]


def _downtime(
    fail_when: Callable[[int, list[JournalEntry]], bool] | None = None,
) -> Downtime:
    bars = two_symbol_bars(14)
    journal = MemoryJournal()
    first, _ = new_service(ScriptedStrategy(SCRIPT), journal=journal, cfg=CFG)
    drive(first, bars, list(range(6)))
    assert {n: sorted(b.exposed()) for n, b in first.session.sim.books.items()} == {
        "baseline": [BTC],
        "shadow": [BTC],
    }
    strategy = ScriptedStrategy(SCRIPT)
    svc = PaperTradingService.restore(CFG, journal, strategy=strategy)
    restored_at = len(journal.batches())
    drive(svc, bars, [9])
    assert svc.state is ServiceState.AWAITING_BACKFILL
    journal.fail_when = fail_when
    n = svc.resume_after_downtime([bars[k][i] for k, i in BACKFILL])
    return Downtime(svc, journal, strategy, bars, restored_at, 0, n)


def _after_backfill(svc: PaperTradingService, bars: dict, *, redeliver_9: bool) -> None:
    """ETH 8 arrives late from the live feed; then the poll at minute 10 (minute 9 re-delivered
    first if the service was restored after it was buffered)."""
    svc.on_update(BarClosed(bars[ETH][8]))
    if redeliver_9:
        svc.on_update(BarClosed(bars[ETH][9]))
        svc.on_update(BarClosed(bars[BTC][9]))
    svc.poll(minute(10))


def _uninterrupted() -> tuple[Downtime, int]:
    """(scenario A: no second crash, the batch count right after the backfill)."""
    d = _downtime()
    assert d.backfilled == 5
    at = len(d.journal.batches())
    _after_backfill(d.svc, d.bars, redeliver_9=False)
    return d, at


def _late(svc: PaperTradingService) -> list[tuple[str, int]]:
    return [(b.symbol, (b.open_time_ms - minute(0)) // M) for b in svc.clock.late]


# ---------------------------------------------------------------- T4 regression
def test_backfill_marks_the_last_bar_of_each_backfilled_minute() -> None:
    d = _downtime()
    assert d.backfilled == 5
    heads = _bar_heads(d.journal.batches()[d.restored_at :])
    assert [(_rel(h), h.get("minute_closed"), h["observed"]) for h in heads] == [
        (("BTCUSDT", 6), None, False),
        (("ETHUSDT", 6), True, False),
        (("BTCUSDT", 7), None, False),
        (("ETHUSDT", 7), True, False),
        (("BTCUSDT", 8), True, False),  # ETH 8 absent: BTC 8 closes minute 8
    ]
    # the durable close equals the clock's skip point
    assert d.svc.session.closed_through_ms == minute(9) == d.svc.clock.next_minute_ms
    # no strategy call, order or entry from any backfilled bar
    assert d.strategy.calls == []
    for b in d.journal.batches()[d.restored_at :]:
        assert not [e.type for e in b[1:] if e.type in DECISIONS]


def test_a_restore_after_the_backfill_keeps_the_backfilled_minutes_closed() -> None:
    """The T4 defect: scenario B (journal copied right after the backfill, restored) must treat
    ETH 8 as late, exactly as the uninterrupted service A does."""
    a, at = _uninterrupted()
    a_batches = a.journal.batches()
    assert _late(a.svc) == [("ETHUSDT", 8)]

    copy = _copy(a_batches[:at])
    sb = ScriptedStrategy(SCRIPT)
    b = PaperTradingService.restore(CFG, copy, strategy=sb)
    assert b.session.last_minute_ms == minute(8)
    assert b.session.closed_through_ms == minute(9)
    assert b.clock.next_minute_ms == minute(9)
    assert b.clock.offer(a.bars[ETH][8]) == "late"
    assert len(copy.batches()) == at  # nothing journaled for it
    _after_backfill(b, a.bars, redeliver_9=True)
    assert ("ETHUSDT", 8) in _late(b)
    assert b.state is ServiceState.RUNNING
    assert b.fault is None
    assert (minute(8), "ETHUSDT") not in sb.calls
    assert sb.calls == a.strategy.calls == [(minute(9), "BTCUSDT"), (minute(9), "ETHUSDT")]
    assert _canonical(copy.batches()) == _canonical(a_batches)
    assert b.ledger_hashes() == a.svc.ledger_hashes()


def test_the_backfilled_journal_replays_to_the_same_ledgers() -> None:
    d = _downtime()
    at = len(d.journal.batches())
    hashes = d.svc.ledger_hashes()
    rep = replay(CFG, d.journal.batches())  # verify=True: every batch reproduced exactly
    assert rep.ledger_hashes() == hashes
    assert rep.closed_through_ms == minute(9)
    _after_backfill(d.svc, d.bars, redeliver_9=False)
    full = replay(CFG, d.journal.batches())
    assert full.ledger_hashes() == d.svc.ledger_hashes()
    assert full.fingerprint() == d.svc.session.fingerprint()
    assert len(d.journal.batches()) == at + 2


def test_a_late_bar_of_a_finalized_recovered_minute_is_rejected() -> None:
    d = _downtime()
    at = len(d.journal.batches())
    before = d.svc.ledger_hashes()
    # a second backfill holding bars of the recovered minutes: all skipped, nothing processed
    again = [d.bars[k][i] for k in (BTC, ETH) for i in (6, 7, 8)]
    assert d.svc.resume_after_downtime(again) == 0
    # the live feed: late
    assert d.svc.clock.offer(d.bars[ETH][7]) == "late"
    assert d.svc.clock.offer(d.bars[ETH][8]) == "late"
    # the session itself refuses them
    with pytest.raises(ValueError, match="already closed"):
        d.svc.session.step_bar(d.bars[ETH][8], observed=False)
    assert len(d.journal.batches()) == at
    assert d.svc.ledger_hashes() == before
    assert d.svc.fault is None


# ---------------------------------------------------------------- second crash
def _fail_on(key: tuple[str, int]) -> Callable[[int, list[JournalEntry]], bool]:
    return lambda _i, batch: _bar_key(batch) == key


@pytest.mark.parametrize(
    "key",
    [("BTCUSDT", 6), ("ETHUSDT", 6), ("BTCUSDT", 7), ("ETHUSDT", 7), ("BTCUSDT", 8)],
)
def test_a_crash_inside_the_backfill_restores_to_the_uninterrupted_journal(
    key: tuple[str, int],
) -> None:
    """The journal write of one backfilled bar fails. The durable journal keeps only the minutes
    whose closing bar was written; the restored service re-awaits the rest, takes the same backfill
    and ends with the uninterrupted journal and ledgers, each bar journaled once."""
    a, _ = _uninterrupted()
    d = _downtime(_fail_on(key))
    assert d.svc.state is ServiceState.FAULTED
    d.journal.fail_when = None
    durable = len(d.journal.batches())
    assert _canonical(d.journal.batches()) == _canonical(a.journal.batches()[:durable])
    closed = d.journal.batches()[-1][0].payload.get("minute_closed") is True
    strategy = ScriptedStrategy(SCRIPT)
    c = PaperTradingService.restore(CFG, d.journal, strategy=strategy)
    last = c.session.last_minute_ms
    assert last is not None
    assert c.session.closed_through_ms == (last + M if closed else last)
    closed_at_restore = c.session.closed_through_ms
    drive(c, d.bars, [9])
    assert c.state is ServiceState.AWAITING_BACKFILL
    assert c.resume_after_downtime([d.bars[k][i] for k, i in BACKFILL]) == 5 - (
        durable - d.restored_at
    )
    assert c.session.closed_through_ms == minute(9)
    _after_backfill(c, d.bars, redeliver_9=False)
    assert c.state is ServiceState.RUNNING  # the next poll finds no gap, as in A
    assert c.fault is None
    assert ("ETHUSDT", 8) in _late(c)
    keys = [_bar_key(b) for b in d.journal.batches() if _bar_key(b) is not None]
    assert len(keys) == len(set(keys))  # no duplicate journal effect
    assert _canonical(d.journal.batches()) == _canonical(a.journal.batches())
    assert c.ledger_hashes() == a.svc.ledger_hashes()
    assert strategy.calls == a.strategy.calls
    # a restart after downtime: bars that closed before the first live minute (9) only from the
    # backfill, unobserved; the downtime began after the poll of minute 5
    assert (
        restart_causality(
            d.journal.batches(),
            durable,
            minute(10),
            closed_at_restore,
            live_from_ms=minute(9),
            down_from_ms=minute(6),
        )
        == []
    )


def test_a_crash_right_after_the_backfill_restores_to_the_uninterrupted_journal() -> None:
    """The backfill is durable; the next write (BTC 9, live) fails. The restored service starts
    after the backfilled minutes: ETH 8 is late, minute 9 is processed once, as uninterrupted."""
    a, at = _uninterrupted()
    d = _downtime(_fail_on(("BTCUSDT", 9)))
    assert d.backfilled == 5
    assert d.svc.fault is None
    _after_backfill(d.svc, d.bars, redeliver_9=False)
    assert d.svc.state is ServiceState.FAULTED
    d.journal.fail_when = None
    assert len(d.journal.batches()) == at
    strategy = ScriptedStrategy(SCRIPT)
    c = PaperTradingService.restore(CFG, d.journal, strategy=strategy)
    assert c.session.closed_through_ms == minute(9)
    _after_backfill(c, d.bars, redeliver_9=True)
    assert c.state is ServiceState.RUNNING
    assert ("ETHUSDT", 8) in _late(c)
    assert (minute(8), "ETHUSDT") not in strategy.calls
    assert strategy.calls == a.strategy.calls
    assert _canonical(d.journal.batches()) == _canonical(a.journal.batches())
    assert c.ledger_hashes() == a.svc.ledger_hashes()
    assert restart_causality(d.journal.batches(), at, minute(10), minute(9)) == []


# ---------------------------------------------------------------- historical journals
def _strip_backfill_marks(batches: Sequence[Batch], start: int) -> MemoryJournal:
    """The journal as code before M3-PB.4 wrote it: no mark on the backfilled (unobserved) bars."""
    out = MemoryJournal()
    for i, batch in enumerate(batches):
        head = batch[0]
        payload = dict(head.payload)
        if i >= start and payload["kind"] == "bar" and payload["observed"] is False:
            payload.pop("minute_closed", None)
        out.append(
            [
                JournalEntry(
                    head.type, head.aggregate_type, head.aggregate_id, head.ts_ms, payload
                ),
                *batch[1:],
            ]
        )
    return out


def test_a_backfill_journal_without_marks_keeps_the_previous_reading() -> None:
    """A journal written before M3-PB.4 (backfilled bars unmarked) still replays and verifies to
    the same ledgers, and restores with the reading it always had: closed through the last live
    close (minute 6), restarted at the last backfilled minute with BTC 8 processed. ETH 8 is then
    still on time for that clock (the legacy behaviour this change removes for new journals)."""
    d = _downtime()
    legacy = _strip_backfill_marks(d.journal.batches(), d.restored_at)
    heads = _bar_heads(legacy.batches()[d.restored_at :])
    assert len(heads) == 5
    assert all("minute_closed" not in h and h["observed"] is False for h in heads)
    rep = replay(CFG, legacy.batches())
    assert rep.ledger_hashes() == d.svc.ledger_hashes()
    assert rep.closed_through_ms == minute(6)
    old = PaperTradingService.restore(CFG, legacy, strategy=ScriptedStrategy(SCRIPT))
    assert old.session.closed_through_ms == minute(6)
    assert old.session.last_minute_ms == minute(8)
    assert old.clock.offer(d.bars[BTC][8]) == "late"  # processed before the restart
    assert old.clock.offer(d.bars[ETH][8]) == "buffered"  # minute 8 open: the previous reading


def test_a_live_only_journal_carries_no_new_marks() -> None:
    """Live sessions are unchanged: marks only where the live clock closed a minute."""
    _svc, journal, _ = run_random_session(13073)
    heads = _bar_heads(journal.batches())
    assert all(h["observed"] is True for h in heads)
    for i, h in enumerate(heads):
        nxt = heads[i + 1]["bar"]["open_time_ms"] if i + 1 < len(heads) else None
        assert h.get("minute_closed") is (True if nxt != h["bar"]["open_time_ms"] else None)


# ---------------------------------------------------------------- OD-1 characterisation
EMPTY_MINUTE = {
    130741648: (
        8,
        4,
        [
            "ledgers differ after restore at batch 8",
            "journal differs after restore at batch 8",
            "awaiting backfill: ['*', 'spot:BTCUSDT', 'spot:ETHUSDT']",
        ],
    ),
    50553820: (
        63,
        34,
        [
            "journal differs after restore at batch 63",
            "awaiting backfill: ['*', 'spot:BTCUSDT', 'spot:ETHUSDT']",
        ],
    ),
}


@pytest.mark.parametrize("seed", sorted(EMPTY_MINUTE))
def test_known_empty_minute_counterexamples_are_the_only_failing_crash_points(seed: int) -> None:
    """KNOWN FAIL-CLOSED COUNTEREXAMPLE (OD-1), not a successful recovery: a minute released with
    no bar at all leaves no input entry to carry its close. Swept over every crash point of the
    seed's session (instant restart), the strict property fails at exactly one point, with exactly
    these problems; every other crash point restores equivalently."""
    _svc, ref_journal, _ = run_random_session(seed)
    failing = {}
    for fb in range(len(ref_journal.batches())):
        problems = check_restore_equivalence(seed, fb)
        if problems:
            failing[fb] = problems
    point, _empty, problems = EMPTY_MINUTE[seed]
    assert failing == {point: problems}


@pytest.mark.parametrize("seed", sorted(EMPTY_MINUTE))
def test_known_empty_minute_counterexample_blocks_without_appending(seed: int) -> None:
    """KNOWN FAIL-CLOSED COUNTEREXAMPLE (OD-1), not a successful recovery: at the failing crash
    point the restored service keeps exactly the durable prefix of the uninterrupted journal,
    appends nothing after the re-delivery, and waits for a backfill of the empty minute, without
    a fault. For 130741648 the ledgers diverge (the uninterrupted session filled entries after the
    empty minute; baseline and shadow differ); for 50553820 they happen to be equal."""
    point, empty, _ = EMPTY_MINUTE[seed]
    r = restore_after_crash(seed, point)
    assert not isinstance(r, str)
    assert _canonical(r.journal.batches()) == _canonical(r.ref_journal.batches()[: r.durable])
    redeliver(r)
    assert len(r.journal.batches()) == r.durable  # nothing appended
    assert _canonical(r.journal.batches()) == _canonical(r.ref_journal.batches()[: r.durable])
    assert r.restored.state is ServiceState.AWAITING_BACKFILL
    assert r.restored.fault is None
    assert r.restored.awaiting_missing["*"] == [minute(empty)]
    got, want = r.restored.ledger_hashes(), r.ref.ledger_hashes()
    if seed == 130741648:
        assert got["baseline"] != want["baseline"]
        assert got["shadow"] != want["shadow"]
        assert got["benchmark"] == want["benchmark"]
    else:
        assert got == want


def test_the_empty_minutes_have_no_bar_at_all() -> None:
    """The two counterexamples are the empty-minute class: neither instrument has a bar there."""
    for seed, (_point, empty, _p) in EMPTY_MINUTE.items():
        _svc, _journal, ctx = run_random_session(seed)
        assert {(empty, "BTCUSDT"), (empty, "ETHUSDT")} <= ctx["missing"]
