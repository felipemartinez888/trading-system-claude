"""Report metrics on a hand-checkable trade set, and the evidence-grade rules."""

from __future__ import annotations

import random
from decimal import Decimal
from typing import Any

import pytest

from tsclaude.contracts.market import Market
from tsclaude.contracts.signals import Side
from tsclaude.research import report
from tsclaude.research.stats import Certification, certify
from tsclaude.sim.book import EquityPoint, TradeRecord
from tsclaude.sim.costs import Liquidity
from tsclaude.sim.fills import ExitReason

H = 3_600_000


def trade(
    i: int, net: str, *, ambiguous: bool = False, alt: str | None = None, gap: bool = False
) -> TradeRecord:
    n = Decimal(net)
    fees, slip, risk = Decimal(2), Decimal(1), Decimal(100)
    return TradeRecord(
        trade_id=f"t{i}",
        book="baseline",
        market=Market.SPOT,
        symbol="BTCUSDT" if i % 2 == 0 else "ETHUSDT",
        side=Side.LONG,
        request_id=f"r{i}",
        signal_id=None,
        entry_bar_open_ms=i * H,
        entry_at_open=True,
        entry_price=Decimal(100),
        entry_liquidity=Liquidity.TAKER,
        qty=Decimal(10),
        stop_price=Decimal(90),
        take_profit_price=Decimal(110),
        exit_bar_open_ms=i * H + 59 * 60_000,
        exit_at_open=False,
        exit_price=Decimal(105),
        exit_liquidity=Liquidity.MAKER,
        exit_reason=ExitReason.TAKE_PROFIT if n > 0 else ExitReason.STOP_LOSS,
        gap_fill=False,
        ambiguous_bar=ambiguous,
        spans_gap=gap,
        gross_pnl=n + fees,
        fees=fees,
        funding=Decimal(0),
        slippage_cost=slip,
        net_pnl=n,
        risk_amount=risk,
        r_multiple=n / risk,
        mae_r=Decimal("0.5"),
        mfe_r=Decimal("1"),
        alt_r_if_tp_first=None if alt is None else Decimal(alt),
    )


TRADES = [
    trade(0, "100"),
    trade(1, "100"),
    trade(2, "-110", ambiguous=True, alt="1.0"),
    trade(3, "50"),
    trade(4, "-100", gap=True),
]


def test_trade_stats_hand_checked() -> None:
    s = report.trade_stats(TRADES, alpha=0.05)
    assert s["n"] == 5
    assert s["wins"] == 3
    assert s["win_rate"] == pytest.approx(0.6)
    # exact Clopper-Pearson 95 % for 3/5 (binomial tail bisection): [0.146633, 0.947255]
    assert s["win_rate_ci"]["lower"] == pytest.approx(0.146633, abs=1e-6)
    assert s["win_rate_ci"]["upper"] == pytest.approx(0.947255, abs=1e-6)
    assert s["expectancy_r"] == pytest.approx((1 + 1 - 1.1 + 0.5 - 1) / 5)
    assert s["profit_factor"] == pytest.approx(250 / 210)
    assert s["payoff_ratio"] == pytest.approx((2.5 / 3) / 1.05)
    assert s["worst_trade_r"] == pytest.approx(-1.1)
    assert s["cost_share"] == pytest.approx(15 / (103 + 103 + 107 + 53 + 97))
    assert s["ambiguous_share"] == pytest.approx(0.2)
    assert s["spans_gap_share"] == pytest.approx(0.2)
    assert s["expectancy_r_if_ambiguous_tp_first"] == pytest.approx((1 + 1 + 1 + 0.5 - 1) / 5)
    assert s["expectancy_pct"] == pytest.approx((10 + 10 - 11 + 5 - 10) / 5)  # % of notional 1000
    assert s["exit_reasons"]["take_profit"] == 3
    b = report.breakdowns(TRADES)
    assert b["by_symbol"]["spot:BTCUSDT"]["n"] == 3
    assert b["by_entry_hour_utc"]["02"]["expectancy_r"] == pytest.approx(-1.1)
    act = report.exposure_turnover(TRADES, Decimal(10_000), 5 * H, 1)
    assert act["exposure"] == pytest.approx(1.0)
    assert act["turnover"] == pytest.approx(5 * 10 * 205 / 10_000)


def test_equity_stats_daily_sharpe() -> None:
    day = 86_400_000
    eq = [EquityPoint(d * day + day - 1, Decimal(v)) for d, v in enumerate([100, 101, 100, 102])]
    summary = {"max_drawdown": "0.0099", "longest_drawdown_ms": day}
    e = report.equity_stats(eq, summary)
    rets = [0.01, 100 / 101 - 1, 0.02]
    import numpy as np

    mu, sd = float(np.mean(rets)), float(np.std(rets, ddof=1))
    assert e["sharpe_daily_annualized"] == pytest.approx(mu / sd * 365**0.5)
    assert e["total_return"] == pytest.approx(0.02)
    assert "sqrt(365)" in e["annualization"]


