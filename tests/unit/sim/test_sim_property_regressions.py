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

Not fixed, pinned as characterisations: release-latency dependence (class 2, seed 465923499, and
the remaining divergence of seed 13073), and a minute the clock released with no bar at all.
"""

from __future__ import annotations

import random

import pytest
from paper_helpers import BTC, ETH, config, drive, new_service, two_symbol_bars
from paper_scenarios import (
    check_live_clock_order,
    check_replay_parity,
    check_restore_equivalence,
    random_bars,
    random_script,
    run_random_session,
)

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
    """Characterisation, NOT a fix: with class 1 resolved, seed 13073 reaches class 2. The
    uninterrupted session released minute 26 at 61.5 s (it waited behind minute 25's deadline);
    the restored one, knowing minute 25 closed, releases it at 1.5 s. Release time bounds
    execution (`not_before_ms`), so the shadow book differs; baseline and benchmark do not.
    `test_restore_after_any_journal_failure` therefore still fails on this example.

    Harness timing (causality audit F1): the failed write happened in the poll at minute 28 +
    1.5 s, while the re-delivery polls the restored service at minute 27 + 1.5 s, before the
    crash, which no real restart can do. The divergence is a property of this timeline, not a
    restore or simulator defect; changing the harness is an owner decision, so it is pinned."""
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
    assert check_restore_equivalence(13073, 124) == ["ledgers differ after restore at batch 49"]


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
    ledger differs. This is a limit of the equivalence property itself, not of the restore
    continuity logic: the failed write happened in the poll at minute 9 + 1.5 s, and the
    re-delivery releases ETH 7 at minute 8 + 1.5 s, before the crash (causality audit F1)."""
    ref, restored, bars, ctx = _crash_and_restore(465923499, 162)
    start = (restored.session.last_minute_ms - minute(0)) // M
    drive(restored, bars, list(range(start, 40)), missing=ctx["missing"])
    assert restored.state is ServiceState.RUNNING
    got, want = restored.ledger_hashes(), ref.ledger_hashes()
    assert got["baseline"] == want["baseline"]
    assert got["benchmark"] == want["benchmark"]
    assert got["shadow"] != want["shadow"]
