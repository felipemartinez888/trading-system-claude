"""Restart safety (SIMULATION §7) and fault injection: rebuild from the journal, resolve open
positions over backfilled bars (`resolved_from_backfill`), expire signals whose validity passed
during downtime, never enter on unobserved minutes; journal failure and a crash mid-fill leave
the service FAULTED and a restore + re-delivery reproduces the uninterrupted session exactly."""

from __future__ import annotations

from typing import Any

import pytest
from paper_helpers import (
    ScriptedStrategy,
    config,
    drive,
    new_service,
    record,
    sid,
    two_symbol_bars,
)

from tsclaude.contracts.market import BarSource
from tsclaude.contracts.signals import EntryRuleKind, SignalStatus
from tsclaude.sim.book import Book
from tsclaude.sim.fills import ExitReason
from tsclaude.sim.journal import MemoryJournal
from tsclaude.sim.paper import PaperTradingService, ServiceState, replay
from tsclaude.sim.testing import minute

N = 14


def restart_strategy() -> ScriptedStrategy:
    m = minute
    return ScriptedStrategy(
        {
            (m(1), "BTCUSDT"): [record(1, 1)],  # opens minute 2, stopped in downtime minute 7
            (m(3), "ETHUSDT"): [
                record(2, 3, symbol="ETHUSDT", stage=SignalStatus.WATCH, valid_bars=3)
            ],
            (m(4), "ETHUSDT"): [
                record(
                    3,
                    4,
                    symbol="ETHUSDT",
                    kind=EntryRuleKind.LIMIT,
                    price=992.0,
                    stop=960.0,
                    tp=1060.0,
                    valid_bars=3,
                )
            ],
            (m(11), "BTCUSDT"): [record(4, 11)],  # after the restart: enters normally
        }
    )


BTC_ROWS = {7: (995, 996, 985, 986), 8: (986, 990, 984, 988), 9: (990, 1001, 989, 1000)}
ETH_ROWS = {6: (1000, 1001, 980, 985)}  # trades through the 992 limit while we are down


def test_restart_resolves_downtime_on_backfill_without_entries() -> None:
    strat = restart_strategy()
    svc, journal = new_service(strat)
    live_bars = two_symbol_bars(N, BTC_ROWS, ETH_ROWS)
    drive(svc, live_bars, list(range(6)))  # minutes 0-5 observed, then the process dies
    assert svc.session.lifecycle.status(sid(1)) is SignalStatus.OPEN
    assert svc.session.lifecycle.status(sid(3)) is SignalStatus.CONFIRMED
    del svc

    restored = PaperTradingService.restore(config(), journal, strategy=strat)
    assert restored.state is ServiceState.RUNNING
    assert restored.session.lifecycle.status(sid(1)) is SignalStatus.OPEN
    calls_before = len(strat.calls)
    backfill = two_symbol_bars(N, BTC_ROWS, ETH_ROWS, source=BarSource.REST_BACKFILL)
    down = [b for seq in backfill.values() for b in seq if minute(6) <= b.open_time_ms <= minute(9)]
    assert restored.resume_after_downtime(down) == 8
    assert len(strat.calls) == calls_before  # no strategy call for an unobserved minute

    lc = restored.session.lifecycle
    s1, s2, s3 = lc.get(sid(1)), lc.get(sid(2)), lc.get(sid(3))
    assert s1 is not None
    assert s1.status is SignalStatus.CLOSED
    assert s1.outcome is not None
    assert s1.outcome["exit_reason"] == "stop_loss"
    assert s1.outcome["resolved_from_backfill"] is True
    assert s2 is not None
    assert (s2.status, s2.reason) == (SignalStatus.EXPIRED, "downtime")
    assert s3 is not None
    assert (s3.status, s3.reason) == (SignalStatus.EXPIRED, "downtime")
    books = restored.session.sim.books
    (trade,) = books["baseline"].trades
    assert trade.exit_reason is ExitReason.STOP_LOSS
    assert trade.resolved_from_backfill
    assert minute(6) <= trade.exit_bar_open_ms <= minute(9)
    for name in ("baseline", "shadow"):
        assert all(t.request_id != sid(3) for t in books[name].trades)  # limit never filled
        assert books[name].open_positions == 0

    # live again from minute 10: new signals enter normally
    drive(restored, live_bars, list(range(10, N)))
    assert (minute(11), "BTCUSDT") in strat.calls
    assert lc.status(sid(4)) is SignalStatus.OPEN
    # the whole journal (before and after the restart) replays to the same ledgers
    assert replay(config(), journal.batches()).ledger_hashes() == restored.ledger_hashes()
    downtime_steps = [
        b[0].payload for b in journal.batches() if not b[0].payload.get("observed", True)
    ]
    assert len(downtime_steps) == 8
    assert all(p["intents"] == [] for p in downtime_steps)