def test_control_comparison() -> None:
    c = report.control_comparison([1.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0], 0.05)
    assert c["edge_r"] == pytest.approx(0.25)
    assert c["beats_control"] is False  # tiny samples: no demonstrated edge
    assert report.control_comparison([1.0], [], 0.05)["beats_control"] is None


def test_clean_is_platform_stable_and_json_safe() -> None:
    assert report.clean(0.1 + 0.2) == 0.3
    assert report.clean(float("inf")) == "inf"
    assert report.clean(float("nan")) == "nan"
    assert report.clean({"a": (1, Decimal("1.5"))}) == {"a": [1, "1.5"]}


# ---------------------------------------------------------------- grading
def _cert(win: float, loss: float, p: float, n: int = 400, seed: int = 5) -> Certification:
    rng = random.Random(seed)
    r = [win if rng.random() < p else -loss for _ in range(n)]
    return certify(
        r,
        p_star=None,
        alpha=0.05,
        n_trials=1,
        min_power=0.8,
        mean_block=None,
        seed=1,
        max_loss_r=3.0,
        max_loss_source="test",
        n_resamples=2000,
    )


GOOD = _cert(0.9, 1.1, 0.9)
BAD = _cert(0.9, 1.1, 0.3)


def _inputs(**kw: Any) -> report.GradeInputs:
    base: dict[str, Any] = {
        "segment": "validation",
        "prereg_id": "P1",
        "integrity_problems": [],
        "cert": GOOD,
        "n_trades": GOOD.n,
        "flagged_share_cap": 0.05,
        "ambiguous_share": 0.0,
        "spans_gap_share": 0.0,
        "max_loss_measured": True,
        "stress": {"x1.5": GOOD, "x2": GOOD},
        "stress_means": {"x1.5": 0.5, "x2": 0.4},
        "beats_control": True,
        "validation_passed": True,
    }
    base.update(kw)
    return report.GradeInputs(**base)


def test_fixtures_are_what_they_claim() -> None:
    assert GOOD.certified
    assert not BAD.certified
    assert BAD.insufficient_n is True or BAD.margin_not_positive is True


def test_grade_ladder() -> None:
    assert report.grade(_inputs())[0] == "B"  # validation passed, holdout pending
    assert report.grade(_inputs(segment="holdout"))[0] == "A"
    assert report.grade(_inputs(segment="holdout", prereg_id=None))[0] == "B"
    assert report.grade(_inputs(segment="development"))[0] == "C"
    assert report.grade(_inputs(segment="mixed"))[0] == "C"


def test_grade_caps_each_individually() -> None:
    cases = {
        "C": [
            {"ambiguous_share": 0.06},
            {"spans_gap_share": 0.051},
            {"max_loss_measured": False},
            {"beats_control": False},
            {"beats_control": None},
        ],
        "B": [
            {"stress": {"x1.5": None, "x2": GOOD}, "segment": "holdout"},
            {"stress_means": {"x1.5": 0.1, "x2": -0.01}, "segment": "holdout"},
            {"validation_passed": False, "segment": "holdout"},  # audit N4
            {"validation_passed": None, "segment": "holdout"},
        ],
    }
    for expected, variants in cases.items():
        for kw in variants:
            g, reasons = report.grade(_inputs(**kw))
            assert g == expected, (kw, reasons)
    # at the cap exactly: not capped
    assert report.grade(_inputs(ambiguous_share=0.05, segment="holdout"))[0] == "A"


def test_grade_x_d_c_failures() -> None:
    g, reasons = report.grade(_inputs(integrity_problems=["data gaps: 3 missing"]))
    assert g == "X"
    assert reasons == ["integrity: data gaps: 3 missing"]
    assert report.grade(_inputs(cert=None, n_trades=1))[0] == "C"
    big_bad = _cert(0.9, 1.1, 0.3, n=2000)
    assert big_bad.insufficient_n is not None
    g, reasons = report.grade(_inputs(cert=big_bad))
    assert g in {"C", "D"}
    if big_bad.insufficient_n is False:
        assert g == "D"
    exceeded = certify(
        [0.9] * 350 + [-1.0] * 49 + [-5.0],
        p_star=None,
        alpha=0.05,
        n_trials=1,
        min_power=0.8,
        mean_block=None,
        seed=1,
        max_loss_r=3.0,
        max_loss_source="test",
        n_resamples=2000,
    )
    assert exceeded.max_loss_exceeded
    assert report.grade(_inputs(cert=exceeded))[0] == "D"


def test_certification_dict_serializes_every_field() -> None:
    d = report.certification_dict(GOOD)
    assert d["certified"] is True
    assert set(d["ci"]) == {"lower", "upper"}
    assert d["max_loss_source"] == "test"
