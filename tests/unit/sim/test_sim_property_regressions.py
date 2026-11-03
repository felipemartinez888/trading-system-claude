"""Pinned regressions for two defects found by the Hypothesis properties in
`tests/property/sim/test_paper_properties.py` (recorded failing examples, kept verbatim).

1. Live clock: the first `release()` committed the clock position to the earliest buffered minute
   even though that minute was neither complete nor due, so an earlier minute that was still in
   flight (within its own settle window) was then classified late.
2. Paper restore: after a restart, a minute in which an instrument's bar never arrived *before the
   crash* (a gap the books already handled; the delivery key is behind the journal's last key) was
   treated as unobserved downtime, so the service waited for a backfill that can never be applied.

3. Paper restore, Part B class 1 (seed 13073): a crash after the live clock had closed a minute
   by its deadline, without an instrument whose bar never arrived. The journal could not tell
   that instrument from one not processed yet, so the restored service waited for a backfill that
   cannot exist. The input entry of the last bar released for a minute now carries
   ``minute_closed: true``; journals without the mark keep the old, fail-closed reading (tested).

4. Restore property harness (M3-PB.2, causality audit F1): the re-delivery polled the restored
   service at the original wall times, some before the crash, which no restart can do; seeds
   13073 and 465923499 diverged only through that. The harness now never polls before the crash
   instant, and both examples reproduce the uninterrupted journal and ledgers.

Pinned as characterisations: the pre-crash timeline of 4 (kept, to show what it did), and a
minute the clock released with no bar at all (not fixed, fails closed).
"""

from __future__ import annotations

import dataclasses
import random

import pytest
from paper_helpers import BTC, ETH, config, drive, new_service, two_symbol_bars
from paper_scenarios import (
    BarStore,
    Restart,
    bar_store,
    check_live_clock_order,
    check_replay_parity,
    check_restore_equivalence,
    random_bars,
    random_script,
    reconnect,
    recovery_outcome,
    restart_causality,
    restore_after_crash,
    run_random_session,
)

from tsclaude.contracts.market import Bar, Market
from tsclaude.sim.journal import Batch, JournalEntry, MemoryJournal
from tsclaude.sim.live import LiveClock
from tsclaude.sim.paper import PaperTradingService, RestoreError, ServiceState
from tsclaude.sim.testing import minute

M = 60_000


# ---------------------------------------------------------------- live clock
def test_recorded_live_clock_example_releases_the_historical_order() -> None:
    """test_live_clock_releases_historical_order: seed=118309, settle_ms=111026."""
    assert check_live_clock_order(118309, 111026) == []


def test_a_waiting_minute_does_not_fix_the_clock_position() -> None:
    from paper_helpers import two_symbol_bars

    bars = two_symbol_bars(2)
    clock = LiveClock([BTC, ETH], settle_ms=120_000)
    assert clock.offer(bars[BTC][1]) == "buffered"  # minute 1 arrives first
    assert clock.release(minute(2) + 5_000) == []  # incomplete, not due: nothing released ...
    assert clock.next_minute_ms is None  # ... and the position is still open
    assert clock.offer(bars[BTC][0]) == "buffered"  # minute 0 is on time, not late
    assert clock.offer(bars[ETH][0]) == "buffered"
    out = clock.release(minute(2) + 6_000)
    assert [(b.symbol, b.open_time_ms) for b in out] == [
        ("BTCUSDT", minute(0)),
        ("ETHUSDT", minute(0)),
    ]  # strictly chronological: minute 0 before the minute-1 bar that arrived earlier
    assert clock.late == []
    assert clock.next_minute_ms == minute(1)
    assert clock.offer(bars[ETH][1]) == "buffered"
    assert [(b.symbol, b.open_time_ms) for b in clock.release(minute(2) + 7_000)] == [
        ("BTCUSDT", minute(1)),
        ("ETHUSDT", minute(1)),
    ]


def test_a_bar_behind_the_released_position_is_still_late() -> None:
    """The fix defers the commitment only until the first release; afterwards the existing
    rule holds: a released minute is closed and a re-delivery or straggler is late."""
    from paper_helpers import two_symbol_bars

    bars = two_symbol_bars(2)
    clock = LiveClock([BTC, ETH], settle_ms=1_000)
    clock.offer(bars[BTC][1])
    assert [b.open_time_ms for b in clock.release(minute(2) + 1_000)] == [minute(1)]  # due
    assert clock.offer(bars[BTC][0]) == "late"
    assert clock.late == [bars[BTC][0]]


def test_explicit_start_resolves_the_undecidable_start_of_session_cases() -> None:
    """With no declared start a clock cannot tell a session that begins at minute 1 from minute 1
    completing before minute 0's bars arrive (inherent: three seeds of 6,000 random cases, listed
    here). A declared `start_ms` removes the ambiguity; this is what a production wiring passes."""
    for seed, settle_ms in [(715208808, 100_000), (14685346, 119_999), (487386866, 119_999)]:
        assert check_live_clock_order(seed, settle_ms, start_ms=minute(0)) == []


# ---------------------------------------------------------------- paper restore
def test_recorded_restore_example_reproduces_the_uninterrupted_ledgers() -> None:
    """test_restore_after_any_journal_failure: seed=23511, fail_batch=118."""
    assert check_restore_equivalence(23511, 118) == []


def _crash_and_restore(
    seed: int, fail_batch: int
) -> tuple[PaperTradingService, PaperTradingService, dict, dict]:
    """(uninterrupted reference, restored service, bars, context) for one journal failure; the
    construction mirrors `check_restore_equivalence` and stops before the re-delivery."""
    ref, ref_journal, ctx = run_random_session(seed)
    fail_at = fail_batch % len(ref_journal.batches())
    rng = random.Random(seed)
    bars = random_bars(rng, 40)
    strat = random_script(rng, bars, 40)
    journal = MemoryJournal(fail_when=lambda i, _b: i == fail_at)
    svc, _ = new_service(strat, journal=journal)
    drive(svc, bars, list(range(40)), missing=ctx["missing"])
    assert svc.fault is not None
    journal.fail_when = None
    restored = PaperTradingService.restore(config(), journal, strategy=strat, tracker=svc.tracker)
    return ref, restored, bars, ctx


def _restored_after_journal_failure() -> tuple[PaperTradingService, dict, dict]:
    """Seed 23511: BTC's minute-21 bar never arrived (a feed gap while running); ETH's did, and was
    the last journalled bar; the next write (BTC minute 22) failed."""
    _ref, restored, bars, ctx = _crash_and_restore(23511, 118)
    assert (21, "BTCUSDT") in ctx["missing"]
    assert restored.session.last_minute_ms == minute(21)
    # the gap matters: both instruments are exposed in the shadow and benchmark books
    assert restored.session.sim.books["shadow"].exposed()
    assert restored.session.sim.books["benchmark"].exposed()
    return restored, bars, ctx


def test_a_gap_the_session_already_passed_is_not_downtime() -> None:
    restored, bars, ctx = _restored_after_journal_failure()
    drive(restored, bars, list(range(21, 40)), missing=ctx["missing"])
    assert restored.state is ServiceState.RUNNING
    assert restored.awaiting_missing == {}
    assert restored.fault is None


