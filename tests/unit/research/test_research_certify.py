"""``certify``: the admission decision of ADR-0012 (with Amendment 1) computable from outcomes.

Scenarios from the task brief, with numbers worked out in the docstrings:

* bracket b = 1, c = 0.05: wins pay 0.95 R, losses cost 1.05 R, p* = 1.05 / 2 = 0.525.
* p = 0.60 -> E = 0.6 * 0.95 - 0.4 * 1.05 = 0.15 R per trade.
* b = 0.1, c = 0 (tiny take-profit): p* = 1 / 1.1 = 0.909; a 90 % win rate *loses*
  E = 0.9 * 0.1 - 0.1 * 1 = -0.01 R per trade: the "fake high win rate" trap.

Gate isolation (Amendment 1.5): for each of the five gates in the ``certified`` expression there
is a case where it is the **only** failing gate, so deleting any gate fails a test.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest
from stats_exact_ref import exact_tail_ge

from tsclaude.research.stats.certify import (
    N_FLOOR_DESIGN,
    N_FLOOR_REALIZED,
    Certification,
    certify,
)

GATES = (
    "insufficient_n",
    "margin_not_positive",
    "expectancy_not_positive",
    "degenerate",
    "bootstrap_unresolved",
)
BASE: dict[str, Any] = {
    "alpha": 0.05,
    "n_trials": 1,
    "min_power": 0.8,
    "mean_block": None,
    "seed": 1,
}


def outcomes(n: int, wins: int, win_r: float, loss_r: float, seed: int = 7) -> np.ndarray:
    r = np.array([win_r] * wins + [loss_r] * (n - wins))
    np.random.default_rng(seed).shuffle(r)
    return r


def bracket_outcomes(n: int, wins: int, b: float, c: float, seed: int = 7) -> np.ndarray:
    return outcomes(n, wins, b - c, -(1.0 + c), seed)


def failing_gates(c: Certification) -> set[str]:
    return {g for g in GATES if getattr(c, g)}


def run(r: Any, **kw: Any) -> Certification:
    return certify(r, **{**BASE, **kw})


# --------------------------------------------------------------------------------------------
# Brief scenarios
# --------------------------------------------------------------------------------------------


def test_sixty_percent_bracket_with_fifty_trades_is_not_certified() -> None:
    c = run(bracket_outcomes(50, 30, 1.0, 0.05), p_star=0.525)
    assert not c.certified
    assert c.insufficient_n
    assert c.n_required is not None
    assert c.n_required > 50
    assert c.margin_not_positive  # LCB of 30/50 is far below 0.525
    assert c.p_hat == 0.6


def test_sixty_percent_bracket_with_two_thousand_trades_is_certified() -> None:
    c = run(bracket_outcomes(2000, 1200, 1.0, 0.05), p_star=0.525)
    assert c.certified, c.reasons
    assert c.reasons == ()
    assert failing_gates(c) == set()
    assert c.n_floor == N_FLOOR_DESIGN
    assert c.n_required == max(N_FLOOR_DESIGN, c.n_min or 0)
    assert math.isclose(c.expectancy, 0.6 * 0.95 - 0.4 * 1.05, rel_tol=1e-12)
    # p_lcb solves the exact one-sided tail equation P(X >= 1200 | p_lcb) = 0.05
    assert math.isclose(float(exact_tail_ge(1200, 2000, c.p_lcb)), 0.05, rel_tol=1e-8)


def test_ninety_percent_win_rate_with_tiny_take_profit_is_not_certified() -> None:
    """The trap: p_hat = 0.90 looks great but p* = 0.909, so the margin and E are negative."""
    c = run(bracket_outcomes(2000, 1800, 0.1, 0.0), p_star=1.0 / 1.1)
    assert c.p_hat == 0.9
    assert not c.certified
    assert c.margin_not_positive
    assert c.expectancy_not_positive
    assert math.isclose(c.expectancy, -0.01, rel_tol=1e-9)
    assert c.n_min is None  # no positive effect to power


def test_ninety_percent_trap_with_realized_break_even() -> None:
    c = run(bracket_outcomes(5000, 4500, 0.1, 0.02), p_star=None, seed=3)
    assert c.p_star_source == "realized"
    assert c.p_star is not None
    assert math.isclose(c.p_star, 1.02 / 1.10, rel_tol=1e-12)
    assert not c.certified
    assert c.expectancy_not_positive


def test_realized_p_star_strategy_certified_and_margin_is_min_of_both_bounds() -> None:
    c = run(bracket_outcomes(2000, 1200, 1.0, 0.05), p_star=None)
    assert c.certified, c.reasons
    assert c.n_floor == N_FLOOR_REALIZED
    assert c.margin_lcb_cp is not None
    assert c.margin_lcb_joint is not None
    assert c.margin_lcb == min(c.margin_lcb_cp, c.margin_lcb_joint)
    # here the Clopper-Pearson component is the binding one (exact bound is conservative)
    assert c.margin_lcb == c.margin_lcb_cp < c.margin_lcb_joint


# --------------------------------------------------------------------------------------------
# One test per gate where it is the only failing condition (Amendment 1.5)
# --------------------------------------------------------------------------------------------


def test_gate_n_min_only() -> None:
    """1100/2000 wins, p* = 0.525: margin and E positive, but n = 2000 < n_min = 2466."""
    c = run(bracket_outcomes(2000, 1100, 1.0, 0.05), p_star=0.525)
    assert failing_gates(c) == {"insufficient_n"}
    assert c.n_min == 2466
    assert not c.certified


def test_gate_sample_floor_only() -> None:
    """60/80 wins, design p*: n_min = 30 is met but the design floor 100 is not."""
    c = run(bracket_outcomes(80, 60, 1.0, 0.05), p_star=0.525)
    assert failing_gates(c) == {"insufficient_n"}
    assert c.n_min is not None
    assert c.n_min <= 80 < N_FLOOR_DESIGN
    assert c.n_required == N_FLOOR_DESIGN
    assert not c.certified


def test_gate_margin_only_via_undefined_payoff_resamples() -> None:
    """299 wins of +1R and one -0.01R loss, realized p*: about e^-1 = 37 % of resamples contain
    no loss (L* undefined) and count against certification, so the joint bound is -inf although
    the Clopper-Pearson margin, the expectancy and n all pass."""
    c = run(outcomes(300, 299, 1.0, -0.01), p_star=None)
    assert failing_gates(c) == {"margin_not_positive"}
    assert c.margin_lcb_cp is not None
    assert c.margin_lcb_cp > 0.9
    assert c.margin_lcb_joint == -math.inf
    assert not c.certified


def test_gate_expectancy_only() -> None:
    """Realized wins of 0.6R vs design p* = 0.525 (wins assumed 0.95R): p_hat = 0.6 passes the
    margin and n gates, but E = 0.6 * 0.6 - 0.4 * 1.05 = -0.06 R."""
    c = run(outcomes(2000, 1200, 0.6, -1.05), p_star=0.525)
    assert failing_gates(c) == {"expectancy_not_positive"}
    assert math.isclose(c.expectancy, -0.06, rel_tol=1e-9)
    assert not c.certified


def test_gate_degenerate_only() -> None:
    """200 identical outcomes: every other gate passes; the zero-width bootstrap must not."""
    c = run(np.full(200, 0.95), p_star=0.525)
    assert failing_gates(c) == {"degenerate"}
    assert c.distinct_values == 1
    assert c.expectancy_ci.lower == c.expectancy_ci.upper  # the zero-width interval
    assert not c.certified


def test_gate_bootstrap_unresolved_only() -> None:
    """alpha_adj = 0.05 / 1000 needs >= 10 / 2.5e-5 = 400 000 replicates; cap 50 000 -> fail."""
    r = bracket_outcomes(3000, 2100, 1.0, 0.05)  # strong edge: would otherwise certify
    capped = run(r, p_star=0.525, n_trials=1000, max_resamples=50_000)
    assert failing_gates(capped) == {"bootstrap_unresolved"}
    assert not capped.certified
    assert any("fail closed" in s for s in capped.reasons)
    small = run(r[:200], p_star=0.525, n_trials=1000)
    assert not small.bootstrap_unresolved
    assert small.n_resamples == 400_000


# --------------------------------------------------------------------------------------------
# Review regressions (F1, F2, F5)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sample",
    [[10.0, 10.0, 10.0, -0.01], [3.0, 3.0, -0.2, 3.0, 3.0, 3.0], [1.0, 1.0, 1.0, 1.0, 1.0, -0.5]],
)
def test_f1_tiny_realized_samples_are_not_certified(sample: list[float]) -> None:
    c = run(sample, p_star=None)
    assert not c.certified
    assert c.insufficient_n
    assert any("sample floor 300" in s for s in c.reasons)


def _fat_tail_losing(rng: np.random.Generator, n: int) -> np.ndarray:
    """+0.5R w.p. 0.85, -0.2R w.p. 0.12, -15R w.p. 0.03: E = 0.425 - 0.024 - 0.45 = -0.049R."""
    u = rng.random(n)
    return np.where(u < 0.85, 0.5, np.where(u < 0.97, -0.2, -15.0))


@pytest.mark.parametrize("n", [10, 20, 40, 80])
def test_f1_fat_tail_losing_strategy_small_n_never_certified(n: int) -> None:
    rng = np.random.default_rng(1000 + n)
    hits = sum(run(_fat_tail_losing(rng, n), p_star=None, seed=i).certified for i in range(50))
    assert hits == 0


def test_f1_fat_tail_losing_strategy_false_certification_rate_at_n_300() -> None:
    """Seeded stratified simulation: losing fat-tail strategy, n = 300, alpha = 0.05, one trial.

    Requirement (Amendment 1): false-certification rate <= alpha_adj = 0.05.

    Estimator: stratify on K = number of -15R losses, K ~ Binomial(300, 0.03), with exact weights
    P(K = k). Given K = k the other 300 - k outcomes are i.i.d. (+0.5R w.p. 0.85/0.97, else -0.2R)
    in uniformly random positions. Strata k <= 3 (P = 0.0199) are bounded by P(cert | k) <= 1
    and k > 20 (P ~ 2e-4) likewise, without simulation (conservative); strata 4..20 are simulated
    with 20 seeded runs each. Tolerance: estimate + 2 stratified standard errors <= 0.05.
    """
    n, p_tail, runs = 300, 0.03, 20
    weight = [math.comb(n, k) * p_tail**k * (1 - p_tail) ** (n - k) for k in range(n + 1)]
    rng = np.random.default_rng(20261020)
    estimate = sum(weight[:4]) + sum(weight[21:])
    variance = 0.0
    for k in range(4, 21):
        hits = 0
        for i in range(runs):
            rest = np.where(rng.random(n - k) < 0.85 / 0.97, 0.5, -0.2)
            r = np.concatenate([rest, np.full(k, -15.0)])
            rng.shuffle(r)
            hits += run(r, p_star=None, seed=100 * k + i).certified
        q = hits / runs
        estimate += weight[k] * q
        variance += weight[k] ** 2 * q * (1 - q) / runs
    assert estimate + 2 * math.sqrt(variance) <= 0.05, (estimate, math.sqrt(variance))


def test_f2_degenerate_all_wins_samples_are_not_certified() -> None:
    for sample, ps in (([0.95] * 5, 0.525), ([0.95] * 40, 0.525), ([0.5] * 500, 0.2)):
        c = run(sample, p_star=ps)
        assert not c.certified
        assert c.degenerate
    # the closed-form CP bound for all-wins is still reported: 0.05^(1/40)
    c40 = run([0.95] * 40, p_star=0.525)
    assert math.isclose(c40.p_lcb, 0.05 ** (1 / 40), rel_tol=1e-12)


def test_f5_policy_floors_reject_loose_alpha_and_power() -> None:
    r = bracket_outcomes(2000, 1080, 1.0, 0.05)  # 54 %
    with pytest.raises(ValueError, match=r"alpha must be <= 0.1"):
        certify(r, p_star=0.525, alpha=0.6, n_trials=1, min_power=0.8, mean_block=None, seed=0)
    with pytest.raises(ValueError, match=r"min_power must be >= 0.8"):
        certify(r, p_star=0.525, alpha=0.05, n_trials=1, min_power=0.05, mean_block=None, seed=0)
    ok = certify(r, p_star=0.525, alpha=0.10, n_trials=1, min_power=0.80, mean_block=None, seed=0)
    assert ok.alpha == 0.10


def test_n_floor_can_be_raised_but_not_lowered() -> None:
    r = bracket_outcomes(2000, 1200, 1.0, 0.05)
    with pytest.raises(ValueError, match=r"may only raise"):
        run(r, p_star=0.525, n_floor=N_FLOOR_DESIGN - 1)
    with pytest.raises(ValueError, match=r"may only raise"):
        run(r, p_star=None, n_floor=N_FLOOR_REALIZED - 1)
    raised = run(r, p_star=0.525, n_floor=5000)
    assert raised.n_required == 5000
    assert failing_gates(raised) == {"insufficient_n"}


# --------------------------------------------------------------------------------------------
# Other behaviour
# --------------------------------------------------------------------------------------------


def test_multiple_testing_makes_certification_stricter() -> None:
    r = bracket_outcomes(2000, 1200, 1.0, 0.05)
    one = run(r, p_star=0.525, n_trials=1)
    many = run(r, p_star=0.525, n_trials=500)
    assert many.alpha_adj == 0.05 / 500
    assert many.p_lcb < one.p_lcb
    assert many.n_min is not None
    assert one.n_min is not None
    assert many.n_min > one.n_min
    # p_hat = 0.565, se = 0.0111: LCB ~ 0.547 > 0.525 at alpha 0.05, ~ 0.524 at alpha 1e-4
    weak = bracket_outcomes(2000, 1130, 1.0, 0.05)
    assert run(weak, p_star=0.525, n_trials=1).certified
    rejected = run(weak, p_star=0.525, n_trials=500)
    assert not rejected.certified
    assert rejected.margin_not_positive


def test_block_bootstrap_bound_is_used_when_more_conservative() -> None:
    r = bracket_outcomes(2000, 1200, 1.0, 0.05)
    c = run(r, p_star=0.525, mean_block=10.0, seed=2)
    assert c.p_lcb_bootstrap is not None
    assert c.p_lcb == min(c.p_lcb_exact, c.p_lcb_bootstrap)
    clustered = np.sort(r)  # all losses then all wins: maximal clustering
    cc = run(clustered, p_star=0.525, mean_block=50.0, seed=2)
    assert cc.p_lcb_bootstrap is not None
    assert cc.p_lcb_bootstrap < cc.p_lcb_exact
    assert cc.p_lcb == cc.p_lcb_bootstrap


def test_zero_loss_sample_without_given_p_star_is_not_certified() -> None:
    c = run([0.5] * 400 + [0.7] * 400, p_star=None)
    assert c.p_star_undefined
    assert c.margin_lcb is None
    assert c.margin_not_positive  # p_star_undefined forces the margin gate
    assert not c.certified
    assert any("average loss not observed" in s for s in c.reasons)


def test_zero_win_sample() -> None:
    c = run([-1.0] * 150 + [-0.5] * 150, p_star=0.5)
    assert c.wins == 0
    assert c.p_lcb == 0.0
    assert not c.certified
    c2 = run([-1.0] * 30, p_star=None)
    assert c2.p_star_undefined
    assert c2.avg_win is None


def test_unprofitable_geometry_p_star_at_least_one() -> None:
    c = run([0.0] * 100 + [-1.1] * 100, p_star=1.0)
    assert not c.certified
    assert any(">= 1" in s for s in c.reasons)


def test_tiny_margin_fails_closed_without_raising() -> None:
    """p_hat = 0.5004 vs p* = 0.5 at alpha 0.05/100: n_min ~ 1e8 exceeds the search limit."""
    r = np.array([1.0] * 2501 + [-1.0] * 2497)
    c = run(r, p_star=0.5, n_trials=100)
    assert not c.certified
    assert c.insufficient_n
    assert c.n_min is None
    assert any("n_min exceeds" in s for s in c.reasons)


def test_certify_is_deterministic_and_validates_inputs() -> None:
    r = bracket_outcomes(500, 300, 1.0, 0.05)
    kw: dict[str, Any] = {"p_star": 0.525, "n_trials": 3, "mean_block": 4.0}
    assert run(r, seed=5, **kw) == run(r, seed=5, **kw)
    with pytest.raises(ValueError, match=r"non-empty"):
        run([], **kw)
    with pytest.raises(ValueError, match=r"finite"):
        run([1.0, math.inf], **kw)
    with pytest.raises(ValueError, match=r"n_trials must be >= 1"):
        run(r, p_star=0.5, n_trials=0)
    with pytest.raises(ValueError, match=r"alpha must be in \(0, 1\)"):
        run(r, p_star=0.5, alpha=1.5)
    with pytest.raises(ValueError, match=r"mean_block must be >= 1"):
        run(r, p_star=0.5, mean_block=0.5)
