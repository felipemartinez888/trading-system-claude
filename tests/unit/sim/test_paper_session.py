"""Live paper session end to end (scripted strategy, simulated subscription): lifecycle across the
baseline / shadow / benchmark books, risk vetoes with shadow outcomes, journal completeness and
replay parity through the historical clock (SIMULATION §1, §5-§7)."""

from __future__ import annotations

import hashlib
from decimal import Decimal as D

import pytest
from paper_helpers import (
    Confirm,
    Invalidate,
    ScriptedStrategy,
    config,
    drive,
    new_service,
    record,
    sid,
    two_symbol_bars,
)

from tsclaude.contracts.events import canonical_json
from tsclaude.contracts.market import Market
from tsclaude.contracts.signals import EntryRuleKind, RunMode, SignalStatus
from tsclaude.risk.config import RiskConfig
from tsclaude.sim.clock import HistoricalClock
from tsclaude.sim.journal import STEP_TYPE, JournalEntry, flatten
from tsclaude.sim.paper import BENCHMARK, RestoreError, ServiceState, replay
from tsclaude.sim.testing import minute

N = 20
BTC_ROWS = {5: (1000, 1035, 999, 1030), 9: (1000, 1015, 999, 1010)}
ETH_ROWS = {7: (1000, 1001, 985, 988)}


def scenario() -> ScriptedStrategy:
    m = minute
    return ScriptedStrategy(
        {
            (m(1), "BTCUSDT"): [record(1, 1)],  # long, TP 1030 hit in minute 5
            (m(2), "ETHUSDT"): [
                record(2, 2, symbol="ETHUSDT", stage=SignalStatus.WATCH, valid_bars=6)
            ],
            (m(4), "ETHUSDT"): [Confirm(sid(2))],  # enters minute-5 open, stopped in minute 7
            (m(6), "BTCUSDT"): [record(3, 6, tp=1012.0)],  # R:R 0.77 -> VETOED; shadow takes it
            (m(8), "BTCUSDT"): [record(4, 8, stage=SignalStatus.WATCH, valid_bars=3)],
            (m(10), "BTCUSDT"): [
                record(5, 10, kind=EntryRuleKind.LIMIT, price=995.0, stop=985.0, valid_bars=4)
            ],
            (m(12), "BTCUSDT"): [Invalidate(sid(5))],
        }
    )


def run_live() -> tuple[object, object, ScriptedStrategy]:
    strat = scenario()
    svc, journal = new_service(strat)
    bars = two_symbol_bars(N, BTC_ROWS, ETH_ROWS)
    drive(svc, bars, list(range(N)), missing={(13, "ETHUSDT")})
    return svc, journal, strat


def test_live_session_lifecycle_books_and_shadow_outcome() -> None:
    svc, _journal, strat = run_live()
    assert svc.state is ServiceState.RUNNING, svc.fault
    lc = svc.session.lifecycle
    s1, s2, s3, s4, s5 = (lc.get(sid(i)) for i in range(1, 6))
    assert s1 is not None
    assert s1.status is SignalStatus.CLOSED
    assert s1.outcome is not None
    assert s1.outcome["exit_reason"] == "take_profit"
    assert s2 is not None
    assert s2.status is SignalStatus.CLOSED
    assert s2.outcome is not None
    assert s2.outcome["exit_reason"] == "stop_loss"
    assert [h[1] for h in s2.history] == ["WATCH", "CONFIRMED", "OPEN", "CLOSED"]
    assert s3 is not None
    assert s3.status is SignalStatus.VETOED
    assert s3.reason is not None
    assert "reward_risk:rr_net" in s3.reason
    assert s3.shadow_outcome is not None
    assert s3.shadow_outcome["exit_reason"] == "take_profit"
    assert s4 is not None
    assert s4.status is SignalStatus.EXPIRED
    assert s4.reason == "validity_elapsed"
    assert s5 is not None
    assert s5.status is SignalStatus.INVALIDATED
    books = svc.session.sim.books
    assert [t.request_id for t in books["baseline"].trades] == [sid(1), sid(2)]
    assert [t.request_id for t in books["shadow"].trades] == [sid(1), sid(2), sid(3)]
    bench = books[BENCHMARK]
    assert bench.open_positions == 2  # equal-weight buy-and-hold of BTC and ETH
    notionals = sorted(p.entry_notional for p in bench._positions.values())
    assert notionals[1] - notionals[0] < D("100")  # equal weight up to fees / rounding
    for book in books.values():
        assert book.ledger.reconcile().ok
    # the strategy is only called on observed bars, once each, in clock order
    assert len(strat.calls) == 2 * N - 1
    assert (minute(13), "ETHUSDT") not in strat.calls
    assert svc.clock.stats.incomplete_minutes == 1


