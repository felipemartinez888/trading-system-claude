"""Target A, bounded deterministic end-to-end demonstration (paper only, no network, no lake).

Chain exercised through the production code paths:

    closed 1m bars -> `bracket_reference` (unmodified, default parameters, via the production
    `ReferenceLiveStrategy.create`) -> signal lifecycle -> risk engine (production
    `configs/risk.yaml`, with real market views from snapshots) -> baseline / shadow / benchmark
    books -> ledger -> journal -> deterministic replay.

Two clearly separated scenarios:

* **Reference scenario.** A fully synthetic, deterministic, causal fixture (`target_a_fixture`) on
  which the real `bracket_reference` confirms three signals. Its 1.0 R target cannot satisfy the
  baseline's 1.5 R net requirement, so the baseline rejects every one of them, on that rule alone
  (fail closed), while the ungated shadow book records hypothetical outcomes. Nothing here is
  forced: no rule, parameter or threshold is changed.
* **Synthetic risk-compatible scenario.** An explicitly labelled, separate synthetic strategy
  (`SyntheticRiskCompatibleStrategy`, NOT `bracket_reference`) whose signals carry a 3 R target, so
  the baseline accepts them. It exists only to exercise an accepted baseline entry with fees,
  slippage and a consistent ledger.

Everything is synthetic. No result here says anything about any strategy's performance: the shadow
outcomes of the reference scenario are artifacts of the fixture's path, not evidence of edge.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal as D
from pathlib import Path

import pytest
import target_a_fixture as fx
from paper_helpers import live, record
from risk_helpers import snapshot

from tsclaude.contracts.market import Bar, Market
from tsclaude.contracts.signals import SignalRecord, SignalStatus
from tsclaude.contracts.timeutil import MS_PER_MINUTE
from tsclaude.marketdata.bars import BarClosed
from tsclaude.risk.config import RiskConfig, load_risk_config
from tsclaude.sim.book import TradeRecord
from tsclaude.sim.costs import CostModel, Liquidity
from tsclaude.sim.engine import SimView
from tsclaude.sim.journal import MemoryJournal, flatten
from tsclaude.sim.paper import (
    BASELINE,
    BENCHMARK,
    SHADOW,
    PaperConfig,
    PaperTradingService,
    ServiceState,
    replay,
)
from tsclaude.sim.testing import bar as make_bar
from tsclaude.sim.testing import spot_spec
from tsclaude.strategies import BracketReferenceParams
from tsclaude.strategies.live_adapter import ReferenceLiveStrategy

REPO = Path(__file__).resolve().parents[3]
KEY = (Market.SPOT, fx.SYMBOL)
START = D(10_000)
HALF_SPREAD = 0.00005  # 0.5 bp each side of the last known close: a 1 bp quoted spread


def production_risk() -> RiskConfig:
    return load_risk_config(REPO / "configs" / "risk.yaml")


def production_paper_config() -> PaperConfig:
    """The paper configuration the app builds from the repository's `configs/`
    (`build_paper_config`: instruments, risk and the decision latency from `backtest.yaml`)."""
    from tsclaude.runtime.paper_runtime import PaperSettings, build_paper_config

    return build_paper_config(PaperSettings(), REPO / "configs")


def production_latency_ms() -> int:
    """The decision latency Paper runs with today (`configs/backtest.yaml`, 3,000 ms since
    `1cc2693`). Target A runs with it (operational-readiness review R-02, task T1)."""
    return production_paper_config().decision_latency_ms


def paper_config(decision_latency_ms: int | None = None) -> PaperConfig:
    """Target A's books: the fixture's one instrument, the production `risk.yaml` and, unless a
    test asks for another value, the production decision latency."""
    latency = production_latency_ms() if decision_latency_ms is None else decision_latency_ms
    return PaperConfig(
        instruments=(spot_spec(),),
        starting_equity=START,
        risk=production_risk(),
        decision_latency_ms=latency,
    )


# ---------------------------------------------------------------------------------- driving
POLL_LAG_MS = 1_500  # the live runtime polls shortly after a minute closes


def drive_with_views(
    svc: PaperTradingService,
    bars: Sequence[Bar],
    *,
    lag_ms: int = POLL_LAG_MS,
    poll_every: int = 1,
) -> None:
    """Deliver each closed 1m bar like the subscription does, then poll after its close.

    Before each close a market snapshot is delivered whose book is centred on the **previous**
    bar's close (the latest price known at that time), so the risk engine sees a real, causal view.
    `lag_ms` is how long after the last delivered minute's close the poll happens (the minute is
    released at that wall time); `poll_every` > 1 queues several minutes before each poll, the way
    a startup backlog does.
    """
    prev_close: float | None = None
    for i, b in enumerate(bars):
        centre = b.open if prev_close is None else prev_close
        svc.on_snapshot(
            snapshot(
                b.close_time_ms - 200,
                bid=centre * (1 - HALF_SPREAD),
                ask=centre * (1 + HALF_SPREAD),
                symbols=(KEY,),
            )
        )
        svc.on_update(BarClosed(b))
        if (i + 1) % poll_every == 0:
            svc.poll(b.close_time_ms + lag_ms)
        prev_close = b.close
    svc.poll(bars[-1].close_time_ms + lag_ms)


@dataclass
class Run:
    svc: PaperTradingService
    journal: MemoryJournal
    cfg: PaperConfig
    adapter: ReferenceLiveStrategy | None = None


def run_reference(
    bars: Sequence[Bar] | None = None,
    *,
    lag_ms: int = POLL_LAG_MS,
    poll_every: int = 1,
    decision_latency_ms: int | None = None,
) -> Run:
    """The real `bracket_reference` over the fixture, from the first cycle after its warm-up.

    The harness, not the adapter, keeps warm-up bars out of the live drive (the adapter's own
    `trade_from` guards are covered by `tests/unit/strategies`, not here)."""
    all_bars = list(fx.fixture_bars() if bars is None else bars)
    trade_from = fx.trade_from_ms()
    cfg = paper_config(decision_latency_ms)
    adapter = ReferenceLiveStrategy.create(cfg.cost_model, [KEY], trade_from_ms=trade_from)
    adapter.warm_up([b for b in all_bars if b.close_time_ms < trade_from])
    journal = MemoryJournal()
    svc = PaperTradingService(cfg, journal, strategy=adapter)
    drive_with_views(
        svc,
        [b for b in all_bars if b.close_time_ms >= trade_from - 2 * MS_PER_MINUTE],
        lag_ms=lag_ms,
        poll_every=poll_every,
    )
    return Run(svc, journal, cfg, adapter)


@pytest.fixture(scope="module")
def ref() -> Run:
    run = run_reference()
    assert run.svc.state is ServiceState.RUNNING, run.svc.fault
    return run


# ------------------------------------------------------------------------ reference scenario
def signals(run: Run) -> list:  # type: ignore[type-arg]
    return sorted(run.svc.session.lifecycle.signals(), key=lambda s: s.record["decision_time_ms"])


def test_the_unmodified_bracket_reference_confirms_signals_on_the_fixture(ref: Run) -> None:
    """The fixture satisfies the real CONFIRMED conditions; nothing was bent to force them."""
    assert ref.adapter is not None
    # production adapter factory: default parameters only (it raises on anything else)
    assert ref.adapter.strategy.params == BracketReferenceParams()
    assert ref.adapter.strategy.strategy_id == "bracket_reference"
    assert ref.adapter.strategy.version == "1.0.0"
    sigs = signals(ref)
    assert len(sigs) == 3  # the three planned breakouts, nothing else
    decisions = [s.record["decision_time_ms"] for s in sigs]
    breakout_bar_closes = [
        fx.T0 + (k + 1) * fx.M15 - 1
        for k in range(fx.CYCLE * fx.N_CYCLES)
        if fx.is_breakout(k) and fx.T0 + (k + 1) * fx.M15 - 1 >= fx.trade_from_ms()
    ]
    assert decisions == breakout_bar_closes  # each decision is the close of its 15m breakout bar
    for s in sigs:
        rec = s.record
        assert rec["strategy_id"] == "bracket_reference"
        assert rec["strategy_version"] == "1.0.0"
        assert rec["evidence_grade"] == "X"
        assert rec["stage"] == "CONFIRMED"
        mc = rec["market_conditions"]
        # every CONFIRMED condition of the strategy's own docstring, read from the record
        assert mc["compressed"] is True
        assert mc["trend"] is True
        assert mc["breakout"] is True
        assert mc["volume_ok"] is True
        assert mc["atr_rank_prev"] <= BracketReferenceParams().rank_max
        assert mc["volume_z"] >= BracketReferenceParams().vol_z_min
        assert mc["close"] > mc["range_high_prev"]
        assert mc["close"] > mc["ema"]
        # the unmodified geometry: stop 1.5 ATR below the close, target exactly 1.0 R
        assert rec["rr_gross"] == pytest.approx(1.0)
        ref_p, stop, tp = rec["ref_price"], rec["stop_price"], rec["take_profit_price"]
        assert tp - ref_p == pytest.approx(ref_p - stop)
        assert ref_p - stop == pytest.approx(1.5 * mc["atr"])


@pytest.mark.parametrize(
    ("after_bars_15m", "poll_every"),
    [(0, 1), (0, 5), (12, 1)],
    ids=["cut-at-decision", "cut-at-decision-queued-minutes", "cut-3h-after-decision"],
)
def test_later_bars_cannot_change_an_earlier_decision_or_its_risk_verdict(
    after_bars_15m: int, poll_every: int
) -> None:
    """Causal canary for the whole chain (bars -> strategy -> risk view -> books -> journal).

    Every bar opening after `cut` is distorted (prices x1.5, volume x5). The journal batches up to
    `cut` must stay byte-identical, and that prefix must contain the first signal AND the baseline's
    verdict on it, so a decision or a risk view that depended on any later bar would differ.
    `cut-at-decision` distorts the very next minute, so even a one-minute peek is visible;
    `queued-minutes` offers five minutes before each poll, as a startup backlog does."""
    base = run_reference(poll_every=poll_every)
    first_sig = signals(base)[0]
    first = first_sig.record["decision_time_ms"]
    cut = first + after_bars_15m * fx.M15
    distorted: list[Bar] = [
        b
        if b.open_time_ms <= cut
        else b.model_copy(
            update={
                "open": b.open * 1.5,
                "high": b.high * 1.5,
                "low": b.low * 1.5,
                "close": b.close * 1.5,
                "volume": b.volume * 5,
                "quote_volume": b.quote_volume * 7.5,
            }
        )
        for b in fx.fixture_bars()
    ]
    other = run_reference(distorted, poll_every=poll_every)
    a = base.journal.batches()
    b_ = other.journal.batches()
    n = sum(1 for batch in a if batch[0].payload["kind"] == "bar" and batch[0].ts_ms <= cut + 1)
    prefix = [e for batch in a[:n] for e in batch]
    assert any(e.type == "signal_created" and e.aggregate_id == first_sig.signal_id for e in prefix)
    assert any(
        e.type == "sim_request_rejected" and e.payload["request_id"] == first_sig.signal_id
        for e in prefix
    )  # not vacuous: the first decision and the risk verdict on it are inside the compared prefix
    assert [[e.canonical() for e in batch] for batch in a[:n]] == [
        [e.canonical() for e in batch] for batch in b_[:n]
    ]
    assert [[e.canonical() for e in batch] for batch in a] != [
        [e.canonical() for e in batch] for batch in b_
    ]  # the distortion is real: the futures differ


_REASON = re.compile(
    r"^risk_blocked:RISK_BLOCKED:reward_risk:rr_net:(?P<rr>-?[0-9.]+)<1\.5$"
)  # exactly one failing rule


def independent_rr_net(rec: Mapping[str, object], cm: CostModel) -> D:
    """Net reward/risk recomputed here from the cost model, not with the risk engine's code."""
    ref_p = D(str(rec["ref_price"]))
    stop = D(str(rec["stop_price"]))
    tp = D(str(rec["take_profit_price"]))
    slip = max(cm.slippage.estimate(Market.SPOT, fx.SYMBOL, D(0), D(0)).total_bps, D(0)) / D(10_000)
    taker = cm.fee_rate(Market.SPOT, Liquidity.TAKER)
    maker = cm.fee_rate(Market.SPOT, Liquidity.MAKER)
    assert cm.tp_as_taker is False
    entry_cost = ref_p * (taker + slip)
    reward = (tp - ref_p) - entry_cost - tp * maker
    risk = (ref_p - stop) + entry_cost + stop * (taker + slip)
    return reward / risk


