"""Research/live consistency (handoff I-1): which research geometries the live baseline book can
ever accept, computed with the live risk engine's own reward/risk function."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from tsclaude.contracts.market import Market
from tsclaude.research import live_compat as lc
from tsclaude.research.walkforward import _live_compatibility
from tsclaude.risk.config import load_risk_config
from tsclaude.sim.costs import CostModel
from tsclaude.strategies import build_strategy

REPO = Path(__file__).resolve().parents[3]
RISK = load_risk_config(REPO / "configs" / "risk.yaml")


def _assess(
    market: Market = Market.SPOT, risk: object = RISK, **params: object
) -> lc.LiveCompatibility:
    return lc.assess(
        build_strategy("bracket_reference", params), risk=risk,  # type: ignore[arg-type]
        cost_model=CostModel(), market=market, symbol="BTCUSDT",
    )  # fmt: skip


@pytest.mark.parametrize("market", [Market.SPOT, Market.USDM])
@pytest.mark.parametrize("tp_r", [0.5, 1.0, 1.5])
def test_the_adr_0012_geometries_are_rejected_live_on_every_trade(
    market: Market, tp_r: float
) -> None:
    """The finding behind decision D-E, as a regression test: with the live floor 1.5 every
    take-profit geometry ADR-0012 requires in research is rejected by the baseline book."""
    assert RISK.min_rr_net == Decimal("1.5")
    got = _assess(market, tp_r=tp_r)
    assert got.status == lc.INCOMPATIBLE
    assert not got.deployable
    assert got.rr_at_min_stop is not None
    assert Decimal(got.rr_at_min_stop) < RISK.min_rr_net


def test_a_wide_take_profit_is_compatible_and_a_middle_one_is_conditional() -> None:
    assert _assess(tp_r=2.0).status == lc.COMPATIBLE
    mid = _assess(tp_r=1.7, max_cost_r=0.5)  # a looser cost filter allows tight stops
    assert mid.status == lc.CONDITIONAL
    assert mid.min_stop_fraction is not None
    assert mid.strategy_min_stop_fraction is not None
    assert mid.min_stop_fraction > mid.strategy_min_stop_fraction
    rr = lc.net_rr(stop_fraction=mid.min_stop_fraction, tp_r=1.7, cost_model=CostModel(),
                   market=Market.SPOT, symbol="BTCUSDT")  # fmt: skip
    assert rr is not None
    assert rr >= RISK.min_rr_net


def test_costs_only_lower_the_net_reward_risk_and_it_grows_with_the_stop() -> None:
    values = [
        lc.net_rr(
            stop_fraction=d, tp_r=2.0, cost_model=CostModel(), market=Market.SPOT, symbol="BTCUSDT"
        )
        for d in (0.005, 0.01, 0.05, 0.2)
    ]
    assert all(v is not None and v < Decimal("2.0") for v in values)
    assert values == sorted(values)  # type: ignore[type-var]


def test_a_lower_floor_would_change_the_answer_but_is_never_chosen_here() -> None:
    lowered = replace(RISK, min_rr_net=Decimal("0.5"))
    assert _assess(risk=lowered, tp_r=1.0).status == lc.COMPATIBLE  # only to show the dependency
    assert RISK.min_rr_net == Decimal("1.5")  # the repository's policy is untouched


def test_a_strategy_without_a_bracket_geometry_is_unknown() -> None:
    strategy = SimpleNamespace()
    got = lc.assess(strategy, risk=RISK, cost_model=CostModel(), market=Market.SPOT,  # type: ignore[arg-type]
                    symbol="BTCUSDT")  # fmt: skip
    assert got.status == lc.UNKNOWN
    assert not got.deployable


def test_the_walk_forward_reports_the_least_compatible_symbol() -> None:
    env = SimpleNamespace(cost_model=CostModel())
    grid = {"a": {"tp_r": 1.0}, "b": {"tp_r": 2.0}}
    out = _live_compatibility(
        env, "bracket_reference", grid, [(Market.SPOT, "BTCUSDT"), (Market.USDM, "BTCUSDT")], RISK,  # type: ignore[arg-type]
    )  # fmt: skip
    assert out is not None
    assert out["configurations"]["a"]["status"] == lc.INCOMPATIBLE
    assert out["configurations"]["b"]["status"] == lc.COMPATIBLE
    assert out["configurations"]["b"]["symbol"] == "spot:BTCUSDT"  # spot costs more: worse
    assert out["any_deployable"] is True
    assert _live_compatibility(env, "bracket_reference", grid, [], None) is None  # type: ignore[arg-type]