def test_restore_of_empty_journal_starts_fresh() -> None:
    svc = PaperTradingService.restore(config(), MemoryJournal())
    assert svc.session.batches == 0
    assert svc.session.last_minute_ms is None


def _reference() -> tuple[PaperTradingService, MemoryJournal]:
    strat = ScriptedStrategy(
        {
            (minute(1), "BTCUSDT"): [record(1, 1)],
            (minute(3), "ETHUSDT"): [record(2, 3, symbol="ETHUSDT")],
        }
    )
    svc, journal = new_service(strat)
    drive(svc, two_symbol_bars(10, {6: (1000, 1035, 999, 1030)}), list(range(10)))
    return svc, journal


def _strategy() -> ScriptedStrategy:
    return ScriptedStrategy(
        {
            (minute(1), "BTCUSDT"): [record(1, 1)],
            (minute(3), "ETHUSDT"): [record(2, 3, symbol="ETHUSDT")],
        }
    )


@pytest.mark.parametrize("fail_batch", [3, 6, 7, 9])
def test_journal_failure_faults_and_restore_plus_redelivery_matches(fail_batch: int) -> None:
    """Fault injection: the journal refuses batch k. The service stops (FAULTED, nothing more is
    processed); a restore from the journal plus re-delivery of the bars from that minute on
    reproduces the uninterrupted session's ledgers byte for byte (also mid-minute: k odd)."""
    ref, _ = _reference()
    journal = MemoryJournal(fail_when=lambda i, _b: i == fail_batch)
    bars = two_symbol_bars(10, {6: (1000, 1035, 999, 1030)})
    svc, _ = new_service(_strategy(), journal=journal)
    drive(svc, bars, list(range(10)))
    assert svc.state is ServiceState.FAULTED
    assert svc.fault is not None
    assert svc.fault.startswith("journal_failed")
    assert len(journal.batches()) == fail_batch
    assert svc.poll(minute(99)) == 0  # fail closed: nothing processed any more

    journal.fail_when = None
    # same-process recovery keeps the market tracker; a cold restart starts without market
    # history, so a fill-stage check spanning the fault would (correctly) fail closed instead
    restored = PaperTradingService.restore(
        config(), journal, strategy=_strategy(), tracker=svc.tracker
    )
    drive(restored, bars, list(range(fail_batch // 2, 10)))
    assert restored.state is ServiceState.RUNNING
    assert restored.ledger_hashes() == ref.ledger_hashes()
    assert restored.session.fingerprint() == ref.session.fingerprint()


def test_crash_mid_fill_leaves_no_partial_state() -> None:
    """Fault injection: the baseline book raises inside the entry fill of signal 1 (after the
    risk check, before the ledger post). The step is not journaled; the restored session has no
    trace of the half-done fill, and re-delivery reproduces the uninterrupted session."""
    ref, _ = _reference()
    bars = two_symbol_bars(10, {6: (1000, 1035, 999, 1030)})
    svc, journal = new_service(_strategy())
    baseline = svc.session.sim.books["baseline"]
    original = Book._open
    calls = {"n": 0}

    def exploding_open(self: Book, *args: Any, **kw: Any) -> None:
        if self is baseline and calls["n"] == 0:
            calls["n"] += 1
            raise OSError("disk vanished mid-fill")
        original(self, *args, **kw)

    baseline._open = exploding_open.__get__(baseline, Book)  # type: ignore[method-assign]
    drive(svc, bars, list(range(10)))
    assert svc.state is ServiceState.FAULTED
    assert svc.fault is not None
    assert svc.fault.startswith("step_failed:OSError")
    recorded = journal.batches()
    entry_id = f"baseline:{sid(1)}:entry"
    fills = [e.payload["order_id"] for b in recorded for e in b if e.type == "sim_order_filled"]
    assert entry_id not in fills
    assert any(e.payload.get("order_id") == entry_id for b in recorded for e in b)  # submitted

    restored = PaperTradingService.restore(
        config(), journal, strategy=_strategy(), tracker=svc.tracker
    )
    assert restored.session.sim.books["baseline"].open_positions == 0
    first_missing = len(recorded) // 2
    drive(restored, bars, list(range(first_missing, 10)))
    assert restored.ledger_hashes() == ref.ledger_hashes()
