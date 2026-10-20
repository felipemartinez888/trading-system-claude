"""Determinism, chunking invariance, replay parity, ledger invariants, drawdown and R series."""

from __future__ import annotations

import random
from decimal import Decimal as D

import pytest

from tsclaude.contracts.events import canonical_json
from tsclaude.contracts.market import Market
from tsclaude.contracts.provenance import Provenance
from tsclaude.contracts.signals import EntryRuleKind, RunMode, Side
from tsclaude.sim.book import Book, BookConfig, EquityPoint, drawdown_series, drawdown_stats
from tsclaude.sim.clock import HistoricalClock
from tsclaude.sim.costs import CostModel
from tsclaude.sim.engine import Simulator, fingerprint
from tsclaude.sim.orders import EntryRequest
from tsclaude.sim.testing import (
    T0,
    InvariantSource,
    ScriptedSource,
    check_book_invariants,
    chunked,
    minute,
    random_scenario,
    run_book,
    series,
    spot_spec,
)

SEEDS = range(25)


def _sim(seed: int, mode: RunMode = RunMode.BACKTEST, config: BookConfig | None = None):  # type: ignore[no-untyped-def]
    sc = random_scenario(seed)
    books = [
        Book("baseline", D(10_000), CostModel(), instruments=sc.specs, config=config),
        Book("stressed", D(10_000), CostModel().scaled(D(2)), instruments=sc.specs, config=config),
    ]
    return (
        sc,
        books,
        Simulator(HistoricalClock(sc.bars, mode=mode), books, ScriptedSource(sc.script)),
    )


@pytest.mark.parametrize("seed", SEEDS)
def test_same_inputs_same_fingerprint(seed: int) -> None:
    a = _sim(seed)[2].run()
    b = _sim(seed)[2].run()
    assert a.fingerprint == b.fingerprint
    assert a.ledger_hashes == b.ledger_hashes
    assert canonical_json(list(a.events)) == canonical_json(list(b.events))
    assert fingerprint(a.events) == a.fingerprint


@pytest.mark.parametrize("seed", SEEDS)
def test_chunking_the_bar_stream_does_not_change_the_fingerprint(seed: int) -> None:
    sc, _, sim = _sim(seed)
    reference = sim.run().fingerprint
    ordered = list(HistoricalClock(sc.bars))
    rng = random.Random(seed)
    for _ in range(3):
        sizes = [rng.randint(1, 50) for _ in range(len(ordered))]
        _, books, sim2 = _sim(seed)
        for chunk in chunked(ordered, sizes):
            sim2.feed(chunk)
        assert sim2.finish().fingerprint == reference
    # Chunking the *input* of the clock (per-symbol slices) is equally irrelevant.
    _, books, _ = _sim(seed)
    clock = HistoricalClock.from_chunks(chunked(sc.bars, [7, 30, 1, 100]))
    sim3 = Simulator(clock, books, ScriptedSource(sc.script))
    assert sim3.run().fingerprint == reference


@pytest.mark.parametrize("seed", range(5))
def test_replay_parity_mode_is_a_label_only(seed: int) -> None:
    """Recorded session replayed through HistoricalClock(REPLAY) == the paper run, byte for byte."""
    paper = _sim(seed, RunMode.LIVE)[2].run()
    replay = _sim(seed, RunMode.REPLAY)[2].run()
    backtest = _sim(seed, RunMode.BACKTEST)[2].run()
    assert paper.fingerprint == replay.fingerprint == backtest.fingerprint
    assert paper.ledger_hashes == replay.ledger_hashes
    assert (paper.provenance, replay.provenance, backtest.provenance) == (
        Provenance.SIMULATED,
        Provenance.REPLAY,
        Provenance.BACKTEST,
    )


def test_fingerprint_changes_when_an_input_changes() -> None:
    sc, _, sim = _sim(3)
    ref = sim.run().fingerprint
    book = Book("baseline", D(10_000), CostModel(), instruments=sc.specs)
    stressed = Book("stressed", D(10_001), CostModel().scaled(D(2)), instruments=sc.specs)
    other = Simulator(HistoricalClock(sc.bars), [book, stressed], ScriptedSource(sc.script))
    assert other.run().fingerprint != ref


def test_golden_fingerprint_is_pinned() -> None:
    """Cross-platform reproducibility anchor (CI runs Linux and Windows).

    If this changes, the event schema or the fill/ledger behaviour changed: update deliberately
    and say why in the commit message.
    """
    res = _sim(0)[2].run()
    assert res.fingerprint == PINNED_FINGERPRINT_SEED0


