"""PT-1 causality audit: attacks on the time exit in Paper that test_pt1_time_exit.py does not make.

(1) a strategy interval other than 1m (the plumbing test only ever runs timeframe=1m, where
    "bucket start + N x step" and "entry open + N x step" are the same number);
(2) journal replay of a session that holds a time-exit position;
(3) restart while the position is open: instant restart (re-delivery) and delayed restart with a
    backfill that spans the due bar, each compared with the uninterrupted twin.

Expected values are hand-computed literals, not read back from the code under test.
"""

from __future__ import annotations

from typing import Any

from paper_helpers import (
    ScriptedStrategy,
    config,
    drive,
    new_service,
    record,
    sid,
    two_symbol_bars,
)

from tsclaude.contracts.market import BarSource, Interval
from tsclaude.contracts.signals import SignalRecord, SignalStatus
from tsclaude.sim.fills import ExitReason
from tsclaude.sim.paper import PaperTradingService, replay
from tsclaude.sim.testing import minute

N = 14


def timed(bars: int, timeframe: Interval = Interval.M1) -> SignalRecord:
    rec = record(1, 0, tp=1030.0)
    return SignalRecord.model_validate(
        {**rec.model_dump(), "time_exit_bars": bars, "timeframe": timeframe}
    )


def strat(rec: SignalRecord) -> ScriptedStrategy:
    return ScriptedStrategy({(minute(0), "BTCUSDT"): [rec]})


def trade_key(t: Any) -> tuple[Any, ...]:
    return (t.entry_bar_open_ms, t.entry_price, t.exit_bar_open_ms, t.exit_price, t.exit_reason)


def reference(rec: SignalRecord) -> tuple[PaperTradingService, Any]:
    svc, journal = new_service(strat(rec))
    drive(svc, two_symbol_bars(N), list(range(N)))
    return svc, journal


# ------------------------------------------------------------------ (1) strategy interval
def test_5m_strategy_time_exit_is_anchored_to_the_entry_bucket() -> None:
    """Decision at the close of minute 0, delivered after minute 1 opened, entry at the open of
    minute 2. T0 is 5m-aligned, so the entry bucket is [minute 0, minute 5) and with
    time_exit_bars=2 the exit is due at minute 0 + 2 x 5 = minute 10, not at minute 12
    (= entry open + 10)."""
    svc, _ = reference(timed(2, Interval.M5))
    (t,) = svc.session.sim.books["baseline"].trades
    assert t.entry_bar_open_ms == minute(2)
    assert t.exit_reason is ExitReason.TIME_EXIT
    assert t.exit_bar_open_ms == minute(10)


# ------------------------------------------------------------------ (2) replay
def test_journal_replay_of_a_time_exit_session_reproduces_it() -> None:
    for rec in (timed(3), timed(2, Interval.M5)):
        svc, journal = reference(rec)
        assert svc.session.sim.books["baseline"].trades, "vacuous: no trade"
        again = replay(config(), journal.batches())
        assert again.ledger_hashes() == svc.ledger_hashes()
        assert [trade_key(t) for t in again.sim.books["baseline"].trades] == [
            trade_key(t) for t in svc.session.sim.books["baseline"].trades
        ]
        recorded = [b[0].payload for b in journal.batches() if b[0].payload["kind"] == "bar"]
        # the record travels through the journal with its time exit
        signals = [i for p in recorded for i in p["intents"] if i["kind"] == "signal"]
        assert [s["record"]["time_exit_bars"] for s in signals] == [rec.time_exit_bars]


# ------------------------------------------------------------------ (3) restart
def test_instant_restart_with_an_open_time_exit_position_matches_the_uninterrupted_twin() -> None:
    rec = timed(3)
    ref, _ = reference(rec)
    svc, journal = new_service(strat(rec))
    bars = two_symbol_bars(N)
    drive(svc, bars, list(range(4)))  # position opened at minute 2, due at minute 5
    assert svc.session.sim.books["baseline"].open_positions == 1
    restored = PaperTradingService.restore(
        config(), journal, strategy=strat(rec), tracker=svc.tracker
    )
    assert restored.session.sim.books["baseline"].open_positions == 1
    drive(restored, bars, list(range(4, N)))
    (t,) = restored.session.sim.books["baseline"].trades
    assert (t.exit_reason, t.exit_bar_open_ms) == (ExitReason.TIME_EXIT, minute(5))
    assert restored.ledger_hashes() == ref.ledger_hashes()
    assert restored.session.fingerprint() == ref.session.fingerprint()


def test_delayed_restart_backfill_spanning_the_due_bar_exits_where_the_twin_does() -> None:
    rec = timed(3)
    ref, _ = reference(rec)
    (twin,) = ref.session.sim.books["baseline"].trades
    svc, journal = new_service(strat(rec))
    live = two_symbol_bars(N)
    drive(svc, live, list(range(4)))
    del svc
    restored = PaperTradingService.restore(config(), journal, strategy=strat(rec))
    backfill = two_symbol_bars(N, source=BarSource.REST_BACKFILL)
    down = [b for seq in backfill.values() for b in seq if minute(4) <= b.open_time_ms <= minute(7)]
    assert restored.resume_after_downtime(down) == 8
    (t,) = restored.session.sim.books["baseline"].trades
    assert t.exit_reason is ExitReason.TIME_EXIT
    assert t.resolved_from_backfill
    assert (t.exit_bar_open_ms, t.exit_price) == (minute(5), twin.exit_price)
    assert restored.session.lifecycle.status(sid(1)) is SignalStatus.CLOSED
    drive(restored, live, list(range(8, N)))
    assert replay(config(), journal.batches()).ledger_hashes() == restored.ledger_hashes()
