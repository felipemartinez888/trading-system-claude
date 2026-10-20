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

from tsclaude.research.stats.bootstrap import optimal_block_length, stationary_bootstrap_sums
from tsclaude.research.stats.certify import (
    N_FLOOR,
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
    assert c.n_floor == N_FLOOR
    assert c.n_required == max(N_FLOOR, c.n_min or 0)
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
    assert c.n_floor == N_FLOOR
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
    """188/250 wins, design p*: n_min is met but the single floor N_FLOOR = 300 is not.

    Kills a lowered floor for design p* (Amendment 3.2); the realized case is tested below.
    """
    c = run(bracket_outcomes(250, 188, 1.0, 0.05), p_star=0.525)
    assert failing_gates(c) == {"insufficient_n"}
    assert c.n_min is not None
    assert c.n_min <= 250 < N_FLOOR == 300
    assert c.n_required == N_FLOOR
    assert not c.certified
    # the same win rate and payoffs with 400 trades certify
    assert run(bracket_outcomes(400, 300, 1.0, 0.05), p_star=0.525).certified


def test_gate_effective_sample_size_only() -> None:
    """Kills "n instead of n_eff" (Amendment 3.1): clustered wins, n = 3000 >= n_required = 300,
    but the block-bootstrap variance of the win rate is ~20x the binomial one, so
    n_eff = 147 < 300. Win rate 0.7 vs p* = 0.4 (+1.5R / -1R) keeps every other gate clear.
    """
    pi, rho = 0.7, 0.9
    s = _two_state_chain(np.random.default_rng(2), 3000, pi, 1.0 - (1.0 - rho) * (1.0 - pi))
    c = run(np.where(s, 1.5, -1.0), p_star=0.4)
    assert failing_gates(c) == {"insufficient_n"}
    assert c.n_required is not None
    assert c.n >= c.n_required > c.n_eff
    assert math.isclose(c.n_eff, c.n * c.variance_ratio, rel_tol=1e-12)
    assert c.variance_ratio < 0.1
    assert not c.certified


def test_gate_margin_only_via_undefined_payoff_resamples() -> None:
    """599 wins of +1R and one -0.01R loss, realized p*: about e^-1 = 37 % of resamples contain
    no loss (L* undefined) and count against certification, so the joint bound is -inf although
    the Clopper-Pearson margin, the expectancy and n_eff all pass."""
    c = run(outcomes(600, 599, 1.0, -0.01), p_star=None)
    assert failing_gates(c) == {"margin_not_positive"}
    assert c.margin_lcb_cp is not None
    assert c.margin_lcb_cp > 0.9
    assert c.margin_lcb_joint == -math.inf
    assert not c.certified


def mixed(parts: list[tuple[float, int]], seed: int = 7) -> np.ndarray:
    """Shuffled sample with ``count`` copies of each ``value``."""
    r = np.concatenate([np.full(k, v) for v, k in parts])
    np.random.default_rng(seed).shuffle(r)
    return r


def test_gate_expectancy_only() -> None:
    """E only just positive, win rate far above p*.

    1400 x +0.5R, 500 x -1R, 100 x -1.4R: E = (700 - 500 - 140)/2000 = 0.03, sd 0.7225,
    t = 0.03 / (0.7225/sqrt(2000)) = 1.86. About Phi(-1.86) = 3.2 % of resamples have E* <= 0:
    the two-sided expectancy bound (2.5 % tail) fails, the one-sided joint margin (5 % tail)
    passes (same sign as E* on every resample), and the win rate 0.7 >> p* = 0.5.
    """
    c = run(mixed([(0.5, 1400), (-1.0, 500), (-1.4, 100)]), p_star=0.5)
    assert failing_gates(c) == {"expectancy_not_positive"}
    assert math.isclose(c.expectancy, 0.03, rel_tol=1e-9)
    assert c.margin_lcb_joint is not None
    assert c.margin_lcb_joint > 0
    assert not c.certified


def test_design_p_star_is_checked_against_realized_payoffs() -> None:
    """Amendment 2.2: realized wins of 0.6R vs design p* = 0.525 (wins assumed 0.95R).

    p_hat = 0.6 clears the design break-even, but realized p* = 1.05/1.65 = 0.636 > 0.6, so the
    joint margin is negative and the margin gate fails (E = 0.36 - 0.42 = -0.06R as well).
    """
    c = run(outcomes(2000, 1200, 0.6, -1.05), p_star=0.525)
    assert c.margin_lcb_cp is not None
    assert c.margin_lcb_cp > 0  # the design-only margin would have passed
    assert c.margin_lcb_joint is not None
    assert c.margin_lcb_joint < 0
    assert c.margin_lcb == c.margin_lcb_joint
    assert c.margin_not_positive
    assert not c.certified


def test_gate_degenerate_only() -> None:
    """400 identical outcomes: zero-width bootstrap; the degenerate gate alone rejects it.

    Margin and expectancy bounds are reported but not evaluated on a degenerate bootstrap, so
    deleting the degenerate gate would certify this sample (n_eff = 400 >= 300, E = 0.95R).
    """
    c = run(np.full(400, 0.95), p_star=0.525)
    assert c.degenerate
    assert c.distinct_values == 1
    # zero-width up to prefix-sum rounding (~1e-14)
    assert math.isclose(c.expectancy_ci.lower, c.expectancy_ci.upper, abs_tol=1e-12)
    assert c.margin_lcb_joint == -math.inf  # reported: no resample has a non-win
    assert failing_gates(c) == {"degenerate"}
    assert any("not evaluated" in x for x in c.reasons)
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
        run(r, p_star=0.525, n_floor=N_FLOOR - 1)
    with pytest.raises(ValueError, match=r"may only raise"):
        run(r, p_star=None, n_floor=N_FLOOR - 1)
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
    # no resample has a non-win: the joint bootstrap is all -inf, so the degenerate gate decides
    assert c.degenerate
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


# --------------------------------------------------------------------------------------------
# Re-review N1-N3 (ADR-0012 Amendment 2)
# --------------------------------------------------------------------------------------------


def test_realized_floor_is_300_for_samples_between_100_and_300() -> None:
    """Kills M8 (floor lowered for realized p*): n = 250, 75 % wins, realized p* = 0.525.

    Every other gate passes; with 400 trades the same payoffs certify.
    """
    realized = run(bracket_outcomes(250, 188, 1.0, 0.05), p_star=None)
    assert N_FLOOR == 300
    assert realized.n_floor == 300
    assert failing_gates(realized) == {"insufficient_n"}
    assert run(bracket_outcomes(400, 300, 1.0, 0.05), p_star=None).certified


def test_expectancy_bound_uses_adjusted_alpha() -> None:
    """Kills M12 (nominal alpha in the expectancy bootstrap).

    1400 x +0.5R, 560 x -1R, 40 x -1.4R: E = 0.042, t ~ 2.7. With n_trials = 10
    (alpha_adj = 0.005) the two-sided expectancy bound (0.25 % tail, z = 2.81) fails while the
    one-sided joint margin (0.5 % tail, z = 2.58) passes; at nominal alpha (2.5 % tail) E passes.
    """
    r = mixed([(0.5, 1400), (-1.0, 560), (-1.4, 40)])
    adjusted = run(r, p_star=0.5, n_trials=10, n_resamples=100_000)
    assert failing_gates(adjusted) == {"expectancy_not_positive"}
    assert run(r, p_star=0.5, n_trials=1).certified


def test_joint_margin_uses_adjusted_alpha() -> None:
    """Kills M11 (nominal alpha in the joint-margin bootstrap).

    596 wins, 4 losses: P(resample has no loss) ~ e^-4 = 1.8 %, which lies between
    alpha_adj = 0.5 % (n_trials = 10 -> bound -inf, margin fails) and alpha = 5 % (passes).
    """
    r = mixed([(1.0, 596), (-1.0, 4)])
    adjusted = run(r, p_star=0.5, n_trials=10, n_resamples=100_000)
    assert failing_gates(adjusted) == {"margin_not_positive"}
    assert adjusted.margin_lcb_joint == -math.inf
    assert run(r, p_star=0.5, n_trials=1).certified


def _two_state_chain(rng: np.random.Generator, n: int, pi: float, stay_win: float) -> np.ndarray:
    """Win/loss Markov chain: P(win -> win) = stay_win, stationary win rate pi.

    P(win -> loss) = a = 1 - stay_win, P(loss -> win) = b = a pi / (1 - pi) so pi = b/(a + b);
    lag-1 autocorrelation of the win indicator is 1 - a - b.
    """
    a = 1.0 - stay_win
    b = a * pi / (1.0 - pi)
    u = rng.random(n)
    s = np.empty(n, dtype=bool)
    s[0] = u[0] < pi
    for t in range(1, n):
        s[t] = (u[t] >= a) if s[t - 1] else (u[t] < b)
    return s


def test_win_rate_bound_uses_block_bootstrap_at_adjusted_alpha() -> None:
    """Clustered wins: the block-bootstrap win-rate bound binds and sits below the exact CP bound.

    Under Amendment 3 this bound can no longer be the *only* failing gate (with power >= 0.8,
    ``n_eff >= n_min`` implies it clears p*), so its level is checked on the reported values in
    ``test_reported_bootstrap_bounds_use_adjusted_alpha``.
    """
    pi, rho = 0.56, 0.85
    s = _two_state_chain(np.random.default_rng(0), 3000, pi, 1.0 - (1.0 - rho) * (1.0 - pi))
    c = run(np.where(s, 1.5, -1.0), p_star=0.5, n_trials=10, seed=0)
    assert c.p_lcb_exact > 0.5 > c.p_lcb_bootstrap
    assert c.p_lcb == c.p_lcb_bootstrap
    assert c.block_length > 20
    assert not c.certified


def test_reported_bootstrap_bounds_use_adjusted_alpha() -> None:
    """Kills nominal-alpha mutants in every bootstrap bound and checks the n_eff formula.

    Recomputes the three statistics from the same stationary-bootstrap stream (the reported
    seed, replicate count and block length) and compares the reported bounds with the
    percentiles at alpha_adj = 0.005 (n_trials = 10); nominal alpha gives different values.
    """
    pi, rho = 0.6, 0.6
    s = _two_state_chain(np.random.default_rng(4), 1500, pi, 1.0 - (1.0 - rho) * (1.0 - pi))
    r = np.where(s, 1.2, -1.0)
    c = run(r, p_star=None, n_trials=10, seed=3)
    n = r.size
    cols = np.vstack([(r > 0).astype(float), np.where(r > 0, r, 0.0), np.where(r > 0, 0.0, -r)])
    sums = stationary_bootstrap_sums(
        cols, n_resamples=c.n_resamples, mean_block=c.block_length, seed=c.seed
    )
    win, pos, neg = sums
    w_bar = np.where(win > 0, pos / np.maximum(win, 1), np.nan)
    l_bar = np.where(win < n, neg / np.maximum(n - win, 1), np.nan)
    joint = np.where((win > 0) & (win < n), win / n - l_bar / (w_bar + l_bar), -math.inf)

    def order_stat(x: np.ndarray, q: float, up: bool) -> float:
        xs = np.sort(x)
        pos_ = q * (xs.size - 1)
        return float(xs[math.ceil(pos_) if up else math.floor(pos_)])

    a_adj = 0.05 / 10
    assert c.alpha_adj == a_adj
    assert c.p_lcb_bootstrap == order_stat(win / n, a_adj, False)
    assert c.p_lcb_bootstrap != order_stat(win / n, 0.05, False)
    assert c.margin_lcb_joint == order_stat(joint, a_adj, False)
    assert c.margin_lcb_joint != order_stat(joint, 0.05, False)
    e = (pos - neg) / n
    assert c.expectancy_ci.lower == order_stat(e, a_adj / 2, False)
    assert c.expectancy_ci.upper == order_stat(e, 1 - a_adj / 2, True)
    assert c.expectancy_ci.lower != order_stat(e, 0.025, False)
    ratio = (c.p_hat * (1 - c.p_hat) / n) / float(np.var(win / n))
    assert math.isclose(c.variance_ratio, ratio, rel_tol=1e-12)
    assert math.isclose(c.n_eff, n * min(1.0, ratio), rel_tol=1e-12)


def test_tail_loss_fields_are_reported() -> None:
    """Amendment 3.2: worst trade in R and the share of trades closed beyond -1R."""
    r = np.array([1.5] * 340 + [-1.0] * 55 + [-1.05] * 3 + [-7.5, -44.5])
    c = run(r, p_star=0.4)
    assert c.worst_trade_r == -44.5
    assert c.share_beyond_minus_1r == 5 / 400  # -1.0 itself is not beyond the stop


def test_block_length_never_below_estimates_or_caller_value() -> None:
    s = _two_state_chain(np.random.default_rng(3), 2000, 0.51, 0.98)
    r = np.where(s, 0.95, -1.05)
    auto = run(r, p_star=0.525)
    est = max(optimal_block_length(r), optimal_block_length(s.astype(np.float64)))
    assert auto.block_length == est
    assert auto.block_length > 30  # dependence detected without being told
    assert run(r, p_star=0.525, mean_block=500.0).block_length == 500.0
    iid = run(bracket_outcomes(2000, 1200, 1.0, 0.05), p_star=0.525, mean_block=None)
    assert iid.block_length >= 1.0
    assert iid.mean_block is None


_N1_RUNS = 300


@pytest.fixture(scope="module")
def n1_markov_rate() -> tuple[int, int]:
    """N1: losing strategy with clustered outcomes, nothing declared by the caller.

    Two-state chain, stationary win rate 0.51 (< p* = 0.525: E = 0.51*0.95 - 0.49*1.05 < 0),
    P(win -> win) = 0.98, n = 2000, mean_block=None, alpha = 0.05, one trial. Before Amendment 2
    (independence assumed): 30 % certified. Seeded.
    """
    rng = np.random.default_rng(20261020)
    hits = 0
    for i in range(_N1_RUNS):
        r = np.where(_two_state_chain(rng, 2000, 0.51, 0.98), 0.95, -1.05)
        hits += run(r, p_star=0.525, seed=i).certified
    return hits, _N1_RUNS


def test_n1_markov_false_certification_not_above_alpha(n1_markov_rate: tuple[int, int]) -> None:
    """Rate is not significantly above alpha_adj = 0.05: rate <= 0.05 + 2 SE(0.05).

    Measured (1000 runs each, outside the suite): 0.042 +- 0.006 for P(win -> win) = 0.98 and
    0.049 +- 0.007 for lag-1 correlation 0.98, versus 0.30 before Amendment 2.
    """
    hits, runs = n1_markov_rate
    assert hits / runs <= 0.05 + 2 * math.sqrt(0.05 * 0.95 / runs), hits


def test_n1_markov_false_certification_clearly_below_alpha(
    n1_markov_rate: tuple[int, int],
) -> None:
    """The bar used for the fat-tail case: estimate + 2 SE <= alpha_adj (Amendment 3.1 n_eff gate;
    the floor-1 SE term keeps the bound meaningful when no run certifies)."""
    hits, runs = n1_markov_rate
    rate = hits / runs
    se = math.sqrt(max(rate * (1 - rate), (1 / runs) * (1 - 1 / runs)) / runs)
    assert rate + 2 * se <= 0.05, hits


def test_n2_gap_risk_false_certification_at_the_floor() -> None:
    """N2 at n = N_FLOOR = 300: design p* = 0.4 (TP 1.5R / SL 1R), but 1 % of trades gap to
    -44.5R (+1.5R 56.6 %, -1R 42.4 %, -44.5R 1 %: E = 0.849 - 0.424 - 0.445 = -0.02R).

    Requirement (Amendment 3.2): false-certification rate <= alpha_adj = 0.05.
    Estimator: stratify on K = number of -44.5R trades, K ~ Binomial(300, 0.01) with exact
    weights; strata 0 and 1 get 60 seeded runs, strata 2..8 get 15, and K > 8 (P = 0.0038) is
    bounded by P(cert | K) <= 1. Tolerance: estimate + 2 stratified SE <= 0.05.

    Measured: P(cert | K = 0) ~ 0.6, P(cert | K = 1) ~ 0.02, 0 for K >= 2; rate ~ 0.032.
    Caveat: P(cert | K = 0) < 1 partly because, at exactly n = 300, i.i.d. bootstrap noise puts
    about half of the samples' n_eff just below the floor; without that the K = 0 stratum alone
    would contribute up to 0.049, i.e. the floor puts this worst case at about alpha.
    """
    n, p_tail = 300, 0.01
    weight = [math.comb(n, k) * p_tail**k * (1 - p_tail) ** (n - k) for k in range(n + 1)]
    rng = np.random.default_rng(20261020)
    estimate = sum(weight[9:])
    variance = 0.0
    for k in range(9):
        runs = 60 if k < 2 else 15
        hits = 0
        for i in range(runs):
            rest = np.where(rng.random(n - k) < 0.566 / 0.99, 1.5, -1.0)
            r = np.concatenate([rest, np.full(k, -44.5)])
            rng.shuffle(r)
            hits += run(r, p_star=0.4, seed=1000 * k + i).certified
        q = hits / runs
        estimate += weight[k] * q
        variance += weight[k] ** 2 * q * (1 - q) / runs
    assert estimate + 2 * math.sqrt(variance) <= 0.05, (estimate, math.sqrt(variance))