TICK = D("0.1")  # spot_spec() tick size
FIXTURE_BARS = {b.open_time_ms: b for b in fx.fixture_bars()}


def assert_market_entry_at_the_open(t: TradeRecord, cm: CostModel) -> None:
    """A market entry fills at the OPEN of its bar plus slippage, never at the close or earlier."""
    bar = FIXTURE_BARS[t.entry_bar_open_ms]
    slip = cm.slippage.estimate(Market.SPOT, fx.SYMBOL, D(0), D(0)).total_bps / D(10_000)
    assert abs(t.entry_price - D(str(bar.open)) * (1 + slip)) <= TICK
    # the bar closes well away from its open, so a fill at the close would be caught here
    assert abs(D(str(bar.close)) - D(str(bar.open))) > 10 * TICK
    assert abs(t.entry_price - D(str(bar.close)) * (1 + slip)) > TICK


@pytest.mark.parametrize(
    "latency", ["production", 0], ids=["production_latency", "latency_0_pure_next_open"]
)
def test_a_minute_released_at_its_close_fills_at_the_first_open_after_the_latency(
    latency: str | int,
) -> None:
    """With the release at the close (+1 ms) only the decision latency delays execution. The
    fill is the first 1m open strictly after the decision time plus the latency
    (`sim.engine.earliest_execution_ms`), and never earlier.
    - **Production latency (3,000 ms):** a decision at hh:mm:59.999 fills at the open of the
      minute after the next one (decision + 1 ms + 1 minute). This is the configuration Paper
      runs with.
    - **Latency 0:** the pure next-open rule, exactly one millisecond after the decision. This
      bound is what the 1.5 s poll hides. It is kept to pin that rule through the paper path.

    Limit, stated: in the live paper path the release time (`not_before_ms`) is always a lower
    bound as well, so a fill EARLIER than the bound cannot be told apart from the release bound
    through this path alone. `earliest_execution_ms` itself is covered by the simulator's own
    causality tests (a mutant that moves it two minutes earlier is caught by
    `tests/unit/sim/test_m3_audit_carryovers.py`, among others)."""
    from tsclaude.sim.engine import earliest_execution_ms

    latency_ms = production_latency_ms() if latency == "production" else int(latency)
    run = run_reference(lag_ms=1, decision_latency_ms=latency_ms)
    assert run.svc.state is ServiceState.RUNNING, run.svc.fault
    assert run.cfg.decision_latency_ms == latency_ms
    shadow = run.svc.session.sim.books[SHADOW]
    by_signal = {s.signal_id: s for s in signals(run)}
    assert len(shadow.trades) >= 2
    for t in shadow.trades:
        decision = by_signal[str(t.signal_id)].record["decision_time_ms"]
        assert t.entry_bar_open_ms == earliest_execution_ms(decision, latency_ms)
        # the bound stated in numbers, so a change of the rule itself is caught here as well
        whole_minutes = -(-latency_ms // MS_PER_MINUTE)  # 0 ms -> 0, 3,000 ms -> 1
        assert t.entry_bar_open_ms == decision + 1 + whole_minutes * MS_PER_MINUTE
        assert t.entry_bar_open_ms > decision + latency_ms  # never before decision + latency
        assert_market_entry_at_the_open(t, run.cfg.cost_model)
    # the baseline verdicts do not depend on the release lag
    rejected = [e for e in flatten(run.journal.batches()) if e.type == "sim_request_rejected"]
    assert len(rejected) == 3
    assert all(_REASON.match(str(e.payload["reason"])) for e in rejected)


def test_baseline_rejects_every_reference_signal_on_reward_risk_alone(ref: Run) -> None:
    risk = production_risk()
    assert risk.min_rr_net == D("1.5")  # the production threshold, untouched
    events = list(flatten(ref.journal.batches()))
    rejected = [e for e in events if e.type == "sim_request_rejected"]
    sigs = signals(ref)
    assert [e.payload["request_id"] for e in rejected] == [s.signal_id for s in sigs]
    for e, s in zip(rejected, sigs, strict=True):
        assert e.payload["book"] == BASELINE
        m = _REASON.match(str(e.payload["reason"]))
        assert m is not None, e.payload["reason"]  # one reason: reward_risk, nothing else failed
        rr = D(m["rr"])
        assert rr < D("1.5")
        # independent recomputation of the rule (the record prices are unquantized floats, the
        # engine prints 8 decimals): agreement to 1e-6, far tighter than any formula error
        assert float(independent_rr_net(s.record, ref.cfg.cost_model)) == pytest.approx(
            float(rr), abs=1e-6
        )
        # the target is 1.0 R gross, so the net value cannot reach 1.5 (the structural reason)
        assert rr < D("1.0")
        assert s.status is SignalStatus.VETOED
        assert s.reason is not None
        assert "reward_risk:rr_net" in s.reason
        assert s.outcome is None  # a vetoed signal has no real outcome


def test_shadow_outcomes_are_hypothetical_and_the_baseline_never_traded(ref: Run) -> None:
    books = ref.svc.session.sim.books
    base, shadow, bench = books[BASELINE], books[SHADOW], books[BENCHMARK]
    events = list(flatten(ref.journal.batches()))
    sigs = signals(ref)
    # baseline: no order, no position, no trade, ledger holds only the starting capital
    assert len(base.trades) == 0
    assert base.open_positions == 0
    assert not [
        e
        for e in events
        if e.aggregate_id == BASELINE and e.type in ("sim_order_submitted", "sim_order_filled")
    ]
    assert [tx.memo for tx in base.ledger.entries] == ["capital"]
    assert base.equity() == START
    # shadow: the same signals without the gate; its trades are tagged to the shadow book
    assert len(shadow.trades) == 2
    assert shadow.open_positions == 1  # the third is still open when the data ends
    by_signal = {s.signal_id: s for s in sigs}
    for t in shadow.trades:
        assert t.book == SHADOW
        s = by_signal[str(t.signal_id)]
        decision = s.record["decision_time_ms"]
        # the poll releases the minute 1.5 s after its close, i.e. after the next open at
        # decision + 1 ms; execution is bounded by the release, so the fill is the open after
        # (conservative). The unbounded case is tested separately with a release at the close.
        assert t.entry_bar_open_ms == decision + 1 + MS_PER_MINUTE
        assert_market_entry_at_the_open(t, ref.cfg.cost_model)
        assert s.shadow_outcome is not None
        assert s.shadow_outcome["exit_reason"] == "take_profit"
        assert s.outcome is None  # recorded as a shadow outcome, never as a baseline trade
    assert sigs[2].shadow_outcome is None
    # the benchmark is a different thing again: one buy-and-hold position, not a strategy trade
    assert bench.open_positions == 1
    assert len(bench.trades) == 0
    for name, book in books.items():
        assert book.ledger.reconcile().ok, name
    hashes = ref.svc.ledger_hashes()
    assert len({hashes[BASELINE], hashes[SHADOW], hashes[BENCHMARK]}) == 3


# ----------------------------------------------------------- synthetic risk-compatible scenario
SYNTHETIC_ID = "synthetic_risk_compatible_probe"


@dataclass
class SyntheticRiskCompatibleStrategy:
    """EXPLICITLY SYNTHETIC. Not `bracket_reference` and not a trading idea: it emits scripted
    signals whose 3 R target satisfies the baseline's 1.5 R net requirement, so the accepted-entry
    path of the baseline book can be exercised with fees, slippage and a consistent ledger."""

    script: Mapping[int, SignalRecord]  # bar open_time_ms -> signal decided at that bar's close

    def on_bar(self, bar: Bar, view: SimView) -> list[SignalRecord]:
        rec = self.script.get(bar.open_time_ms)
        return [] if rec is None else [rec]


def synthetic_bars() -> list[Bar]:
    """Bars 7 and 14 (the entry bars) deliberately close away from their open, so an entry priced
    at the close instead of the open would be visible."""
    flat = (30_000, 30_005, 29_995, 30_000)
    flat2 = (31_300, 31_305, 31_295, 31_300)
    rows: list[tuple[float, float, float, float]] = []
    rows += [flat] * 7  # 0-6
    rows += [(30_000, 30_040, 29_990, 30_030)]  # 7: signal A enters at this open
    rows += [(30_030, 31_400, 29_990, 31_300)]  # 8: the target of signal A (31,350) is hit
    rows += [flat2] * 5  # 9-13
    rows += [(31_300, 31_340, 31_290, 31_330)]  # 14: signal B enters at this open
    rows += [(31_330, 31_330, 30_800, 30_850)]  # 15: the stop of signal B (30,850) is hit
    rows += [(30_850, 30_855, 30_845, 30_850)] * 4  # 16-19
    return [live(make_bar(i, *r, symbol=fx.SYMBOL, market=Market.SPOT)) for i, r in enumerate(rows)]


def synthetic_script() -> dict[int, SignalRecord]:
    def rec(n: int, at: int, ref_p: float, stop: float, tp: float) -> SignalRecord:
        return record(n, at, ref=ref_p, stop=stop, tp=tp, valid_bars=5).model_copy(
            update={
                "strategy_id": SYNTHETIC_ID,
                "strategy_version": "0",
                "rationale": "SYNTHETIC scripted signal for the Target A accepted-entry scenario",
            }
        )

    a = rec(1, 5, 30_000.0, 29_550.0, 31_350.0)  # +3 R, entered at bar 7's open
    b = rec(2, 12, 31_300.0, 30_850.0, 32_650.0)  # +3 R, entered at bar 14's open, stopped at 15
    bars = synthetic_bars()
    return {bars[5].open_time_ms: a, bars[12].open_time_ms: b}


def run_synthetic() -> Run:
    cfg = paper_config()
    journal = MemoryJournal()
    svc = PaperTradingService(
        cfg, journal, strategy=SyntheticRiskCompatibleStrategy(synthetic_script())
    )
    drive_with_views(svc, synthetic_bars())
    return Run(svc, journal, cfg)


@pytest.fixture(scope="module")
def syn() -> Run:
    run = run_synthetic()
    assert run.svc.state is ServiceState.RUNNING, run.svc.fault
    return run


def test_synthetic_scenario_is_labelled_and_is_not_the_reference_strategy(syn: Run) -> None:
    sigs = sorted(syn.svc.session.lifecycle.signals(), key=lambda s: s.record["decision_time_ms"])
    assert [s.record["strategy_id"] for s in sigs] == [SYNTHETIC_ID, SYNTHETIC_ID]
    assert all(s.record["strategy_id"] != "bracket_reference" for s in sigs)
    assert all("SYNTHETIC" in s.record["rationale"] for s in sigs)


def test_baseline_accepts_a_risk_compatible_entry_with_fees_slippage_and_a_consistent_ledger(
    syn: Run,
) -> None:
    books = syn.svc.session.sim.books
    base = books[BASELINE]
    events = list(flatten(syn.journal.batches()))
    assert not [
        e for e in events if e.type == "sim_request_rejected" and e.aggregate_id == BASELINE
    ]
    cm = syn.cfg.cost_model
    bars = {b.open_time_ms: b for b in synthetic_bars()}
    trades = list(base.trades)
    assert [t.exit_reason.value for t in trades] == ["take_profit", "stop_loss"]
    slip = cm.slippage.estimate(Market.SPOT, fx.SYMBOL, D(0), D(0)).total_bps / D(10_000)
    taker = cm.fee_rate(Market.SPOT, Liquidity.TAKER)
    # the documented, unverified VIP0 defaults are pinned literally: a deliberate change of the
    # cost model must change this test knowingly (spot maker == taker, so a maker/taker mix-up is
    # NOT detectable here)
    assert taker == D("0.001")
    assert cm.fee_rate(Market.SPOT, Liquidity.MAKER) == D("0.001")
    assert slip == D("0.0003")
    tick = TICK
    start_equity = START
    equity_before = START
    for t in trades:
        entry_open = D(str(bars[t.entry_bar_open_ms].open))
        entry_close = D(str(bars[t.entry_bar_open_ms].close))
        # market entry at the open, slippage against the buyer, rounded to the tick
        assert abs(t.entry_price - entry_open * (1 + slip)) <= tick
        assert abs(t.entry_price - entry_close * (1 + slip)) > tick  # not the close
        assert t.entry_price > entry_open
        assert t.entry_liquidity is Liquidity.TAKER
        assert t.qty > 0
        assert t.slippage_cost > 0
        # fees recomputed from the cost model and the trade's own fills
        entry_fee = t.qty * t.entry_price * taker
        exit_fee = t.qty * t.exit_price * cm.fee_rate(Market.SPOT, t.exit_liquidity)
        assert abs(t.fees - (entry_fee + exit_fee)) <= D("0.00000002")
        gross = t.qty * (t.exit_price - t.entry_price)
        assert abs(t.gross_pnl - gross) <= D("0.00000002")
        assert t.net_pnl == t.gross_pnl - t.fees + t.funding
        # sized so that the stop loses about the configured risk (0.5 % of the equity at the
        # sizing moment, which includes the earlier trade's result), and not more
        assert t.risk_amount <= equity_before * D("0.005") + D("0.01")
        # ... and sized to it, not merely under it: the quantity is the fixed-fractional size
        # floored to the lot (0.001), within one lot of risk / (entry - stop)
        ideal = equity_before * D("0.005") / (t.entry_price - t.stop_price)
        assert D(0) <= ideal - t.qty < D("0.002")
        equity_before += t.net_pnl
    win, loss = trades
    # exits: the target fills at the target; the stop fills at the stop minus taker slippage
    assert win.exit_price == win.take_profit_price
    assert win.exit_price == D("31350")
    assert loss.exit_price < loss.stop_price
    assert abs(loss.exit_price - loss.stop_price * (1 - slip)) <= tick
    assert win.net_pnl > 0 > loss.net_pnl
    # the ledger: balanced, replayable from its entries, and equal to the sum of the trades
    assert base.ledger.reconcile().ok
    assert base.open_positions == 0
    assert base.equity() == start_equity + sum((t.net_pnl for t in trades), D(0))
    assert base.cash == base.equity()
    # the entry's risk check passed with the production threshold, not a relaxed one
    assert syn.cfg.risk.min_rr_net == D("1.5")


def test_synthetic_signals_pass_the_risk_gate_and_become_signal_outcomes(syn: Run) -> None:
    sigs = sorted(syn.svc.session.lifecycle.signals(), key=lambda s: s.record["decision_time_ms"])
    assert [s.status for s in sigs] == [SignalStatus.CLOSED, SignalStatus.CLOSED]
    assert [s.outcome["exit_reason"] for s in sigs if s.outcome] == ["take_profit", "stop_loss"]
    assert all(s.reason is None or "reward_risk" not in s.reason for s in sigs)


# ------------------------------------------------------------------------------ replay
def canonical(journal: MemoryJournal) -> str:
    return json.dumps([[e.canonical() for e in b] for b in journal.batches()], sort_keys=True)


def test_replay_reproduces_every_batch_and_every_ledger_hash_in_both_scenarios(
    ref: Run, syn: Run
) -> None:
    for run in (ref, syn):
        replayed = replay(run.cfg, run.journal.batches())  # verify=True: each batch must match
        assert replayed.ledger_hashes() == run.svc.ledger_hashes()
        assert replayed.fingerprint() == run.svc.session.fingerprint()
        assert replayed.batches == run.svc.session.batches
        for book in replayed.sim.books.values():
            assert book.ledger.reconcile().ok


def test_the_same_inputs_reproduce_the_same_journal_and_hashes() -> None:
    first, second = run_reference(), run_reference()
    assert canonical(first.journal) == canonical(second.journal)
    assert first.svc.ledger_hashes() == second.svc.ledger_hashes()
    s1, s2 = run_synthetic(), run_synthetic()
    assert canonical(s1.journal) == canonical(s2.journal)
    assert s1.svc.ledger_hashes() == s2.svc.ledger_hashes()


def test_target_a_runs_with_the_production_paper_latency() -> None:
    """Operational-readiness review R-02 (task T1): Target A ran at latency 0 while Paper runs at
    the latency `build_paper_config` reads from `configs/backtest.yaml`. Fails if they drift."""
    production = production_paper_config()
    assert production.decision_latency_ms == 3_000  # configs/backtest.yaml since 1cc2693
    assert paper_config().decision_latency_ms == production.decision_latency_ms
    # the two scenarios build their books from paper_config(): both run at that latency
    assert run_synthetic().cfg.decision_latency_ms == production.decision_latency_ms
    assert paper_config().risk.config_hash == production.risk.config_hash
    assert paper_config().cost_model.describe() == production.cost_model.describe()


def test_production_thresholds_and_costs_are_the_ones_under_test() -> None:
    risk = production_risk()
    assert risk.min_rr_net == D("1.5")
    assert risk.max_risk_per_trade == D("0.005")
    assert risk.config_hash == RiskConfig().config_hash  # configs/risk.yaml == the code defaults
    cfg = paper_config()
    assert cfg.risk.min_rr_net == D("1.5")
    # the cost model is the paper default, pinned by value (not compared with itself)
    assert cfg.cost_model.fee_rate(Market.SPOT, Liquidity.TAKER) == D("0.001")
    assert cfg.cost_model.slippage.estimate(Market.SPOT, fx.SYMBOL, D(0), D(0)).total_bps == D("3")
    assert cfg.cost_model.version == CostModel().version