PINNED_FINGERPRINT_SEED0 = "03040c483c1ab8ee842e59991ea1dc9d5862b4996f0ff1c94288a1f00a8c4b18"


@pytest.mark.parametrize("seed", range(40))
def test_ledger_and_sizing_invariants_seeded(seed: int) -> None:
    """Balanced entries, equity == reconstruction at every bar, spot cash never negative,
    size never above risk / notional / cash limits (deterministic, fixed seeds)."""
    sc = random_scenario(seed, signal_prob=0.12)
    config = BookConfig(
        risk_per_trade=D("0.02") if seed % 2 else D("0.005"),
        max_notional_fraction=D("0.5") if seed % 3 == 0 else D("1"),
        max_notional_abs=D("3000") if seed % 5 == 0 else None,
    )
    book = Book("baseline", D(10_000), CostModel(), instruments=sc.specs, config=config)
    src = InvariantSource(ScriptedSource(sc.script), [book])
    res = Simulator(HistoricalClock(sc.bars), [book], src).run()
    assert src.checks == len(sc.bars)
    assert src.problems == []
    assert check_book_invariants(book, res) == []
    assert book.open_positions == 0  # scenarios end flat
    assert book.equity() == book.ledger.reconcile().equity_from_entries


def test_drawdown_against_hand_computed_series() -> None:
    """Equity 100, 110, 99, 121, 115 at t = 0..4.

    Running peak 100, 110, 110, 121, 121 -> drawdown 0, 0, 11/110 = 0.1, 0, 6/121 = 0.04958678.
    Max 0.1 from peak t=1 to trough t=2; underwater t=1..3 (2) and t=3..4 (1) -> longest 2.
    """
    pts = [EquityPoint(t, D(v)) for t, v in enumerate([100, 110, 99, 121, 115])]
    assert drawdown_series(pts) == [D(0), D(0), D("0.1"), D(0), D("0.04958678")]
    st = drawdown_stats(pts)
    assert (st.max_drawdown, st.peak_ts_ms, st.trough_ts_ms) == (D("0.10000000"), 1, 2)
    assert st.longest_duration_ms == 2
    flat = drawdown_stats([EquityPoint(0, D(5)), EquityPoint(1, D(5))])
    assert (flat.max_drawdown, flat.longest_duration_ms) == (D(0), 0)


def test_r_multiples_against_hand_computed_series() -> None:
    """Three sequential fee-bearing long trades on spot (10 000 equity, 0.5 % risk), stop 10 below.

    All market entries at open 1000 -> entry 1000.3, risk/unit 10.3... computed per trade:
      t1 TP 1020 (as G1): R = 1.71647573
      t2 starts from equity 10085.8172638: risk budget 50.429086319 -> qty floor(4.89602...) = 4.896
         stop 990 hit intrabar: exit 989.7, notional 4845.5712; entry notional 4897.4688
         fees 4.8974688 + 4.8455712 = 9.74304; gross -51.8976; net -61.64064
         1R = 4.896 x 10.3 = 50.4288 -> R = -1.22233010
    Mean R = (1.71647573 - 1.22233010) / 2 = 0.24707282 (rounded half-even at 1e-8).
    """
    rows = [
        (1000, 1001, 999, 1000),
        (1000, 1005, 998, 1002),
        (1002, 1021, 1001, 1019),
        (1000, 1001, 999, 1000),
        (1000, 1001, 995, 1000),
        (1000, 1001, 989, 995),
    ]
    bars = series(rows)

    def market(i: int, rid: str) -> EntryRequest:
        return EntryRequest(
            rid,
            Market.SPOT,
            "BTCUSDT",
            Side.LONG,
            EntryRuleKind.MARKET_NEXT_OPEN,
            stop_price=D("990"),
            take_profit_price=D("1020"),
            valid_until_ms=minute(i + 3),
        )

    script = {(T0, "BTCUSDT"): [market(0, "t1")], (minute(3), "BTCUSDT"): [market(3, "t2")]}
    book, _ = run_book(bars, script, specs=[spot_spec()])
    t1, t2 = book.trades
    assert t1.r_multiple == D("1.71647573")
    assert (t2.qty, t2.risk_amount, t2.net_pnl) == (D("4.896"), D("50.4288"), D("-61.64064"))
    assert t2.r_multiple == D("-1.22233010")
    mean = ((t1.r_multiple + t2.r_multiple) / 2).quantize(D("1e-8"))
    assert mean == D("0.24707282")
