"""Normal distribution, break-even algebra and multiple-testing corrections."""

from __future__ import annotations

import itertools
import math
import random
from fractions import Fraction
from statistics import NormalDist

import pytest

from tsclaude.research.stats.breakeven import (
    bracket_break_even,
    break_even_win_rate,
    expectancy,
    expectancy_from_margin,
    expectancy_identity_holds,
    realized_payoffs,
)
from tsclaude.research.stats.multiple_testing import (
    bonferroni_alpha,
    holm_adjusted_alpha,
    holm_adjusted_p_values,
    holm_bonferroni,
)
from tsclaude.research.stats.normal import norm_cdf, norm_pdf, norm_ppf, norm_sf

STD = NormalDist()

# --------------------------------------------------------------------------------------------
# Normal
# --------------------------------------------------------------------------------------------


def test_norm_ppf_matches_stdlib_on_dense_grid() -> None:
    """Absolute error < 1e-9 against ``statistics.NormalDist().inv_cdf`` (brief requirement)."""
    grid = [i / 10_000 for i in range(1, 10_000)]
    grid += [10.0**-e for e in range(3, 17)] + [1 - 10.0**-e for e in range(3, 16)]
    worst = max(abs(norm_ppf(p) - STD.inv_cdf(p)) for p in grid)
    assert worst < 1e-9


def test_norm_ppf_relative_accuracy_and_round_trip() -> None:
    rng = random.Random(3)
    for _ in range(2000):
        p = rng.random()
        if not 0.0 < p < 1.0:
            continue
        x = norm_ppf(p)
        ref = STD.inv_cdf(p)
        assert math.isclose(x, ref, rel_tol=1e-12, abs_tol=1e-14)
        assert math.isclose(norm_cdf(x), p, rel_tol=1e-13)


def test_norm_ppf_extreme_tails() -> None:
    assert norm_ppf(0.0) == -math.inf
    assert norm_ppf(1.0) == math.inf
    for p in (1e-50, 1e-200, 1e-300, 1e-310, 5e-324):
        x = norm_ppf(p)
        assert math.isfinite(x)
        assert x < -8
        assert math.isclose(norm_cdf(x), p, rel_tol=1e-6)  # subnormals have few significant bits
    with pytest.raises(ValueError, match=r"must be in \[0, 1\]"):
        norm_ppf(-0.1)
    with pytest.raises(ValueError, match=r"finite"):
        norm_ppf(math.nan)


def test_norm_cdf_sf_pdf_match_stdlib() -> None:
    for x in [-6.0, -3.3, -1.0, 0.0, 0.5, 1.96, 6.0]:
        # stdlib uses 1 + erf: relative accuracy degrades in the lower tail, hence 1e-9 there
        assert math.isclose(norm_cdf(x), STD.cdf(x), rel_tol=1e-9)
        assert math.isclose(norm_pdf(x), STD.pdf(x), rel_tol=1e-12)
        assert math.isclose(norm_sf(x), STD.cdf(-x), rel_tol=1e-9)


def test_norm_tails_match_mills_ratio_asymptotics() -> None:
    """For x << 0: Phi(x) = phi(x)/|x| * sum_j (-1)^j (2j-1)!! / x^(2j)  (asymptotic series).

    Truncating after the x^-12 term leaves a relative error below 135135/x^14 (~8e-14 at
    |x| = 20), so the check at 1e-11 is limited by phi and erfc rounding, not by the series.
    """
    for x in (-20.0, -30.0, -37.0):
        t = 1.0 / (x * x)
        series = 1 - t + 3 * t**2 - 15 * t**3 + 105 * t**4 - 945 * t**5 + 10395 * t**6
        ref = STD.pdf(x) / abs(x) * series
        assert math.isclose(norm_cdf(x), ref, rel_tol=1e-11)
        assert math.isclose(norm_sf(-x), ref, rel_tol=1e-11)
    assert norm_sf(10.0) > 0.0  # tail-accurate (1 - cdf would be 0)


# --------------------------------------------------------------------------------------------
# Break-even
# --------------------------------------------------------------------------------------------


def test_break_even_identity_on_random_inputs() -> None:
    """E = p W - (1-p) L == (W + L)(p - p*) on random inputs; verified exactly with Fractions."""
    rng = random.Random(99)
    for _ in range(5000):
        p, w, loss = rng.random(), rng.uniform(0, 10), rng.uniform(0, 10)
        if w + loss == 0:
            continue
        assert expectancy_identity_holds(p, w, loss)
        fp, fw, fl = Fraction(p), Fraction(w), Fraction(loss)
        exact = fp * fw - (1 - fp) * fl
        assert math.isclose(expectancy(p, w, loss), float(exact), rel_tol=1e-12, abs_tol=1e-14)
        assert math.isclose(
            expectancy_from_margin(p, w, loss), float(exact), rel_tol=1e-9, abs_tol=1e-12
        )


