"""Pinned regressions for two defects found by the Hypothesis properties in
`tests/property/sim/test_paper_properties.py` (recorded failing examples, kept verbatim).

1. Live clock: the first `release()` committed the clock position to the earliest buffered minute
   even though that minute was neither complete nor due, so an earlier minute that was still in
   flight (within its own settle window) was then classified late.
2. Paper restore: after a restart, a minute in which an instrument's bar never arrived *before the
   crash* (a gap the books already handled; the delivery key is behind the journal's last key) was
   treated as unobserved downtime, so the service waited for a backfill that can never be applied.

Two further classes of the restore property are NOT fixed and are pinned here as characterisations
(see the last two tests): a crash in the middle of a minute with a symbol that never arrived, and
release-latency dependence of the shadow book.
"""

from __future__ import annotations

import random

from paper_helpers import BTC, ETH, config, drive, new_service
from paper_scenarios import (
    check_live_clock_order,
    check_restore_equivalence,
    random_bars,
    random_script,
    run_random_session,
)

from tsclaude.sim.journal import MemoryJournal
from tsclaude.sim.live import LiveClock
from tsclaude.sim.paper import PaperTradingService, ServiceState
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


# ---------------------------------------------------------------- known residual restore cases
def test_crash_in_the_middle_of_a_minute_with_a_never_delivered_symbol_blocks() -> None:
    """Characterisation, NOT a fix (seed=13073, fail_batch=124, found by the property): the last
    journalled bar is BTC minute 25, and ETH minute 25 never arrived while the session ran.
    The journal cannot tell 'ETH 25 not processed yet / lost in the restart' from 'ETH 25 never
    existed' (both are a key after `last_key`), so the restored service fails closed: it waits for
    a backfill and processes nothing. Resolving it needs a recorded per-minute close (design
    decision); until then `test_restore_after_any_journal_failure` can still hit this class."""
    ref, restored, bars, ctx = _crash_and_restore(13073, 124)
    assert restored.session.last_minute_ms == minute(25)
    assert (25, "ETHUSDT") in ctx["missing"]
    before = len(restored.journal.batches())
    drive(restored, bars, list(range(25, 40)), missing=ctx["missing"])
    assert restored.state is ServiceState.AWAITING_BACKFILL
    assert list(restored.awaiting_missing) == ["spot:ETHUSDT"]
    assert len(restored.journal.batches()) == before  # fail closed: nothing processed
    assert restored.ledger_hashes() != ref.ledger_hashes()


def test_release_latency_changes_the_shadow_book_across_a_restart() -> None:
    """Characterisation (seed=465923499, fail_batch=162): the shadow book never fills before the
    wall time the bar was released (`not_before_ms`), and a restart changes that time (the
    redelivered ETH minute 7 is released 1.5 s after its close instead of 61.5 s, because minute 6
    is no longer pending). The baseline and benchmark ledgers are identical; only the shadow
    ledger differs. This is a limit of the equivalence property itself, not of the restore
    continuity logic."""
    ref, restored, bars, ctx = _crash_and_restore(465923499, 162)
    start = (restored.session.last_minute_ms - minute(0)) // M
    drive(restored, bars, list(range(start, 40)), missing=ctx["missing"])
    assert restored.state is ServiceState.RUNNING
    got, want = restored.ledger_hashes(), ref.ledger_hashes()
    assert got["baseline"] == want["baseline"]
    assert got["benchmark"] == want["benchmark"]
    assert got["shadow"] != want["shadow"]