def test_every_state_change_is_journaled_in_order() -> None:
    svc, journal, _ = run_live()
    batches = journal.batches()
    assert len(batches) == svc.session.batches == 2 * N - 1
    assert all(b[0].type == STEP_TYPE for b in batches)
    sim_entries = [e for e in flatten(batches) if e.type.startswith("sim_")]
    assert [e.payload["sim_seq"] for e in sim_entries] == list(
        range(1, len(svc.session.sim.events) + 1)
    )
    kinds = {e.type for e in sim_entries}
    assert {"sim_order_filled", "sim_position_closed", "sim_ledger_posted"} <= kinds
    lifecycle = [e for e in flatten(batches) if e.aggregate_type == "signal"]
    assert [e.type for e in lifecycle].count("signal_created") == 5
    first = batches[0][0].payload
    assert first["session"]["risk_config_hash"] == config().risk.config_hash


def test_replay_parity_identical_ledger_hash_and_fingerprint() -> None:
    """SIMULATION §1/§9: a recorded live session replayed through `HistoricalClock` reproduces the
    paper ledger byte for byte (every batch is verified, then the hashes are compared)."""
    svc, journal, _ = run_live()
    replayed = replay(config(), journal.batches())
    assert isinstance(replayed.sim.clock, HistoricalClock)
    assert replayed.sim.clock.mode is RunMode.REPLAY
    assert replayed.ledger_hashes() == svc.ledger_hashes()
    assert replayed.fingerprint() == svc.session.fingerprint()
    live_lc = {s.signal_id: (s.status, s.shadow_outcome) for s in svc.session.lifecycle.signals()}
    rep_lc = {s.signal_id: (s.status, s.shadow_outcome) for s in replayed.lifecycle.signals()}
    assert rep_lc == live_lc


def test_replay_parity_hashes_are_pinned() -> None:
    """Reproducibility anchor across platforms (CI: Linux and Windows). Update deliberately."""
    svc, journal, _ = run_live()
    digest = hashlib.sha256(
        canonical_json([e.to_dict() for e in flatten(journal.batches())]).encode()
    ).hexdigest()
    assert svc.ledger_hashes() == PINNED_LEDGER_HASHES
    assert digest == PINNED_JOURNAL_HASH


# Recorded on Linux (sandbox). If an intentional change to the simulator, risk engine, lifecycle or
# journal format alters these, update them in the same commit and say why.
PINNED_LEDGER_HASHES = {
    "baseline": "514dcc33727311849e90846205afc4c237823f5f8f2b3aa3354b05fc70fc2be5",
    "shadow": "6673efd89691c46cc42d1bd4988654e4257e5846d7cee3ea8445647c60ac95ce",
    "benchmark": "1fd2d1d95519a831947e62257082b8b9ca084f20c9aab832451dcba4271730d9",
}
PINNED_JOURNAL_HASH = "893a9fc2b2ae81495ef0153d85b3c2edda1b889046d89b602bbd63998d2dc082"


def _replace_entry(batches: list, b: int, e: int, payload: dict) -> None:
    old = batches[b][e]
    batches[b][e] = JournalEntry(old.type, old.aggregate_type, old.aggregate_id, old.ts_ms, payload)


def test_tampered_journal_fails_restore() -> None:
    """Replay verifies outputs: a changed input that alters any outcome, or a changed output, is
    detected. (A changed input that alters nothing -- e.g. the close of a bar nobody holds -- is
    caught by the event store's hash chain, not by replay.)"""
    _, journal, _ = run_live()
    batches = journal.batches()
    # input: BTC minute 5 no longer reaches the take-profit (batch 10 = minute 5, BTC first)
    head = batches[10][0]
    assert head.payload["bar"]["symbol"] == "BTCUSDT"
    assert head.payload["bar"]["open_time_ms"] == minute(5)
    bar = {**head.payload["bar"], "high": 1001.0, "close": 1000.0}
    _replace_entry(batches, 10, 0, {**head.payload, "bar": bar})
    with pytest.raises(RestoreError, match=r"batch 10 .* diverged"):
        replay(config(), batches)
    # output: a fill price edited in the journal
    batches = journal.batches()
    i, j = next(
        (i, j)
        for i, b in enumerate(batches)
        for j, e in enumerate(b)
        if e.type == "sim_order_filled"
    )
    fill = batches[i][j].payload
    _replace_entry(batches, i, j, {**fill, "price": "1.0"})
    with pytest.raises(RestoreError, match=f"batch {i} "):
        replay(config(), batches)
    # a changed risk configuration cannot silently re-decide a recorded session either
    with pytest.raises(RestoreError):
        replay(config(risk=RiskConfig(min_rr_net=D("0.5"))), journal.batches())