def test_break_even_values() -> None:
    assert break_even_win_rate(1.0, 1.0) == 0.5
    assert math.isclose(break_even_win_rate(2.0, 1.0), 1 / 3)
    assert break_even_win_rate(0.0, 1.0) == 1.0
    assert break_even_win_rate(1.0, 0.0) == 0.0
    with pytest.raises(ValueError, match=r"undefined"):
        break_even_win_rate(0.0, 0.0)
    with pytest.raises(ValueError, match=r">= 0"):
        break_even_win_rate(-1.0, 1.0)
    with pytest.raises(ValueError, match=r"finite"):
        break_even_win_rate(math.inf, 1.0)


def test_bracket_break_even_equals_realized_break_even() -> None:
    """For bracket payoffs W = b - c, L = 1 + c the two formulas agree exactly."""
    for b, c in itertools.product([0.1, 0.5, 1.0, 1.5, 3.0], [0.0, 0.02, 0.05, 0.2]):
        if b <= c:
            continue
        exact = (1 + Fraction(c)) / (1 + Fraction(b))
        assert math.isclose(bracket_break_even(b, c), float(exact), rel_tol=1e-15)
        assert math.isclose(break_even_win_rate(b - c, 1 + c), float(exact), rel_tol=1e-14)
    assert math.isclose(bracket_break_even(1.0, 0.05), 0.525)
    assert bracket_break_even(0.1, 0.1) == 1.0  # cost eats the whole take-profit
    with pytest.raises(ValueError, match=r"b must be > 0"):
        bracket_break_even(0.0, 0.0)
    with pytest.raises(ValueError, match=r"cost in R must be >= 0"):
        bracket_break_even(1.0, -0.01)


def test_realized_payoffs_identity_is_exact_for_sample_mean() -> None:
    rng = random.Random(5)
    for _ in range(200):
        r = [rng.uniform(-2, 3) for _ in range(rng.randint(2, 60))] + [0.0, 0.25]
        pay = realized_payoffs(r)
        assert pay.wins + pay.losses == len(r)
        assert pay.avg_win is not None
        assert pay.avg_loss is not None
        e = (pay.avg_win + pay.avg_loss) * (
            pay.p_hat - break_even_win_rate(pay.avg_win, pay.avg_loss)
        )
        assert math.isclose(e, math.fsum(r) / len(r), rel_tol=1e-9, abs_tol=1e-12)


def test_realized_payoffs_zero_counts_as_non_win_and_missing_sides_are_none() -> None:
    pay = realized_payoffs([0.0, 0.0, 1.0])
    assert (pay.wins, pay.losses, pay.avg_loss) == (1, 2, 0.0)
    assert realized_payoffs([1.0, 2.0]).avg_loss is None
    assert realized_payoffs([-1.0]).avg_win is None
    with pytest.raises(ValueError, match=r"non-empty"):
        realized_payoffs([])
    with pytest.raises(ValueError, match=r"finite"):
        realized_payoffs([1.0, math.nan])


# --------------------------------------------------------------------------------------------
# Multiple testing
# --------------------------------------------------------------------------------------------


def test_bonferroni_and_holm_thresholds() -> None:
    assert bonferroni_alpha(0.05, 1) == 0.05
    assert bonferroni_alpha(0.05, 20) == 0.0025
    assert holm_adjusted_alpha(0.05, 20, 1) == 0.05 / 20
    assert holm_adjusted_alpha(0.05, 20, 20) == 0.05
    previous = 1.0
    for m in range(1, 200):
        a = bonferroni_alpha(0.05, m)
        assert a < previous or m == 1
        previous = a
    with pytest.raises(ValueError, match=r"rank must satisfy"):
        holm_adjusted_alpha(0.05, 3, 4)
    with pytest.raises(ValueError, match=r"m must be >= 1"):
        bonferroni_alpha(0.05, 0)


def test_holm_textbook_example() -> None:
    """Hand-worked: m = 4, alpha = 0.05, thresholds 0.0125, 0.01667, 0.025, 0.05.

    sorted p = 0.01 (<= 0.0125 reject), 0.015 (<= 0.01667 reject), 0.03 (> 0.025 stop).
    """
    p = [0.03, 0.01, 0.2, 0.015]
    assert holm_bonferroni(p, 0.05) == [False, True, False, True]
    # adjusted: 4*0.01=0.04, max(0.04, 3*0.015=0.045), max(.045, 2*.03=.06), max(.06, .2)
    adj = holm_adjusted_p_values(p)
    assert adj == pytest.approx([0.06, 0.04, 0.2, 0.045], abs=1e-15)
    assert [a <= 0.05 for a in adj] == holm_bonferroni(p, 0.05)


def test_holm_is_at_least_as_powerful_as_bonferroni_and_controls_fwer_by_brute_force() -> None:
    rng = random.Random(11)
    for _ in range(500):
        m = rng.randint(1, 12)
        p = [rng.random() ** 3 for _ in range(m)]
        holm = holm_bonferroni(p, 0.05)
        bonf = [x <= 0.05 / m for x in p]
        assert all(h or not b for h, b in zip(holm, bonf, strict=True))
    # FWER under the global null with independent uniform p-values (Monte Carlo, fixed seed)
    m, sims, false_any = 10, 20_000, 0
    for _ in range(sims):
        false_any += any(holm_bonferroni([rng.random() for _ in range(m)], 0.05))
    se = math.sqrt(0.05 * 0.95 / sims)
    assert false_any / sims <= 0.05 + 3 * se
