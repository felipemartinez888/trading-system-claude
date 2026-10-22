"""Causality audit M3-T1 (risk engine, live paper trading, lifecycle, restart): failing tests.

Each test demonstrates one finding of the audit record. They FAIL on 61c9d72 by design; they are
the acceptance checks for the fixes. Do not weaken them to make them pass -- escalate instead.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal as D
from pathlib import Path

from paper_helpers import (
    BTC,
    ETH,
    ScriptedStrategy,
    config,
    drive,
    new_service,
    record,
    sid,
    two_symbol_bars,
)
from risk_helpers import snapshot

from tsclaude.contracts.events import canonical_json
from tsclaude.contracts.market import Market
from tsclaude.contracts.provenance import Provenance, Sourced
from tsclaude.contracts.signals import EntryRuleKind, Side, SignalStatus
from tsclaude.contracts.timeutil import MS_PER_MINUTE
from tsclaude.marketdata.bars import BarClosed
from tsclaude.risk.config import RiskConfig
from tsclaude.risk.engine import RiskEngine
from tsclaude.risk.hook import EngineRiskHook
from tsclaude.risk.market import MarketStateTracker, MarketView, cutoff_ms
from tsclaude.sim.book import Book, BookConfig, RiskCheck
from tsclaude.sim.clock import HistoricalClock
from tsclaude.sim.costs import CostModel
from tsclaude.sim.engine import Simulator
from tsclaude.sim.orders import EntryRequest
from tsclaude.sim.paper import PaperTradingService, ServiceState
from tsclaude.sim.testing import T0, ScriptedSource, minute, series, spot_spec
from tsclaude.store.eventstore import EventStore


def _close(i: int) -> int:
    return minute(i) + MS_PER_MINUTE - 1


# ---------------------------------------------------------------- A1 live fill before decision
def test_live_entry_never_executes_before_the_decision_was_released() -> None:
    """SIMULATION §2: earliest execution is the next open after T + decision_latency. In a live
    session the real decision latency is the release time of the bar (journaled as
    ``released_at_ms``). Here ETH's minute-1 bar never arrives, so minute 1 is released at the
    settle deadline (close + 90 s), i.e. *after minute 2 has fully closed* -- yet the BTC entry
    decided on minute 1 is filled at the minute-2 open, a price that no longer existed when the
    decision was made."""
    strat = ScriptedStrategy({(minute(1), "BTCUSDT"): [record(1, 1)]})
    svc, journal = new_service(strat)
    drive(svc, two_symbol_bars(8), list(range(8)), missing={(1, "ETHUSDT")})
    assert svc.state is ServiceState.RUNNING, svc.fault
    decision = next(
        b[0].payload
        for b in journal.batches()
        if b[0].payload["kind"] == "bar"
        and b[0].payload["bar"]["symbol"] == "BTCUSDT"
        and b[0].payload["bar"]["open_time_ms"] == minute(1)
    )
    released = int(decision["released_at_ms"])
    assert released > _close(2)  # the precondition: decision released after minute 2 closed
    fills = [
        e
        for b in journal.batches()
        for e in b
        if e.type == "sim_order_filled" and e.payload.get("order_id") == f"baseline:{sid(1)}:entry"
    ]
    assert fills, "the entry did not fill at all"
    pos = svc.session.sim.books["baseline"]
    trades_and_open = [t.entry_bar_open_ms for t in pos.trades] or [minute(2)]
    assert min(trades_and_open) >= released, (
        f"entry executed at bar open {min(trades_and_open)} but the decision was only released "
        f"at {released} (release latency {decision['release_latency_ms']} ms)"
    )


# ---------------------------------------------------------------- A2 daily loss latch
def _healthy(check: RiskCheck) -> MarketView:
    from risk_helpers import symbol_market

    cut = cutoff_ms(check.stage, check.ts_ms)
    sym = symbol_market(
        symbol=check.request.symbol, last_bar_close_ms=cut, spread_as_of_ms=cut - 10
    )
    return MarketView(cut - 10, 0, {(sym.market, sym.symbol): sym})


def test_daily_loss_limit_blocks_until_next_utc_day_even_after_recovery() -> None:
    """SIMULATION §6: "-2 % of start-of-day equity -> no new entries until next UTC day". The rule
    is evaluated on the *current* equity only, so a book that breached -2 % at minute 2 and
    recovered by minute 4 accepts new entries again the same UTC day."""
    cfg = RiskConfig(max_risk_per_trade=D("0.5"))
    hook = EngineRiskHook(RiskEngine(cfg, CostModel()), _healthy)
    eth = spot_spec(symbol="ETHUSDT")
    book = Book(
        "baseline",
        D(10_000),
        CostModel(),
        hook,
        instruments=[spot_spec(), eth],
        config=BookConfig(risk_per_trade=D("0.5"), max_notional_fraction=D("0.45")),
    )
    flat = (1000, 1001, 999, 1000)
    btc_rows = [flat, flat, (1000, 1000, 935, 940), (940, 1001, 939, 1000), flat, flat, flat]
    bars = sorted(
        [*series(btc_rows), *series([flat] * len(btc_rows), symbol="ETHUSDT")],
        key=lambda b: (b.open_time_ms, str(b.market), b.symbol),
    )
    big = EntryRequest(
        "big",
        Market.SPOT,
        "BTCUSDT",
        Side.LONG,
        EntryRuleKind.MARKET_NEXT_OPEN,
        stop_price=D("850"),
        take_profit_price=D("1400"),
        valid_until_ms=minute(30),
    )
    later = EntryRequest(
        "later",
        Market.SPOT,
        "ETHUSDT",
        Side.LONG,
        EntryRuleKind.MARKET_NEXT_OPEN,
        stop_price=D("850"),
        take_profit_price=D("1300"),
        valid_until_ms=minute(30),
    )
    script = {(T0, "BTCUSDT"): [big], (minute(4), "ETHUSDT"): [later]}
    sim = Simulator(HistoricalClock(bars), [book], ScriptedSource(script))
    sim.run()
    low = min(p.equity for p in book.equity_curve)
    assert low <= D(10_000) * D("0.98"), "precondition: the book breached the daily loss limit"
    filled = [e["payload"]["order_id"] for e in sim.events if e["type"] == "sim_order_filled"]
    assert "baseline:later:entry" not in filled, (
        f"entry accepted the same UTC day after a {low} low (< -2 % of 10000)"
    )


# ---------------------------------------------------------------- A3 stale clock-offset sample
def test_clock_skew_rule_rejects_an_offset_sample_that_is_hours_old() -> None:
    """Fail closed on stale state: the server-clock offset carries the time of its last sample
    (`as_of_ms`), but the tracker drops it, so a single sample taken hours ago (clock monitor
    dead) passes the skew rule forever."""
    now = _close(200)
    snap = snapshot(now - 200)
    old = Sourced[int](value=15, provenance=Provenance.LIVE, as_of_ms=now - 3 * 3_600_000)
    snap = snap.model_copy(update={"clock_offset_ms": old})
    tracker = MarketStateTracker()
    tracker.observe_snapshot(snap)
    view = tracker.view_at(now, [BTC])
    assert view is not None
    assert view.clock_offset_ms is None, (
        "a 3-hour-old clock offset sample is used as if it were current"
    )


# ---------------------------------------------------------------- A4 restore without resolution
def test_live_bars_after_restore_are_refused_until_the_downtime_is_resolved() -> None:
    """SIMULATION §7: open positions are resolved over the backfilled path before going live.
    `restore` does not enforce it: feeding live bars right after a restore skips the downtime,
    so the stop hit in minute 7 (while down) is never booked, the position survives, and the
    strategy is called and may enter on an unresolved book. Fail closed would refuse (or fault)
    live bars after a gap until `resume_after_downtime` has covered it."""
    strat = ScriptedStrategy(
        {
            (minute(1), "BTCUSDT"): [record(1, 1)],
            (minute(11), "ETHUSDT"): [record(2, 11, symbol="ETHUSDT")],
        }
    )
    svc, journal = new_service(strat)
    rows = {7: (995, 996, 985, 986), 8: (986, 990, 984, 988), 9: (990, 1001, 989, 1000)}
    bars = two_symbol_bars(14, rows)
    drive(svc, bars, list(range(6)))
    assert svc.session.lifecycle.status(sid(1)) is SignalStatus.OPEN
    restored = PaperTradingService.restore(config(), journal, strategy=strat)
    calls = len(strat.calls)
    drive(restored, bars, list(range(10, 14)))  # no resume_after_downtime(...)
    entered = restored.session.lifecycle.status(sid(2)) is SignalStatus.OPEN
    survived = restored.session.lifecycle.status(sid(1)) is SignalStatus.OPEN
    assert not (len(strat.calls) > calls or entered or survived), (
        f"live processing resumed over an unresolved 4-minute downtime: strategy calls "
        f"+{len(strat.calls) - calls}, new entry={entered}, stopped-out position still open="
        f"{survived}"
    )


# ---------------------------------------------------------------- A5 shadow outcome coverage
def test_every_vetoed_signal_has_a_shadow_outcome_or_an_explicit_reason() -> None:
    """SIGNALS_AND_AGENTS §1: a vetoed signal's hypothetical outcome is still computed. The
    shadow book is one-position-per-symbol, so a second vetoed BTC signal while the shadow still
    holds the first is silently dropped: VETOED with no shadow outcome and no event saying why.
    Veto-quality statistics are then computed on a selected subset without any trace."""
    strat = ScriptedStrategy(
        {
            (minute(1), "BTCUSDT"): [record(1, 1, tp=1012.0, valid_bars=3)],  # R:R veto
            (minute(3), "BTCUSDT"): [record(2, 3, tp=1012.0, valid_bars=3)],  # R:R veto
        }
    )
    svc, journal = new_service(strat)
    drive(svc, two_symbol_bars(12, {9: (1000, 1015, 999, 1010)}), list(range(12)))
    lc = svc.session.lifecycle
    assert lc.status(sid(1)) is SignalStatus.VETOED
    assert lc.status(sid(2)) is SignalStatus.VETOED
    events = [e for b in journal.batches() for e in b if e.aggregate_id == sid(2)]
    explained = lc.shadow_outcome(sid(2)) is not None or any(
        e.type == "signal_shadow_outcome" for e in events
    )
    assert explained, (
        "vetoed signal 2 has no shadow outcome and nothing in the journal records that the "
        f"shadow book refused it: {[e.type for e in events]}"
    )


# ---------------------------------------------------------------- A6 hash chain on restore
def test_restore_from_event_store_verifies_the_hash_chain(tmp_path: Path) -> None:
    """test_paper_session.test_tampered_journal_fails_restore says inputs that alter no outcome
    are "caught by the event store's hash chain, not by replay". The production restore path
    (`PaperTradingService.open`) never calls `EventStore.verify()`: an edited row -- here the
    journaled release latency, which per SIMULATION §2 feeds the backtest latency -- restores
    silently."""
    path = tmp_path / "state.db"
    strat = ScriptedStrategy({(minute(1), "BTCUSDT"): [record(1, 1)]})
    with EventStore(path) as store:
        svc = PaperTradingService.open(config(), store, strategy=strat)
        drive(svc, two_symbol_bars(4), list(range(4)))
    con = sqlite3.connect(path)
    with con:  # someone with file access (the triggers only stop honest mistakes)
        con.execute("DROP TRIGGER events_no_update")
        rows = con.execute("SELECT seq, payload FROM events WHERE type = 'paper_step'").fetchall()
        for seq, text in rows:
            p = json.loads(text)
            if p.get("released_at_ms") is None:
                continue
            p["released_at_ms"] -= 1_490  # consistent edit: latency 1500 -> 10 ms
            p["release_latency_ms"] = p["released_at_ms"] - p["bar"]["close_time_ms"]
            con.execute("UPDATE events SET payload = ? WHERE seq = ?", (canonical_json(p), seq))
    con.close()
    with EventStore(path) as store:
        assert not store.verify().ok  # precondition: the chain is broken
        try:
            restored = PaperTradingService.open(config(), store, strategy=strat)
        except Exception:
            return  # fail closed: acceptable
        assert restored.state is ServiceState.FAULTED, (
            f"restored over a broken hash chain; latencies now {restored.latency_ms()}"
        )


__all__ = ["ETH", "BarClosed"]
