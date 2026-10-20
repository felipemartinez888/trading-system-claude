"""``certify``: the admission decision of ADR-0012 computable from outcomes alone.

Scenarios from the task brief, with numbers worked out in the docstrings:

* bracket b = 1, c = 0.05: wins pay 0.95 R, losses cost 1.05 R, p* = 1.05 / 2 = 0.525.
* p = 0.60 -> E = 0.6 * 0.95 - 0.4 * 1.05 = 0.15 R per trade.
* b = 0.1, c = 0 (tiny take-profit): p* = 1 / 1.1 = 0.909; a 90 % win rate *loses*
  E = 0.9 * 0.1 - 0.1 * 1 = -0.01 R per trade: the "fake high win rate" trap.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from stats_exact_ref import exact_tail_ge

from tsclaude.research.stats.certify import certify


def bracket_outcomes(n: int, wins: int, b: float, c: float, seed: int = 7) -> np.ndarray:
    r = np.array([b - c] * wins + [-(1.0 + c)] * (n - wins))
    np.random.default_rng(seed).shuffle(r)
    return r


def test_sixty_percent_bracket_with_fifty_trades_is_not_certified() -> None:
    r = bracket_outcomes(50, 30, 1.0, 0.05)
    c = certify(
        r, p_star=1.05 / 2.0, alpha=0.05, n_trials=1, min_power=0.8, mean_block=None, seed=1
    )
    assert not c.certified
    assert c.insufficient_n
    assert c.n_min is not None
    assert c.n_min > 50
    assert c.margin_not_positive  # LCB of 30/50 is far below 0.525
    assert c.p_hat == 0.6
    assert c.wins == 30


def test_sixty_percent_bracket_with_two_thousand_trades_is_certified() -> None:
    r = bracket_outcomes(2000, 1200, 1.0, 0.05)
    c = certify(
        r, p_star=1.05 / 2.0, alpha=0.05, n_trials=1, min_power=0.8, mean_block=None, seed=1
    )
    assert c.certified, c.reasons
    assert c.reasons == ()
    assert c.n_min is not None
    assert c.n >= c.n_min
    assert c.margin_lcb is not None
    assert c.margin_lcb > 0
    assert c.expectancy_ci.lower > 0
    assert math.isclose(c.expectancy, 0.6 * 0.95 - 0.4 * 1.05, rel_tol=1e-12)
    # p_lcb solves the exact one-sided tail equation P(X >= 1200 | p_lcb) = 0.05
    assert math.isclose(float(exact_tail_ge(1200, 2000, c.p_lcb)), 0.05, rel_tol=1e-8)


def test_ninety_percent_win_rate_with_tiny_take_profit_is_not_certified() -> None:
    """The trap: p_hat = 0.90 looks great but p* = 0.909, so the margin and E are negative."""
    r = bracket_outcomes(2000, 1800, 0.1, 0.0)
    c = certify(r, p_star=1.0 / 1.1, alpha=0.05, n_trials=1, min_power=0.8, mean_block=None, seed=1)
    assert c.p_hat == 0.9
    assert not c.certified
    assert c.margin_not_positive
    assert c.margin_lcb is not None
    assert c.margin_lcb < 0
    assert math.isclose(c.expectancy, -0.01, rel_tol=1e-9)
    assert c.n_min is None  # no positive effect to power


def test_ninety_percent_trap_with_realized_break_even() -> None:
    r = bracket_outcomes(5000, 4500, 0.1, 0.02)
    c = certify(r, p_star=None, alpha=0.05, n_trials=1, min_power=0.8, mean_block=None, seed=3)
    assert c.p_star_source == "realized"
    assert c.p_star is not None
    assert math.isclose(c.p_star, 1.02 / 1.10, rel_tol=1e-12)
    assert not c.certified
    assert c.expectancy_not_positive


def test_multiple_testing_makes_certification_stricter() -> None:
    r = bracket_outcomes(2000, 1200, 1.0, 0.05)
    kw = {"p_star": 0.525, "alpha": 0.05, "min_power": 0.8, "mean_block": None, "seed": 1}
    one = certify(r, n_trials=1, **kw)  # type: ignore[arg-type]
    many = certify(r, n_trials=500, **kw)  # type: ignore[arg-type]
    assert many.alpha_adj == 0.05 / 500
    assert many.p_lcb < one.p_lcb
    assert many.n_min is not None
    assert one.n_min is not None
    assert many.n_min > one.n_min
    # A marginal edge certified alone is rejected after 500 trials: p_hat = 0.565, se = 0.0111;
    # LCB ~ 0.565 - 1.645 se = 0.547 > 0.525 at alpha = 0.05 but ~ 0.565 - 3.72 se = 0.524 < 0.525
    # at alpha = 1e-4.
    weak = bracket_outcomes(2000, 1130, 1.0, 0.05)
    assert certify(weak, n_trials=1, **kw).certified  # type: ignore[arg-type]
    rejected = certify(weak, n_trials=500, **kw)  # type: ignore[arg-type]
    assert not rejected.certified
    assert rejected.margin_not_positive


def test_block_bootstrap_bound_is_used_when_more_conservative() -> None:
    r = bracket_outcomes(2000, 1200, 1.0, 0.05)
    c = certify(r, p_star=0.525, alpha=0.05, n_trials=1, min_power=0.8, mean_block=10.0, seed=2)
    assert c.p_lcb_bootstrap is not None
    assert c.p_lcb == min(c.p_lcb_exact, c.p_lcb_bootstrap)
    # clustered wins (long streaks) inflate the bootstrap spread -> bootstrap bound is lower
    clustered = np.sort(r)  # all losses then all wins: maximal clustering
    cc = certify(
        clustered, p_star=0.525, alpha=0.05, n_trials=1, min_power=0.8, mean_block=50.0, seed=2
    )
    assert cc.p_lcb_bootstrap is not None
    assert cc.p_lcb_bootstrap < cc.p_lcb_exact
    assert cc.p_lcb == cc.p_lcb_bootstrap


def test_zero_loss_sample_without_given_p_star_is_not_certified() -> None:
    c = certify(
        [0.5] * 40, p_star=None, alpha=0.05, n_trials=1, min_power=0.8, mean_block=None, seed=0
    )
    assert c.p_star_undefined
    assert c.margin_lcb is None
    assert not c.certified
    assert c.avg_loss is None
    assert any("average loss not observed" in s for s in c.reasons)


def test_zero_loss_sample_with_bracket_p_star_uses_it() -> None:
    """All 40 trades win: CP lower bound = 0.05^(1/40) = 0.9278 > p* = 0.525."""
    c = certify(
        [0.95] * 40, p_star=0.525, alpha=0.05, n_trials=1, min_power=0.8, mean_block=None, seed=0
    )
    assert c.p_star_source == "given"
    assert math.isclose(c.p_lcb, 0.05 ** (1 / 40), rel_tol=1e-12)
    assert c.margin_lcb is not None
    assert c.margin_lcb > 0
    # n_min at p_alt = 1: exact test rejects iff all n trades win: need 0.525^n <= 0.05 -> n = 5
    assert c.n_min_exact == math.ceil(math.log(0.05) / math.log(0.525))
    assert c.certified  # every outcome-only condition holds (holdout etc. are the caller's)


def test_zero_win_sample() -> None:
    c = certify(
        [-1.0] * 30, p_star=0.5, alpha=0.05, n_trials=1, min_power=0.8, mean_block=None, seed=0
    )
    assert c.wins == 0
    assert c.p_lcb == 0.0
    assert not c.certified
    c2 = certify(
        [-1.0] * 30, p_star=None, alpha=0.05, n_trials=1, min_power=0.8, mean_block=None, seed=0
    )
    assert c2.p_star_undefined
    assert c2.avg_win is None


def test_unprofitable_geometry_p_star_at_least_one() -> None:
    c = certify(
        [0.0] * 10 + [-1.1] * 10,
        p_star=1.0,
        alpha=0.05,
        n_trials=1,
        min_power=0.8,
        mean_block=None,
        seed=0,
    )
    assert not c.certified
    assert any(">= 1" in s for s in c.reasons)


def test_certify_is_deterministic_and_validates_inputs() -> None:
    r = bracket_outcomes(500, 300, 1.0, 0.05)
    kw = {"p_star": 0.525, "alpha": 0.05, "n_trials": 3, "min_power": 0.8, "mean_block": 4.0}
    assert certify(r, seed=5, **kw) == certify(r, seed=5, **kw)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=r"non-empty"):
        certify([], **kw, seed=0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=r"finite"):
        certify([1.0, math.inf], **kw, seed=0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=r"n_trials must be >= 1"):
        certify(r, p_star=0.5, alpha=0.05, n_trials=0, min_power=0.8, mean_block=None, seed=0)
    with pytest.raises(ValueError, match=r"alpha must be in \(0, 1\)"):
        certify(r, p_star=0.5, alpha=1.5, n_trials=1, min_power=0.8, mean_block=None, seed=0)
    with pytest.raises(ValueError, match=r"mean_block must be >= 1"):
        certify(r, p_star=0.5, alpha=0.05, n_trials=1, min_power=0.8, mean_block=0.5, seed=0)


def test_unresolvable_bootstrap_tail_fails_closed() -> None:
    """alpha_adj = 0.05 / 1000 needs >= 10 / 2.5e-5 = 400 000 replicates; cap 50 000 -> fail."""
    r = bracket_outcomes(3000, 2100, 1.0, 0.05)  # strong edge: would otherwise certify
    kw = {"p_star": 0.525, "alpha": 0.05, "n_trials": 1000, "min_power": 0.8, "mean_block": None}
    capped = certify(r, seed=0, max_resamples=50_000, **kw)  # type: ignore[arg-type]
    assert capped.bootstrap_unresolved
    assert not capped.certified
    assert capped.margin_lcb is not None
    assert capped.margin_lcb > 0
    assert any("fail closed" in s for s in capped.reasons)
    small = certify(r[:200], seed=0, **kw)  # type: ignore[arg-type]
    assert not small.bootstrap_unresolved
    assert small.n_resamples == 400_000


def test_tiny_margin_fails_closed_without_raising() -> None:
    """p_hat = 0.5004 vs p* = 0.5 at alpha 0.05/100: n_min ~ 1e8 exceeds the search limit."""
    r = np.array([1.0] * 2501 + [-1.0] * 2497)
    c = certify(r, p_star=0.5, alpha=0.05, n_trials=100, min_power=0.8, mean_block=None, seed=0)
    assert not c.certified
    assert c.insufficient_n
    assert c.n_min is None
    assert any("n_min exceeds" in s for s in c.reasons)