def test_real_downtime_after_the_journal_still_needs_a_backfill() -> None:
    """Fail closed is unchanged: minutes after the last journalled key that the restarted service
    never saw are unobserved; live bars wait and nothing is processed."""
    restored, bars, _ = _restored_after_journal_failure()
    before = len(restored.journal.batches())
    drive(restored, bars, [24, 25])  # minutes 22 and 23 were lost to the restart
    assert restored.state is ServiceState.AWAITING_BACKFILL
    gaps = {k: [(m - minute(0)) // M for m in v] for k, v in restored.awaiting_missing.items()}
    assert gaps["spot:BTCUSDT"] == [22, 23]  # minute 21 is not in the list: it was passed live
    assert gaps["spot:ETHUSDT"] == [22, 23]
    assert gaps["*"] == [22, 23]
    assert len(restored.journal.batches()) == before  # nothing processed while awaiting


# ---------------------------------------------------------------- Part B: recorded minute close
def _heads(journal: MemoryJournal) -> list[dict]:
    return [b[0].payload for b in journal.batches()]


def _bar_heads(journal: MemoryJournal) -> list[dict]:
    return [h for h in _heads(journal) if h["kind"] == "bar"]


def _rebuilt(batches: list[Batch], edit) -> MemoryJournal:  # type: ignore[no-untyped-def]
    """A new journal holding `batches` with `edit(index, head payload)` applied to each input
    entry (the stored form of a journal written by other code, or edited)."""
    out = MemoryJournal()
    for i, batch in enumerate(batches):
        head = batch[0]
        payload = edit(i, dict(head.payload))
        new_head = JournalEntry(
            head.type, head.aggregate_type, head.aggregate_id, head.ts_ms, payload
        )
        out.append([new_head, *batch[1:]])
    return out


def _without_close_marks(journal: MemoryJournal) -> MemoryJournal:
    """The same journal as code before Part B wrote it: no input entry has ``minute_closed``."""

    def strip(_i: int, p: dict) -> dict:
        p.pop("minute_closed", None)
        return p

    return _rebuilt(journal.batches(), strip)


def test_seed_13073_the_minute_closed_before_the_crash_is_not_downtime() -> None:
    """seed=13073, fail_batch=124: minute 25 was released at its deadline with BTC only (ETH 25
    never arrived), BTC 25 was the last durable batch, the next write failed. BTC 25's input entry
    records the close, so ETH 25 is a gap the session passed, not downtime: the restored service
    runs on with no backfill and never processes an ETH 25 bar."""
    ref, restored, bars, ctx = _crash_and_restore(13073, 124)
    assert restored.session.last_minute_ms == minute(25)
    assert (25, "ETHUSDT") in ctx["missing"]
    last = _bar_heads(restored.journal)[-1]
    assert (last["bar"]["symbol"], last["minute_closed"]) == ("BTCUSDT", True)
    # released at the deadline (close + 90 s settle), with ETH 25 absent: not complete
    assert last["release_latency_ms"] == 121_500 >= 90_000
    assert restored.session.closed_through_ms == minute(26)
    assert restored.clock.next_minute_ms == minute(26)
    assert restored.missing_before(minute(26)) == {}
    drive(restored, bars, list(range(25, 40)), missing=ctx["missing"])
    assert restored.state is ServiceState.RUNNING
    assert restored.awaiting_missing == {}
    assert restored.fault is None
    assert [(b.symbol, b.open_time_ms) for b in restored.clock.late] == [("BTCUSDT", minute(25))]
    processed = [
        (h["bar"]["symbol"], h["bar"]["open_time_ms"]) for h in _bar_heads(restored.journal)
    ]
    assert ("ETHUSDT", minute(25)) not in processed
    assert processed == [
        (h["bar"]["symbol"], h["bar"]["open_time_ms"]) for h in _bar_heads(ref.journal)
    ]  # the same bars in the same order as the uninterrupted session


def test_seed_13073_still_differs_by_release_latency_only() -> None:
    """Characterisation of the pre-crash timeline (causality audit F1), kept as it was. Re-driven
    at the original wall times, the restored service releases minute 26 at minute 27 + 1.5 s,
    although the failed write happened in the poll at minute 28 + 1.5 s: a release no restart can
    make. The uninterrupted session released minute 26 at 61.5 s (behind minute 25's deadline),
    the restored one at 1.5 s. Release time bounds execution (`not_before_ms`), so the shadow book
    differs; baseline and benchmark do not.

    Under the restart timeline of M3-PB.2 (never polled before the crash), the same case is
    equivalent: the last assertion, which before M3-PB.2 pinned the property's failure here."""
    ref, restored, bars, ctx = _crash_and_restore(13073, 124)
    drive(restored, bars, list(range(25, 40)), missing=ctx["missing"])
    lat = {
        (h["bar"]["symbol"], h["bar"]["open_time_ms"]): h["release_latency_ms"]
        for h in _bar_heads(restored.journal)
    }
    ref_lat = {
        (h["bar"]["symbol"], h["bar"]["open_time_ms"]): h["release_latency_ms"]
        for h in _bar_heads(ref.journal)
    }
    assert (lat[("BTCUSDT", minute(26))], ref_lat[("BTCUSDT", minute(26))]) == (1_500, 61_500)
    got, want = restored.ledger_hashes(), ref.ledger_hashes()
    assert got["baseline"] == want["baseline"]
    assert got["benchmark"] == want["benchmark"]
    assert got["shadow"] != want["shadow"]
    assert check_restore_equivalence(13073, 124) == []


def test_a_journal_without_close_marks_keeps_the_fail_closed_reading() -> None:
    """Legacy journals (written before Part B) carry no ``minute_closed``. They still replay and
    verify unchanged, and the original characterisation of seed 13073 holds for them verbatim:
    ETH 25 is a key after `last_key` that might be unprocessed, so the service waits for a
    backfill and processes nothing."""
    ref, crashed, bars, ctx = _crash_and_restore(13073, 124)
    legacy = _without_close_marks(crashed.journal)
    assert all("minute_closed" not in h for h in _heads(legacy))
    restored = PaperTradingService.restore(
        config(), legacy, strategy=crashed.session.source.strategy
    )
    assert restored.session.closed_through_ms is None
    assert restored.session.last_minute_ms == minute(25)
    before = len(restored.journal.batches())
    drive(restored, bars, list(range(25, 40)), missing=ctx["missing"])
    assert restored.state is ServiceState.AWAITING_BACKFILL
    assert list(restored.awaiting_missing) == ["spot:ETHUSDT"]
    assert len(restored.journal.batches()) == before  # fail closed: nothing processed
    assert restored.ledger_hashes() != ref.ledger_hashes()


def test_real_downtime_after_a_closed_minute_still_needs_a_backfill() -> None:
    """Fail closed is unchanged after a recorded close: minutes 26 and 27 were lost to the
    restart, so live bars wait and nothing is processed; the closed minute 25 is not listed."""
    _ref, restored, bars, ctx = _crash_and_restore(13073, 124)
    before = len(restored.journal.batches())
    drive(restored, bars, [28, 29], missing=ctx["missing"])
    assert restored.state is ServiceState.AWAITING_BACKFILL
    gaps = {k: [(m - minute(0)) // M for m in v] for k, v in restored.awaiting_missing.items()}
    assert gaps == {"spot:BTCUSDT": [26, 27], "spot:ETHUSDT": [26, 27], "*": [26, 27]}
    assert len(restored.journal.batches()) == before


def test_a_backfilled_bar_of_a_closed_minute_is_late() -> None:
    """A backfill that holds the bar the live clock released the minute without (here ETH 25,
    as REST could recover it later) does not reopen the minute: it is skipped, as it would be
    late live, and the rest of the backfill resolves the downtime."""
    _ref, restored, bars, ctx = _crash_and_restore(13073, 124)
    drive(restored, bars, [28], missing=ctx["missing"])
    assert restored.state is ServiceState.AWAITING_BACKFILL
    backfill = [bars[k][i] for i in (25, 26, 27) for k in (BTC, ETH)]
    # BTC 25 was journaled before the crash (at `last_key`) and ETH 25 is in the closed minute:
    # both are skipped, and only the four bars of minutes 26 and 27 are processed
    assert restored.resume_after_downtime(backfill) == 4
    assert restored.fault is None
    heads = _bar_heads(restored.journal)
    assert [
        (h["bar"]["symbol"], (h["bar"]["open_time_ms"] - minute(0)) // M) for h in heads[-4:]
    ] == [
        ("BTCUSDT", 26),
        ("ETHUSDT", 26),
        ("BTCUSDT", 27),
        ("ETHUSDT", 27),
    ]
    assert all(h["observed"] is False and "minute_closed" not in h for h in heads[-4:])
    assert restored.poll(minute(29) + 1_500) >= 1
    assert restored.state is ServiceState.RUNNING


def _minute_split_crash(redeliver: bool) -> tuple[PaperTradingService, PaperTradingService]:
    """Minute 10 completes (both bars released in one poll); BTC 10 is written, the ETH 10 write
    fails. Minute 10 has no recorded close, so ETH 10 is still to be processed after the restart.
    Returns (uninterrupted reference, restored service after minutes 10-14 are re-delivered)."""
    bars = two_symbol_bars(16)
    ref, _ = new_service()
    drive(ref, bars, list(range(15)))

    def fail(_i: int, batch: list[JournalEntry]) -> bool:
        b = batch[0].payload.get("bar") or {}
        return (b.get("symbol"), b.get("open_time_ms")) == ("ETHUSDT", minute(10))

    journal = MemoryJournal(fail_when=fail)
    svc, _ = new_service(journal=journal)
    drive(svc, bars, list(range(15)))
    assert svc.fault is not None
    journal.fail_when = None
    restored = PaperTradingService.restore(config(), journal, tracker=svc.tracker)
    assert restored.session.last_minute_ms == minute(10)
    assert restored.session.closed_through_ms == minute(10)  # minute 9 closed, minute 10 not
    missing = set() if redeliver else {(10, "ETHUSDT")}
    drive(restored, bars, list(range(10, 15)), missing=missing)
    return ref, restored


def test_a_minute_without_a_recorded_close_still_waits_for_its_bar() -> None:
    """The mark is written with the closing bar, atomically: a crash before it leaves the minute
    open, so a lost ETH 10 is downtime (fail closed), never a passed gap."""
    _ref, restored = _minute_split_crash(redeliver=False)
    assert restored.state is ServiceState.AWAITING_BACKFILL
    assert restored.awaiting_missing["spot:ETHUSDT"] == [minute(10)]


def test_a_minute_without_a_recorded_close_resumes_on_its_redelivered_bar() -> None:
    ref, restored = _minute_split_crash(redeliver=True)
    assert restored.state is ServiceState.RUNNING
    assert restored.ledger_hashes() == ref.ledger_hashes()
    eth10 = [
        h
        for h in _bar_heads(restored.journal)
        if (h["bar"]["symbol"], h["bar"]["open_time_ms"]) == ("ETHUSDT", minute(10))
    ]
    assert len(eth10) == 1
    assert eth10[0]["minute_closed"] is True


def test_only_the_last_bar_released_for_a_minute_carries_the_close_mark() -> None:
    """In a random live session every released minute is closed by exactly its last bar batch,
    the mark is never anything but ``true``, and the journal still replays to identical batches."""
    _svc, journal, _ = run_random_session(13073)
    heads = _bar_heads(journal)
    for i, h in enumerate(heads):
        nxt = heads[i + 1]["bar"]["open_time_ms"] if i + 1 < len(heads) else None
        closes = nxt != h["bar"]["open_time_ms"]
        assert h.get("minute_closed") is (True if closes else None)
    assert check_replay_parity(13073) == []


def test_a_bar_after_its_recorded_close_refuses_the_restore() -> None:
    """A journal claiming a minute closed before another bar of it (impossible live: the clock
    would have rejected that bar as late) is refused whole: fail closed, no partial restore."""
    _svc, journal, _ = run_random_session(13073)
    batches = journal.batches()
    target = next(
        i
        for i, b in enumerate(batches[:-1])
        if b[0].payload["kind"] == "bar"
        and b[0].payload["bar"]["symbol"] == "BTCUSDT"
        and batches[i + 1][0].payload.get("bar", {}).get("open_time_ms")
        == b[0].payload["bar"]["open_time_ms"]
    )

    def mark(i: int, p: dict) -> dict:
        if i == target:
            p["minute_closed"] = True
        return p

    with pytest.raises(RestoreError, match="recorded closed"):
        PaperTradingService.restore(config(), _rebuilt(batches, mark))


def test_a_close_mark_with_another_value_refuses_the_restore() -> None:
    _svc, journal, _ = run_random_session(13073)
    batches = journal.batches()
    target = next(i for i, b in enumerate(batches) if b[0].payload.get("minute_closed") is True)

    def mark(i: int, p: dict) -> dict:
        if i == target:
            p["minute_closed"] = "true"
        return p

    with pytest.raises(RestoreError, match="diverged"):
        PaperTradingService.restore(config(), _rebuilt(batches, mark))


def test_the_session_refuses_a_bar_of_a_minute_already_closed() -> None:
    """Review N-2: the step guard. No live path reaches it (the restored clock starts after a
    closed minute, and backfill skips its bars), so it is driven directly: the session refuses
    the bar before changing anything, and through the service the step faults, fail closed,
    without journaling anything."""
    bars = two_symbol_bars(6)
    svc, journal = new_service()
    drive(svc, bars, list(range(4)))
    assert svc.session.closed_through_ms == minute(4)
    before = (svc.session.batches, len(journal.batches()), svc.ledger_hashes())
    with pytest.raises(ValueError, match="already closed"):
        svc.session.step_bar(bars[ETH][3], observed=True)
    assert (svc.session.batches, len(journal.batches()), svc.ledger_hashes()) == before
    assert svc._process(lambda: svc.session.step_bar(bars[BTC][2], observed=True)) is False
    assert svc.state is ServiceState.FAULTED
    assert svc.fault is not None
    assert "already closed" in svc.fault
    assert len(journal.batches()) == before[1]


# A single poll releases minutes 0-5 at once: BTC 2 and ETH 3 never arrive, so minute 2 is closed
# by ETH 2 and minute 3 by BTC 3; the others by ETH. Every poll below is at or after the previous
# one, so the restored service is never polled before the crash.
_SPLIT_MISSING = {(2, "BTCUSDT"), (3, "ETHUSDT")}
_SPLIT_RELEASE_MS = minute(6) + 90_000  # minute 5's deadline (close + 1 ms + 90 s settle)


def _offer_minutes(svc: PaperTradingService, bars: dict, minutes: range) -> None:
    from tsclaude.marketdata.bars import BarClosed

    for i in minutes:
        for key in (ETH, BTC):
            if (i, key[1]) not in _SPLIT_MISSING:
                svc.on_update(BarClosed(bars[key][i]))


def _multi_minute_session(svc: PaperTradingService, bars: dict) -> None:
    _offer_minutes(svc, bars, range(6))
    svc.poll(_SPLIT_RELEASE_MS)
    _offer_minutes(svc, bars, range(6, 7))
    drive(svc, bars, list(range(7, 14)))


@pytest.mark.parametrize(
    ("symbol", "at"),
    [("ETHUSDT", 2), ("BTCUSDT", 3), ("BTCUSDT", 4), ("ETHUSDT", 4), ("ETHUSDT", 5)],
)
def test_a_crash_inside_a_multi_minute_release_restores_equivalently(symbol: str, at: int) -> None:
    """Review N-4: a journal write fails at (symbol, minute) inside one release of six minutes.
    Only the minutes whose closing bar was journaled count as closed; the rest is re-delivered
    and processed once, with the same marks, bars and ledgers as the uninterrupted session, and
    the re-delivered bars of durable minutes are late, never processed twice."""
    bars = two_symbol_bars(14)
    ref, ref_journal = new_service()
    _multi_minute_session(ref, bars)
    closing = [
        (h["bar"]["symbol"], (h["bar"]["open_time_ms"] - minute(0)) // M)
        for h in _bar_heads(ref_journal)[:10]
        if h.get("minute_closed")
    ]
    assert closing == [
        ("ETHUSDT", 0),
        ("ETHUSDT", 1),
        ("ETHUSDT", 2),  # BTC 2 never arrived
        ("BTCUSDT", 3),  # ETH 3 never arrived
        ("ETHUSDT", 4),
        ("ETHUSDT", 5),
    ]

    def fail(_i: int, batch: list[JournalEntry]) -> bool:
        b = batch[0].payload.get("bar") or {}
        return (b.get("symbol"), b.get("open_time_ms")) == (symbol, minute(at))

    journal = MemoryJournal(fail_when=fail)
    svc, _ = new_service(journal=journal)
    _multi_minute_session(svc, bars)
    assert svc.fault is not None
    journal.fail_when = None
    durable = len(journal.batches())
    restored = PaperTradingService.restore(config(), journal, tracker=svc.tracker)
    last_head = _bar_heads(journal)[-1]
    closed = last_head.get("minute_closed") is True
    last = minute((last_head["bar"]["open_time_ms"] - minute(0)) // M)
    assert restored.session.closed_through_ms == (last + M if closed else last)
    _multi_minute_session(restored, bars)  # re-delivers everything, at the same wall times
    assert restored.state is ServiceState.RUNNING
    assert restored.fault is None
    assert [e.canonical() for b in journal.batches()[durable:] for e in b] == [
        e.canonical() for b in ref_journal.batches()[durable:] for e in b
    ]
    assert restored.ledger_hashes() == ref.ledger_hashes()
    late = {(b.symbol, b.open_time_ms) for b in restored.clock.late}
    for h in _bar_heads(journal)[:durable]:
        key = (h["bar"]["symbol"], h["bar"]["open_time_ms"])
        if h["bar"]["open_time_ms"] < minute(6):
            assert key in late  # re-delivered, refused as late, not processed again


def test_a_minute_released_without_any_bar_still_blocks() -> None:
    """Characterisation, NOT a fix (seed=130741648, fail_batch=8): minute 4 had no bar at all,
    so no input entry exists to record its close, and the crash came before minute 5's first
    batch. The restored service cannot tell minute 4 passed from minute 4 lost, and fails closed.
    Recording that would need a journal entry without a bar (a new input kind): not done here."""
    ref, restored, bars, ctx = _crash_and_restore(130741648, 8)
    assert {(4, "BTCUSDT"), (4, "ETHUSDT")} <= ctx["missing"]
    assert restored.session.last_minute_ms == minute(3)
    assert restored.session.closed_through_ms == minute(4)
    drive(restored, bars, list(range(3, 40)), missing=ctx["missing"])
    assert restored.state is ServiceState.AWAITING_BACKFILL
    assert restored.awaiting_missing["*"] == [minute(4)]
    assert restored.ledger_hashes() != ref.ledger_hashes()


def test_release_latency_changes_the_shadow_book_across_a_restart() -> None:
    """Characterisation (seed=465923499, fail_batch=162): the shadow book never fills before the
    wall time the bar was released (`not_before_ms`), and a restart changes that time (the
    redelivered ETH minute 7 is released 1.5 s after its close instead of 61.5 s, because minute 6
    is no longer pending). The baseline and benchmark ledgers are identical; only the shadow
    ledger differs. This is the pre-crash timeline (causality audit F1), not a restore or
    simulator defect: the failed write happened in the poll at minute 9 + 1.5 s, and this
    re-delivery releases ETH 7 at minute 8 + 1.5 s, before the crash. Under the restart timeline
    of M3-PB.2 the case is equivalent
    (`test_an_instant_restart_reproduces_the_uninterrupted_journal`)."""
    ref, restored, bars, ctx = _crash_and_restore(465923499, 162)
    start = (restored.session.last_minute_ms - minute(0)) // M
    drive(restored, bars, list(range(start, 40)), missing=ctx["missing"])
    assert restored.state is ServiceState.RUNNING
    got, want = restored.ledger_hashes(), ref.ledger_hashes()
    assert got["baseline"] == want["baseline"]
    assert got["benchmark"] == want["benchmark"]
    assert got["shadow"] != want["shadow"]


# ---------------------------------------------------------------- restart timeline (M3-PB.2)
# the two examples the pre-crash timeline made diverge (causality audit F1); the property's own
# examples (`tests/property/sim/test_paper_properties.py`) are the same pairs
KNOWN_RESTARTS = [(13073, 124), (465923499, 162)]


def _crash_instant(seed: int, fail_batch: int) -> tuple[int, int]:
    """(failed batch, crash instant): the wall time of the poll whose write fails, read from the
    uninterrupted journal, which is identical up to that batch."""
    _svc, ref_journal, _ = run_random_session(seed)
    fail_at = fail_batch % len(ref_journal.batches())
    return fail_at, ref_journal.batches()[fail_at][0].payload["released_at_ms"]


def test_the_crash_instant_is_the_poll_of_the_failed_write() -> None:
    assert _crash_instant(13073, 124) == (49, minute(28) + 1_499)
    assert _crash_instant(465923499, 162) == (14, minute(9) + 1_499)


@pytest.mark.parametrize(("seed", "fail_batch"), KNOWN_RESTARTS)
def test_an_instant_restart_reproduces_the_uninterrupted_journal(
    seed: int, fail_batch: int
) -> None:
    """Never polled before the crash, the restored service journals the same bars, in the same
    order, with the same release times, marks and events, and ends with the same ledgers."""
    assert check_restore_equivalence(seed, fail_batch) == []


@pytest.mark.parametrize(("seed", "fail_batch"), KNOWN_RESTARTS)
def test_the_pre_crash_timeline_is_what_broke_equivalence(seed: int, fail_batch: int) -> None:
    """The same restore, re-driven at the original wall times, releases bars before the crash
    and diverges; held at the crash instant, it is causal and equal to the uninterrupted run."""
    _fail_at, crash = _crash_instant(seed, fail_batch)
    for not_before, equal in ((None, False), (crash, True)):
        ref, restored, bars, ctx = _crash_and_restore(seed, fail_batch)
        durable = len(restored.journal.batches())
        closed = restored.session.closed_through_ms
        start = (restored.session.last_minute_ms - minute(0)) // M
        drive(
            restored, bars, list(range(start, 40)), missing=ctx["missing"], not_before_ms=not_before
        )
        problems = restart_causality(restored.journal.batches(), durable, crash, closed)
        assert (restored.ledger_hashes() == ref.ledger_hashes()) is equal
        assert (problems == []) is equal
        if not equal:
            assert any("before" in p and "released at" in p for p in problems)


@pytest.mark.parametrize(
    ("seed", "fail_batch", "delay_ms", "expected"),
    [
        (13073, 124, 30_000, []),
        (13073, 124, 600_000, []),
        (465923499, 162, 30_000, []),
        (465923499, 162, 600_000, []),
        (23511, 118, 600_000, []),
        (853832589, 176, 600_000, []),
        # the shrunk property counterexample: a restart at the open itself, then 1 ms later
        (0, 1, 58_501, []),
        (0, 1, 58_502, ["benchmark:benchmark:BTCUSDT:entry filled at an open during the downtime"]),
        # production limitation P-1 (see the partly observed minute tests): reported, not fixed.
        # At an open during the downtime:
        (
            853832589,
            176,
            180_000,
            ["shadow:SIG00000000000000000000003:entry filled at an open during the downtime"],
        ),
        # at the open of the minute the crash interrupted (L-02: not an observed minute):
        (
            23511,
            118,
            30_000,
            ["shadow:SIG00000000000000000000008:entry filled in the minute the crash interrupted"],
        ),
        (
            853832589,
            176,
            30_000,
            ["shadow:SIG00000000000000000000003:entry filled in the minute the crash interrupted"],
        ),
        (
            0,
            2,
            1,
            [
                "benchmark:benchmark:BTCUSDT:entry filled in the minute the crash interrupted",
                "benchmark:benchmark:ETHUSDT:entry filled in the minute the crash interrupted",
            ],
        ),
    ],
)
def test_a_delayed_restart_stays_causal(
    seed: int, fail_batch: int, delay_ms: int, expected: list[str]
) -> None:
    """Downtime may change fills, so ledgers are not compared: the recovery goes through the
    production path (new subscription, AWAITING_BACKFILL, the runtime's backfill) and is causal,
    except where production itself fills a resting entry in a minute the downtime overlaps (P-1)."""
    assert check_restore_equivalence(seed, fail_batch, delay_ms) == expected


def test_the_empty_minute_still_fails_the_instant_restart_property() -> None:
    """Not fixed (seed=130741648, fail_batch=8): a minute with no bar leaves no entry to mark, so
    the restored service fails closed and the strict property reports it. After downtime the same
    case also fails closed: the backfill cannot hold the empty minute, so the runtime refuses it
    and nothing is processed (correct, so the delayed check has nothing to report)."""
    assert check_restore_equivalence(130741648, 8) == [
        "ledgers differ after restore at batch 8",
        "journal differs after restore at batch 8",
        "awaiting backfill: ['*', 'spot:BTCUSDT', 'spot:ETHUSDT']",
    ]
    r = _reconnected(130741648, 8, 30_000)
    assert r.restored.state is ServiceState.AWAITING_BACKFILL
    assert len(r.journal.batches()) == r.durable
    assert check_restore_equivalence(130741648, 8, 30_000) == []


def test_the_second_empty_minute_example_fails_closed() -> None:
    """Not fixed (seed=50553820, fail_batch=135): minute 34 had no bar. The check before M3-PB.2
    passed it, since the blocked service's ledgers happened to equal the uninterrupted ones; the
    journal comparison reports it."""
    assert check_restore_equivalence(50553820, 135) == [
        "journal differs after restore at batch 63",
        "awaiting backfill: ['*', 'spot:BTCUSDT', 'spot:ETHUSDT']",
    ]
    r = _reconnected(50553820, 135, 30_000)
    assert r.restored.state is ServiceState.AWAITING_BACKFILL
    assert len(r.journal.batches()) == r.durable


# ---------------------------------------------------------------- restart after downtime (B-1)
def _reconnected(
    seed: int, fail_batch: int, delay_ms: int, store: BarStore | None = None
) -> Restart:
    r = restore_after_crash(seed, fail_batch, delay_ms)
    assert isinstance(r, Restart)
    reconnect(r, store)
    return r


def _after(r: Restart) -> list[Batch]:
    return r.journal.batches()[r.durable :]


def _old_live_redelivery(seed: int, fail_batch: int, delay_ms: int) -> Restart:
    """The delayed model before B-1: every minute from the restored position re-delivered as live,
    observed bars, downtime included. Kept to show the checks reject it."""
    r = restore_after_crash(seed, fail_batch, delay_ms)
    assert isinstance(r, Restart)
    start = (r.restored.session.last_minute_ms - minute(0)) // M
    drive(r.restored, r.bars, list(range(start, 40)), missing=r.missing, not_before_ms=r.restart_ms)
    r.first_live = next(i for i in range(40) if minute(i) + 59_999 >= r.restart_ms)
    return r


def _downtime_problems(r: Restart, store: BarStore | None = None) -> list[str]:
    return restart_causality(
        r.journal.batches(),
        r.durable,
        r.restart_ms,
        r.closed_through_ms,
        live_from_ms=r.restart_ms,
        down_from_ms=r.crash_ms,
    ) + recovery_outcome(r, store)


RECOVERIES = [
    # (seed, fail_batch, delay, state after the restart, backfill applied)
    (465923499, 162, 600_000, ServiceState.RUNNING, True),  # ten candles of downtime
    (13073, 124, 30_000, ServiceState.RUNNING, True),  # restart inside the next minute
    (13073, 124, 600_000, ServiceState.AWAITING_BACKFILL, False),  # a bar the feed never had
    (780949414, 169, 322_973, ServiceState.AWAITING_BACKFILL, False),  # the B-1 example
    (130741648, 8, 600_000, ServiceState.AWAITING_BACKFILL, False),  # an empty minute
]


@pytest.mark.parametrize(("seed", "fail_batch", "delay_ms", "state", "applied"), RECOVERIES)
def test_a_restart_after_downtime_recovers_as_production_does(
    seed: int, fail_batch: int, delay_ms: int, state: ServiceState, applied: bool
) -> None:
    """Disconnected from the crash to the restart: the new subscription delivers only bars that
    close at or after the restart, the gap blocks, and the runtime's one backfill attempt either
    resolves the downtime over unobserved bars (RUNNING) or, with a downtime bar the store does not
    have, is refused and nothing is processed (AWAITING_BACKFILL)."""
    r = _reconnected(seed, fail_batch, delay_ms)
    assert r.restored.state is state
    assert r.backfill is not None
    assert r.backfill["ok"] is applied
    after = _after(r)
    if applied:
        downtime = [b for b in after if b[0].payload["bar"]["close_time_ms"] < r.restart_ms]
        assert downtime
        assert all(b[0].payload["observed"] is False for b in downtime)
        assert all(b[0].payload["released_at_ms"] is None for b in downtime)
        assert all(b[0].payload["observed"] is True for b in after[len(downtime) :])
    else:
        assert any("missing from the backfill" in p for p in r.backfill["problems"])
        assert after == []  # fail closed: nothing processed
    assert not set(r.strategy.calls[r.calls_at_restore :]) & {
        (b.open_time_ms, b.symbol)
        for k in (BTC, ETH)
        for b in r.bars[k]
        if b.close_time_ms < r.restart_ms
    }  # the strategy never sees a downtime bar
    assert _downtime_problems(r) == []
    assert check_restore_equivalence(seed, fail_batch, delay_ms) == []


def test_the_live_redelivery_model_is_rejected() -> None:
    """B-1: the old delayed model let an entry placed before the crash fill at a downtime open
    (seed=780949414, fail_batch=169, restart 5 min 22.973 s after the crash: shadow SIG1 at the
    minute-10 open) and the strategy decide on downtime bars. The checks now report both; the
    production path fails closed on the same case (a downtime bar is missing from the store)."""
    old = _old_live_redelivery(780949414, 169, 322_973)
    fills = [
        (e.payload["order_id"], e.payload["bar_open_ms"])
        for b in _after(old)
        for e in b[1:]
        if e.type == "sim_order_filled" and e.payload["role"] == "entry"
    ]
    assert ("shadow:SIG00000000000000000000001:entry", minute(10)) in fills
    assert old.crash_ms < minute(10) < old.restart_ms
    problems = _downtime_problems(old)
    assert any("closed during the downtime but processed observed" in p for p in problems)
    assert any("the strategy saw downtime bars" in p for p in problems)
    new = _reconnected(780949414, 169, 322_973)
    assert not [
        e
        for b in _after(new)
        for e in b[1:]
        if e.type == "sim_order_filled"
        and old.crash_ms <= e.payload["bar_open_ms"] < new.restart_ms
    ]
    assert _downtime_problems(new) == []


def test_a_pending_entry_is_not_filled_during_the_downtime() -> None:
    """Entries resting at the crash whose execution window overlaps ten minutes of downtime
    (seed=696831125, fail_batch=13): over the backfill they were not on any exchange, so they
    never fill on a downtime bar; those whose validity passed meanwhile expire as downtime
    (SIMULATION §7, as `book.py` applies it)."""
    r = restore_after_crash(696831125, 13, 600_000)
    assert isinstance(r, Restart)
    pending = {
        pe.order.order_id
        for book in r.restored.session.sim.books.values()
        for pe in book._pending.values()
        if r.crash_ms <= pe.order.earliest_exec_ms < r.restart_ms
    }
    assert pending  # the scenario is real
    reconnect(r)
    assert r.restored.state is ServiceState.RUNNING
    outcome = {
        e.payload["order_id"]: (e.type, e.payload.get("reason"), e.payload.get("bar_open_ms"))
        for b in _after(r)
        for e in b[1:]
        if e.payload.get("order_id") in pending
    }
    for oid in pending:
        kind, reason, at = outcome[oid]
        if kind == "sim_order_filled":
            assert at >= r.restart_ms
        else:
            assert "downtime" in str(reason)
    assert any("downtime" in str(v[1]) for v in outcome.values())
    assert _downtime_problems(r) == []


def test_a_resting_entry_can_fill_at_the_open_of_a_partly_observed_minute() -> None:
    """Production limitation P-1, NOT fixed (seed=696831125, fail_batch=13, restart 3 minutes after
    the crash, at minute 10 + 1.5 s). The new subscription delivers minute 10 live, so the service
    processes it observed, and the entries of SIG3 resting since before the crash fill at its open,
    1.5 s before the service was running. Under SIMULATION §7 as `book.py` applies it, such an order
    was not on any exchange during the downtime, and L-02 does not count a partly monitored minute
    as observed. The runtime records `observing_since_ms` (a Part B hook) but the service
    does not use it. The check reports it, so the delayed property fails on such cases until a
    production decision; this task changes tests only."""
    r = _reconnected(696831125, 13, 180_000)
    assert r.restart_ms == minute(10) + 1_499
    early = [
        (e.payload["order_id"], e.payload["bar_open_ms"], b[0].payload["observed"])
        for b in _after(r)
        for e in b[1:]
        if e.type == "sim_order_filled"
        and e.payload["role"] == "entry"
        and e.payload["bar_open_ms"] < r.restart_ms
    ]
    assert early == [
        ("baseline:SIG00000000000000000000003:entry", minute(10), True),
        ("shadow:SIG00000000000000000000003:entry", minute(10), True),
    ]
    expected = [
        "baseline:SIG00000000000000000000003:entry filled at an open during the downtime",
        "shadow:SIG00000000000000000000003:entry filled at an open during the downtime",
    ]
    assert _downtime_problems(r) == expected
    assert check_restore_equivalence(696831125, 13, 180_000) == expected


@pytest.mark.parametrize(
    ("seed", "fail_batch", "delay_ms", "order_id"),
    [
        (32438366, 129, 24_837, "shadow:SIG00000000000000000000007:entry"),
        (891223841, 151, 35_763, "shadow:SIG00000000000000000000012:entry"),
    ],
)
def test_an_intrabar_entry_fill_in_the_minute_the_crash_interrupted_is_reported(
    seed: int, fail_batch: int, delay_ms: int, order_id: str
) -> None:
    """Production limitation P-1, NOT fixed (RR-B1 / BR-1 of the M3-PB.2 re-review). The crash and
    the restart fall in the same minute, so the subscription delivers that minute live and the
    service processes it observed; a resting entry fills inside it, intrabar. Its open precedes the
    crash, but nothing about the minute was durable before it: its fills are journaled with the
    bar, after its close, and its price path after the crash was not monitored. L-02 does not
    count the minute as observed, so the fill is reported (before RR-B1 the check passed it)."""
    r = _reconnected(seed, fail_batch, delay_ms)
    assert r.restart_ms - r.crash_ms == delay_ms
    fills = [
        (e.payload["order_id"], e.payload["at_open"], b[0].payload["observed"])
        for b in _after(r)
        for e in b[1:]
        if e.type == "sim_order_filled"
        and e.payload["role"] == "entry"
        and e.payload["bar_open_ms"] < r.crash_ms < e.payload["bar_open_ms"] + M
    ]
    assert fills == [(order_id, False, True)]
    assert check_restore_equivalence(seed, fail_batch, delay_ms) == [
        f"{order_id} filled in the minute the crash interrupted"
    ]


def _downtime_fills(
    batches: list[Batch], durable: int, down: int, live: int, restart_ms: int
) -> list[str]:
    problems = restart_causality(
        batches, durable, restart_ms, None, live_from_ms=live, down_from_ms=down
    )
    return [
        p
        for p in problems
        if p.endswith(
            ("filled at an open during the downtime", "filled in the minute the crash interrupted")
        )
    ]


def test_the_downtime_fill_check_covers_each_minute_the_downtime_overlaps() -> None:
    """An entry fill after the restore is reported when its bar's minute overlaps
    ``[down_from_ms, live_from_ms)``, at its open or intrabar, one millisecond either side of each
    bound; a minute that closed before the crash, or opens at or after the restart, is not."""
    # an intrabar fill on minute t; the crash 1.5 s into it
    r = _reconnected(32438366, 129, 24_837)
    batches, t = r.journal.batches(), r.crash_ms - 1_499
    oid = "shadow:SIG00000000000000000000007:entry"

    def found(down: int, live: int) -> list[str]:
        return _downtime_fills(batches, r.durable, down, live, r.restart_ms)

    assert found(r.crash_ms, r.restart_ms) == [f"{oid} filled in the minute the crash interrupted"]
    assert found(t + 1, r.restart_ms) == [
        f"{oid} filled in the minute the crash interrupted"
    ]  # crash 1 ms after the open
    assert found(t + M - 1, r.restart_ms) == [
        f"{oid} filled in the minute the crash interrupted"
    ]  # crash at the bar's last ms
    assert found(t + M, r.restart_ms) == []  # the minute closed before the crash
    assert found(t, r.restart_ms) == [
        f"{oid} filled at an open during the downtime"
    ]  # crash at the open itself
    assert found(t - 1, t + 1) == [f"{oid} filled at an open during the downtime"]
    assert found(t - 1, t) == []  # the restart at the open: observed from its start (L-02)
    # the entries of 696831125 / 13 fill at the minute-10 open, inside the downtime
    r = _reconnected(696831125, 13, 180_000)
    batches, t = r.journal.batches(), minute(10)
    both = [
        f"{s}:SIG00000000000000000000003:entry filled at an open during the downtime"
        for s in ("baseline", "shadow")
    ]
    assert _downtime_fills(batches, r.durable, r.crash_ms, r.restart_ms, r.restart_ms) == both
    assert _downtime_fills(batches, r.durable, t - 1, t, r.restart_ms) == []


def test_a_fill_journaled_before_the_crash_is_never_judged_as_downtime() -> None:
    """A bar's fills are journaled with the bar, at its release after its close, so every fill in
    the durable journal is on a minute that closed before the crash, and the check does not judge
    those fills even with a window over their minute. The same fill journaled after the restore
    would be reported."""
    r = _reconnected(32438366, 129, 24_837)
    batches = r.journal.batches()
    durable_fills = [
        (i, e.payload["order_id"], e.payload["bar_open_ms"])
        for i, b in enumerate(batches[: r.durable])
        for e in b[1:]
        if e.type == "sim_order_filled" and e.payload["role"] == "entry"
    ]
    assert durable_fills
    for i, _oid, _at in durable_fills:
        assert Bar.model_validate(batches[i][0].payload["bar"]).close_time_ms < r.crash_ms
    i, oid, at = durable_fills[-1]
    assert _downtime_fills(batches, r.durable, at + 1, at + M, r.restart_ms) == []
    assert f"{oid} filled in the minute the crash interrupted" in _downtime_fills(
        batches, i, at + 1, at + M, r.restart_ms
    )


def test_a_bracket_resolves_over_the_backfill() -> None:
    """An open position's take-profit fills on a backfilled bar (seed=853832589, fail_batch=176):
    resting SL/TP are resolved over the downtime path, as if on the exchange (SIMULATION §7);
    that is not a retroactive decision, and the checks accept it."""
    r = _reconnected(853832589, 176, 30_000)
    brackets = [
        e.payload["role"]
        for b in _after(r)
        if b[0].payload["observed"] is False
        for e in b[1:]
        if e.type == "sim_order_filled"
    ]
    assert brackets == ["take_profit"]
    # only the P-1 entry of the interrupted minute is reported, never the take-profit
    assert _downtime_problems(r) == [
        "shadow:SIG00000000000000000000003:entry filled in the minute the crash interrupted"
    ]


@pytest.mark.parametrize(
    "delay_ms",
    [1, 58_499, 58_500, 58_501, 59_999, 60_000, 60_001, 88_500, 88_501, 89_999, 90_000, 90_001],
)
def test_restarts_at_minute_and_settle_boundaries(delay_ms: int) -> None:
    """Crash in the poll at minute 9 + 1.5 s (seed=465923499, fail_batch=162). Minute 9 closes
    58.5 s later: a restart at or before it receives minute 9 live, one after it backfills minute
    9; 88.5 s is minute 8's settle deadline. Every case recovers causally."""
    r = _reconnected(465923499, 162, delay_ms)
    assert r.first_live == (9 if delay_ms <= 58_500 else 10)
    assert r.restored.state is ServiceState.RUNNING
    assert _downtime_problems(r) == []


def _store_without(r: Restart, drop: tuple[int, str]) -> BarStore:
    full = bar_store(r)

    def read(market: Market, symbol: str, start_ms: int, end_ms: int) -> list[Bar]:
        return [
            b for b in full(market, symbol, start_ms, end_ms) if (b.open_time_ms, b.symbol) != drop
        ]

    return read


def _store_with(r: Restart, extra: Bar) -> BarStore:
    full = bar_store(r)

    def read(market: Market, symbol: str, start_ms: int, end_ms: int) -> list[Bar]:
        rows = list(full(market, symbol, start_ms, end_ms))
        return [*rows, extra] if (market, symbol) == (extra.market, extra.symbol) else rows

    return read


def test_an_incomplete_or_invalid_backfill_fails_closed() -> None:
    """The complete recovery of seed=465923499, fail_batch=162 after 10 minutes, with a bar store
    that lacks one downtime bar of an exposed instrument, or holds a duplicate or an unclosed bar:
    the runtime refuses the whole backfill, nothing is processed and the service stays blocked."""
    base = restore_after_crash(465923499, 162, 600_000)
    assert isinstance(base, Restart)
    assert (Market.SPOT, "BTCUSDT") in base.exposed
    downtime_bar = base.bars[BTC][12]
    unclosed = base.bars[BTC][25]  # its close is after the backfill attempt
    stores = {
        "missing": lambda r: _store_without(r, (downtime_bar.open_time_ms, "BTCUSDT")),
        "duplicate": lambda r: _store_with(r, downtime_bar),
        "unclosed": lambda r: _store_with(r, unclosed),
    }
    for name, make in stores.items():
        r = restore_after_crash(465923499, 162, 600_000)
        assert isinstance(r, Restart)
        store = make(r)
        reconnect(r, store)
        assert r.restored.state is ServiceState.AWAITING_BACKFILL, name
        assert r.backfill is not None
        assert r.backfill["ok"] is False
        assert r.backfill["problems"], name
        assert _after(r) == [], name
        assert _downtime_problems(r, store) == [], name
        if name == "missing":
            # judged against the complete store, a blocked service is wrong: the check says so
            assert any("has every downtime bar" in p for p in recovery_outcome(r))


def test_recovery_outcome_reports_each_violation() -> None:
    r = _reconnected(465923499, 162, 600_000)
    assert recovery_outcome(r) == []
    batches = r.journal.batches()
    downtime = next(
        i for i in range(r.durable, len(batches)) if batches[i][0].payload["observed"] is False
    )
    # a downtime bar of an exposed instrument left out, and the last live bar lost
    for drop, message in ((downtime, "downtime bars of exposed instruments"), (-1, "live bars")):
        kept = [b for i, b in enumerate(batches) if i != (drop % len(batches))]
        cut = dataclasses.replace(r, journal=MemoryJournal(_batches=kept))
        assert any(message in p for p in recovery_outcome(cut)), message
    # a downtime bar processed observed, a live bar processed unobserved
    live = next(
        i for i in range(r.durable, len(batches)) if batches[i][0].payload["observed"] is True
    )
    for i, flag, message in ((downtime, True, "observed"), (live, False, "unobserved")):
        flipped = dataclasses.replace(
            r, journal=MemoryJournal(_batches=_forge(batches, i, 0, observed=flag))
        )
        assert any(p.endswith(f"processed {message}") for p in recovery_outcome(flipped)), message
    # a bar the journal already had, processed again after the restore
    old = next(i for i in range(r.durable) if batches[i][0].payload.get("kind") == "bar")
    again = dataclasses.replace(r, journal=MemoryJournal(_batches=[*batches, batches[old]]))
    assert any("processed but not a bar after the journal" in p for p in recovery_outcome(again))
    # the strategy saw a downtime bar
    head = batches[downtime][0].payload["bar"]
    r.strategy.calls.append((head["open_time_ms"], head["symbol"]))
    assert any("the strategy saw downtime bars" in p for p in recovery_outcome(r))
    r.strategy.calls.pop()
    # blocked after processing, and faulted
    r.restored.state = ServiceState.AWAITING_BACKFILL
    blocked = recovery_outcome(r)
    assert any("AWAITING_BACKFILL after processing" in p for p in blocked)
    assert any("has every downtime bar" in p for p in blocked)
    r.restored.state = ServiceState.FAULTED
    assert any("faulted" in p for p in recovery_outcome(r))


def test_the_delayed_check_reports_the_recovery_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reconnection that never delivers anything is causal, so only the recovery outcome inside
    `check_restore_equivalence` can report it."""
    import paper_scenarios

    def silent(r: Restart, store: BarStore | None = None) -> None:
        r.first_live = next(i for i in range(40) if minute(i) + 59_999 >= r.restart_ms)

    monkeypatch.setattr(paper_scenarios, "reconnect", silent)
    problems = check_restore_equivalence(465923499, 162, 600_000)
    assert any("live bars not processed" in p for p in problems)


def _forge(batches: list[Batch], i: int, j: int, **payload: object) -> list[list[JournalEntry]]:
    out = [list(b) for b in batches]
    out[i][j] = dataclasses.replace(out[i][j], payload={**out[i][j].payload, **payload})
    return out


def _find(batches: list[Batch], pred: object) -> tuple[int, int, JournalEntry]:
    return next(
        (i, j, e)
        for i, b in enumerate(batches)
        for j, e in enumerate(b)
        if pred(e)  # type: ignore[operator]
    )


def test_restart_causality_reports_each_violation() -> None:
    _svc, journal, _ = run_random_session(13073)
    batches = journal.batches()
    durable = next(
        i
        for i in range(len(batches) - 1, 0, -1)
        if any(e.type == "sim_order_submitted" for e in batches[i][1:])
    )
    after = batches[durable:]
    first = after[0][0].payload["released_at_ms"]
    assert restart_causality(batches, durable, first, None) == []
    # a release before the restart, and an entry executable before it
    late = restart_causality(batches, durable, first + 3 * M, None)
    assert any("released at" in p for p in late)
    assert any("executable before the restart" in p for p in late)
    # a bar processed twice
    twice = restart_causality([*batches, batches[durable]], durable, first, None)
    assert any("processed again or out of clock order" in p for p in twice)
    # a bar of a minute recorded closed before the crash
    bar_open = after[0][0].payload["bar"]["open_time_ms"]
    closed = restart_causality(batches, durable, first, bar_open + M)
    assert any("closed before the crash" in p for p in closed)
    # an entry filled before its earliest execution
    submitted = {
        e.payload["order_id"]: e.payload["earliest_exec_ms"]
        for b in batches
        for e in b[1:]
        if e.type == "sim_order_submitted"
    }
    i, j, fill = next(
        (i, j, e)
        for i, b in enumerate(batches)
        for j, e in enumerate(b)
        if e.type == "sim_order_filled" and e.payload["order_id"] in submitted
    )
    early = dataclasses.replace(
        fill, payload={**fill.payload, "bar_open_ms": submitted[fill.payload["order_id"]] - M}
    )
    forged = [list(b) for b in batches]
    forged[i][j] = early
    assert any(
        "filled before its earliest execution" in p
        for p in restart_causality(forged, durable, first, None)
    )


def test_restart_causality_reports_each_execution_and_bracket_violation() -> None:
    """Seed 18 has entries at the open and inside the bar, stop-losses, take-profits and market
    exits; its journal is clean, and each forged violation is reported."""
    _svc, journal, _ = run_random_session(18)
    batches = journal.batches()
    t0 = batches[0][0].payload["released_at_ms"]

    def check(forged: list[list[JournalEntry]] | list[Batch]) -> list[str]:
        return restart_causality(forged, 0, t0, None)

    assert check(batches) == []
    # a bar released before its close, and a release time going back
    i, _j, head = _find(batches, lambda e: e.payload.get("kind") == "bar")
    close = head.payload["bar"]["close_time_ms"]
    assert any(
        "before its close" in p for p in check(_forge(batches, i + 5, 0, released_at_ms=close))
    )
    back = batches[i + 5][0].payload["released_at_ms"] - 1
    assert any(
        f"released at {back}, before" in p
        for p in check(_forge(batches, i + 6, 0, released_at_ms=back))
    )
    # an order executable before its release minute, and at its decision
    i, j, sub = _find(
        batches, lambda e: e.type == "sim_order_submitted" and e.payload["book"] != "benchmark"
    )
    floor = -(-batches[i][0].payload["released_at_ms"] // M) * M
    assert any(
        "before its release minute" in p
        for p in check(_forge(batches, i, j, earliest_exec_ms=floor - M))
    )
    decision = sub.payload["decision_time_ms"]
    assert any(
        "at or before its decision" in p
        for p in check(_forge(batches, i, j, earliest_exec_ms=decision))
    )
    # a decision on an unobserved bar
    assert any(
        "sim_order_submitted on the unobserved bar" in p
        for p in check(_forge(batches, i, 0, observed=False))
    )
    # a market exit due before its release minute
    i, j, _ex = _find(
        batches, lambda e: e.type == "sim_exit_requested" and e.payload["action"] == "market_exit"
    )
    floor = -(-batches[i][0].payload["released_at_ms"] // M) * M
    assert any(
        "market exit due before" in p for p in check(_forge(batches, i, j, exit_due_ms=floor - M))
    )
    # an entry filled without a submission, and on an unobserved bar
    i, j, entry = _find(
        batches, lambda e: e.type == "sim_order_filled" and e.payload["role"] == "entry"
    )
    k, m, _ = _find(
        batches,
        lambda e: (
            e.type == "sim_order_submitted" and e.payload["order_id"] == entry.payload["order_id"]
        ),
    )
    unsubmitted = [list(b) for b in batches]
    unsubmitted[k] = [e for n, e in enumerate(unsubmitted[k]) if n != m]
    assert any("filled without a submission" in p for p in check(unsubmitted))
    assert any(
        "filled on an unobserved bar" in p for p in check(_forge(batches, i, 0, observed=False))
    )
    # a stop-loss and a market exit before their entry
    for role in ("stop_loss", "exit"):
        i, j, _f = _find(
            batches, lambda e, role=role: e.type == "sim_order_filled" and e.payload["role"] == role
        )
        early = _forge(batches, i, j, bar_open_ms=minute(0) - M)
        assert any("before its position's entry" in p for p in check(early)), role
    # a take-profit in the bar of an intrabar entry
    i, j, tp = _find(
        batches, lambda e: e.type == "sim_order_filled" and e.payload["role"] == "take_profit"
    )
    prefix = tp.payload["order_id"].rsplit(":", 1)[0]
    k, m, entry = _find(
        batches,
        lambda e: e.type == "sim_order_filled" and e.payload["order_id"] == f"{prefix}:entry",
    )
    same_bar = _forge(
        _forge(batches, k, m, at_open=False), i, j, bar_open_ms=entry.payload["bar_open_ms"]
    )
    assert any("take-profit in the bar of an intrabar entry" in p for p in check(same_bar))
    # with a downtime: a bar that closed before the restart processed observed, and a backfilled
    # bar after a live one
    r = _reconnected(465923499, 162, 600_000)
    rb = r.journal.batches()
    assert (
        restart_causality(
            rb,
            r.durable,
            r.restart_ms,
            r.closed_through_ms,
            live_from_ms=r.restart_ms,
            down_from_ms=r.crash_ms,
        )
        == []
    )
    d = next(n for n in range(r.durable, len(rb)) if rb[n][0].payload["observed"] is False)
    seen = restart_causality(
        _forge(rb, d, 0, observed=True),
        r.durable,
        r.restart_ms,
        None,
        live_from_ms=r.restart_ms,
        down_from_ms=r.crash_ms,
    )
    assert any("closed during the downtime but processed observed" in p for p in seen)
    moved = [*rb[:d], *rb[d + 1 :], rb[d]]
    assert any(
        "backfilled after a live bar" in p
        for p in restart_causality(
            moved, r.durable, r.restart_ms, None, live_from_ms=r.restart_ms, down_from_ms=r.crash_ms
        )
    )