def test_risk_rule_exception_vetoes_with_rule_code() -> None:
    strat = ScriptedStrategy({(minute(1), "BTCUSDT"): [record(1, 1)]})
    svc, _ = new_service(strat)

    def broken(*_: object) -> str | None:
        raise KeyError("boom")

    eng = svc.session.engine
    eng.rules = (*eng.rules[:-1], (eng.rules[-1][0], broken))
    drive(svc, two_symbol_bars(4), list(range(4)))
    s = svc.session.lifecycle.get(sid(1))
    assert s is not None
    assert s.status is SignalStatus.VETOED
    assert s.reason is not None
    assert "signal_validity:rule_error:KeyError" in s.reason


def test_stale_stream_vetoes_and_strategy_error_is_contained() -> None:
    """Fault injection: the BTC kline stream is STALE at the decision -> VETOED; a strategy that
    raises on one bar yields no intents for that bar (recorded), the session keeps running."""
    from tsclaude.contracts.market import StreamState

    strat = ScriptedStrategy(
        {(minute(2), "BTCUSDT"): [record(1, 2)]}, fail_at={(minute(1), "ETHUSDT")}
    )
    svc, journal = new_service(strat)

    def states(i: int) -> dict | None:
        if i != 2:
            return None
        close = minute(i) + 59_999
        return {
            "kline_1m": (StreamState.STALE, close - 3_000),
            "book_ticker": (StreamState.HEALTHY, close - 600_000),
        }

    drive(svc, two_symbol_bars(5), list(range(5)), snapshot_states=states)
    s = svc.session.lifecycle.get(sid(1))
    assert s is not None
    assert s.status is SignalStatus.VETOED
    assert "data_freshness:streams_unhealthy" in (s.reason or "")
    errors = [b[0].payload["intent_errors"] for b in journal.batches()]
    assert ["strategy_error:RuntimeError"] in errors
    assert svc.state is ServiceState.RUNNING
    assert replay(config(), journal.batches()).ledger_hashes() == svc.ledger_hashes()


def test_invalid_intents_are_rejected_and_recorded() -> None:
    strat = ScriptedStrategy(
        {
            (minute(1), "BTCUSDT"): [record(1, 0)],  # decision time of another bar
            (minute(2), "BTCUSDT"): [record(2, 2, symbol="ETHUSDT")],  # other instrument
            (minute(3), "BTCUSDT"): [Confirm(sid(9))],  # unknown signal
        }
    )
    svc, journal = new_service(strat)
    drive(svc, two_symbol_bars(5), list(range(5)))
    errors = [e for b in journal.batches() for e in b[0].payload["intent_errors"]]
    assert len(errors) == 3
    assert svc.session.lifecycle.signals() == []


def test_funding_applies_to_holding_books_and_replays() -> None:
    from tsclaude.sim.orders import InstrumentSpec

    cfg = config(
        instruments=(InstrumentSpec(Market.USDM, "BTCUSDT", D("0.1"), D("0.001"), D("5")),)
    )
    strat = ScriptedStrategy({(minute(1), "BTCUSDT"): [record(1, 1, market=Market.USDM)]})
    svc, journal = new_service(strat, cfg=cfg)
    from risk_helpers import snapshot

    from tsclaude.contracts.market import BarSource
    from tsclaude.marketdata.bars import BarClosed
    from tsclaude.sim.testing import series

    bars = [
        b.model_copy(update={"source": BarSource.WS_LIVE})
        for b in series([(1000, 1001, 999, 1000)] * 6, symbol="BTCUSDT", market=Market.USDM)
    ]
    for i in range(6):
        close = minute(i) + 59_999
        svc.on_snapshot(snapshot(close - 200, market=Market.USDM))
        svc.on_update(BarClosed(bars[i]))
        svc.poll(close + 1_000)
        if i == 3:
            assert svc.apply_funding_rate(Market.USDM, "BTCUSDT", minute(4), D("0.0001"), D("1000"))
    head = [b[0].payload for b in journal.batches() if b[0].payload["kind"] == "funding"]
    assert head[0]["applied"] == ["baseline", "shadow"]
    assert replay(cfg, journal.batches()).ledger_hashes() == svc.ledger_hashes()
