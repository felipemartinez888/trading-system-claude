"""Pins the derived [D] numbers of docs/research/MATHEMATICAL_STRATEGY_FOUNDATIONS.md (Part 1, 1.2)
to the repository's own functions, so a change to the cost model, the risk engine, the power
formula or a feature definition that would make the document wrong fails here. No data."""

from __future__ import annotations

import math

import numpy as np
import pytest

from tsclaude.contracts.market import Market
from tsclaude.research.live_compat import net_rr
from tsclaude.research.stats.power import n_min_one_sided
from tsclaude.sim.costs import CostModel

COSTS = CostModel()


def rho(b: float, d: float) -> float:
    """The paper engine's cost-adjusted net reward/risk (spot, long market bracket)."""
    v = net_rr(stop_fraction=d, tp_r=b, cost_model=COSTS, market=Market.SPOT, symbol="BTCUSDT")
    assert v is not None
    return float(v)


def b_min(d: float, e: float = 0.0013, tp: float = 0.0010, sl: float = 0.0013) -> float:
    """Closed form of rho(b, d) = 1.5 solved for b (audit §3.1)."""
    return (1.5 * (d + e + sl - sl * d) + e + tp) / (d * (1 - tp))


def test_section_3_1_floor_algebra_matches_the_engine() -> None:
    assert rho(2.0, 0.013) == pytest.approx(1.5192, abs=1e-4)  # the decision brief's 1.52
    row = [round(b_min(d), 2) for d in (0.008, 0.013, 0.02, 0.03, 0.05)]
    assert row == [2.28, 1.98, 1.81, 1.71, 1.62]
    for d in (0.008, 0.013, 0.02, 0.03, 0.05):  # the closed form is the engine's own boundary
        assert rho(b_min(d), d) == pytest.approx(1.5, abs=1e-6)
    assert b_min(0.013) * 0.013 == pytest.approx(0.0257, abs=1e-4)  # target >= 2.57 % of price
    limit = [round(b_min(d, e=0.001), 2) for d in (0.008, 0.013, 0.02, 0.03, 0.05)]
    assert limit == [2.18, 1.92, 1.77, 1.68, 1.61]
    usdm = [round(b_min(d, e=0.0008, tp=0.0002, sl=0.0008), 2) for d in (0.008, 0.013, 0.02, 0.03,
                                                                         0.05)]  # fmt: skip
    assert usdm == [1.92, 1.76, 1.67, 1.61, 1.57]


def test_section_3_2_break_even_and_expectancy() -> None:
    assert pytest.approx(0.40) == 1 / (1 + 1.5)
    rows = ((0.50, 0.75, 0.125), (0.80, 0.65, 0.17), (1.52, 0.45, 0.134), (2.35, 0.35, 0.1725))
    for r, p, e in rows:
        assert p * r - (1 - p) == pytest.approx(e, abs=1e-3)
    assert pytest.approx(0.512) == 0.60 * 1.52 - 0.40


def test_section_3_5_sample_sizes() -> None:
    table = {
        (0.40, 0.43): (1661, 3682, 5000),
        (0.40, 0.45): (600, 1329, 1804),
        (0.40, 0.48): (235, 520, 706),
        (0.40, 0.50): (151, 333, 452),
        (0.30, 0.35): (534, 1176, 1594),
        (0.30, 0.40): (136, 298, 403),
    }
    for (ps, pa), expected in table.items():
        assert tuple(n_min_one_sided(ps, pa, 0.05 / t) for t in (1, 24, 199)) == expected
    assert n_min_one_sided(0.40, 0.45, 0.05 / 199) / n_min_one_sided(0.40, 0.45, 0.05 / 24) == (
        pytest.approx(1.357, abs=1e-3)
    )  # §7: 36 % more trades at 199 rows than at 24
    assert 1 - n_min_one_sided(0.40, 0.45, 0.05 / 55) / 1804 == pytest.approx(0.16, abs=0.005)


def test_section_3_4_the_floor_costs_about_twice_the_trades() -> None:
    """Reconciliation 1.2: for E = +0.15 R the sample size ratio is 1.97 (the audit said 1.9)."""

    def n_for(r: float) -> int:
        ps = 1 / (1 + r)
        return n_min_one_sided(ps, ps + 0.15 / (1 + r), 0.05)

    assert n_for(1.52) == 423
    assert n_for(0.8) == 215
    assert n_for(1.52) / n_for(0.8) == pytest.approx(1.97, abs=0.01)


def _scale(a: float, n: int = 20_000) -> float:
    h = a / n
    return sum(math.exp(((i + 0.5) * h) ** 2 / 2) for i in range(n)) * h


@pytest.mark.parametrize(("k", "b", "ou"), [(1.5, 2, 0.68), (2.0, 2, 0.87), (2.0, 3, 0.71)])
def test_section_3_3_ou_reference_probabilities(k: float, b: float, ou: float) -> None:
    stop = k * (1 + 1 / b)
    p = (_scale(stop) - _scale(k)) / _scale(stop)
    assert p == pytest.approx(ou, abs=0.005)
    assert (stop - k) / stop == pytest.approx(1 / (1 + b))  # random walk: 1/(1 + b)


def test_assumptions_a5_a7_a8() -> None:
    from tsclaude.features.core import BarArrays, volume_zscore

    n = 20
    vol = np.array([1.0] * (n - 1) + [100.0])
    bars = BarArrays(
        open_time_ms=np.arange(n, dtype=np.int64) * 60_000,
        close_time_ms=np.arange(n, dtype=np.int64) * 60_000 + 59_999,
        open=np.ones(n), high=np.ones(n), low=np.ones(n), close=np.ones(n), volume=vol,
    )  # fmt: skip
    assert float(volume_zscore(bars, n)[-1]) == pytest.approx(math.sqrt(19))  # A5: the bound
    for sigma, bars_mean in ((0.003, 37.6), (0.002, 84.5), (0.005, 13.5)):  # A7
        assert 0.026 * 0.013 / sigma**2 == pytest.approx(bars_mean, abs=0.05)
    assert [round(1 / (1 + b), 2) for b in (2.0, 2.5, 3.0)] == [0.33, 0.29, 0.25]  # A8
