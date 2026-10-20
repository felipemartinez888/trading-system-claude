"""Binomial distribution, Clopper-Pearson, Wilson and the exact one-sided test.

Every reference value is derived independently of the code under test: exact rational
summation (``stats_exact_ref``) or closed forms evaluated in the test. Hard-coded literals were
obtained the same way and are re-verified in ``test_hardcoded_cp_literals_are_exact_roots``.
"""

from __future__ import annotations

import math
import random
from fractions import Fraction
from statistics import NormalDist

import numpy as np
import pytest
from stats_exact_ref import exact_cdf, exact_tail_ge

from tsclaude.research.stats.binomial import (
    binom_cdf,
    binom_logpmf,
    binom_pmf,
    binom_sf,
    binomial_test_greater,
    clopper_pearson,
    wilson,
)

# --------------------------------------------------------------------------------------------
# Distribution functions
# --------------------------------------------------------------------------------------------


def test_cdf_small_n_matches_exact_fraction_sum() -> None:
    rng = random.Random(20261019)
    worst = 0.0
    for _ in range(300):
        n = rng.randint(1, 80)
        k = rng.randint(0, n - 1)
        p = rng.random()
        ref = float(exact_cdf(k, n, p))
        got = binom_cdf(k, n, p)
        if ref > 1e-300:
            worst = max(worst, abs(got - ref) / ref)
    assert worst < 1e-12


def test_sf_moderate_n_matches_exact_sum_in_both_tails() -> None:
    """n up to 2000 with p near k/n (the cancellation-prone regime), relative error < 1e-11."""
    rng = random.Random(7)
    for _ in range(25):
        n = rng.choice([200, 1000, 2000])
        k = rng.randint(1, n - 2)
        num = min(max(int((k / n + rng.gauss(0.0, 0.03)) * 1000), 1), 999)
        p = Fraction(num, 1000)
        upper = exact_tail_ge(k + 1, n, p)
        lower = 1 - upper
        for got, ref in ((binom_sf(k, n, num / 1000), upper), (binom_cdf(k, n, num / 1000), lower)):
            assert math.isclose(got, float(ref), rel_tol=1e-11), (n, k, p)


def test_pmf_matches_exact() -> None:
    for n, k, p in [(10, 3, 0.25), (100, 50, 0.5), (1000, 530, 0.5), (60, 0, 0.1), (60, 60, 0.9)]:
        ref = Fraction(math.comb(n, k)) * Fraction(p) ** k * (1 - Fraction(p)) ** (n - k)
        assert math.isclose(binom_pmf(k, n, p), float(ref), rel_tol=1e-13)


def test_large_n_pmf_sums_to_one_and_tails_are_complementary() -> None:
    n, p = 100_000, 0.53
    ks = np.arange(51_000, 55_000)
    total = math.fsum(binom_pmf(int(k), n, p) for k in ks)
    # mass outside +-13 sd is < 1e-30: the window must hold essentially all of it
    assert math.isclose(total, 1.0, rel_tol=1e-12)
    for k in (52_000, 53_000, 54_000):
        assert math.isclose(binom_cdf(k, n, p) + binom_sf(k, n, p), 1.0, rel_tol=0, abs_tol=1e-15)
        # pmf(k) as a difference of the *smaller* tail (differencing values near 1 loses
        # ~log10(1/pmf) digits to cancellation in any floating-point implementation)
        if binom_cdf(k, n, p) <= 0.5:
            diff = binom_cdf(k, n, p) - binom_cdf(k - 1, n, p)
        else:
            diff = binom_sf(k - 1, n, p) - binom_sf(k, n, p)
        assert math.isclose(diff, binom_pmf(k, n, p), rel_tol=1e-9)


def test_large_n_extreme_tail_matches_log_space_sum() -> None:
    """Deep tail at n = 100 000: compare with an independent log-sum-exp of lgamma terms."""
    n, p, k = 100_000, 0.5, 51_500  # z ~ 9.5, tail ~ 1e-21
    terms = [
        math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + n * math.log(p)
        for i in range(k + 1, k + 3000)
    ]
    mx = max(terms)
    ref = math.exp(mx) * math.fsum(math.exp(t - mx) for t in terms)
    # lgamma(1e5) carries ~1e-11 absolute error -> ~1e-11 relative in each term
    assert math.isclose(binom_sf(k, n, p), ref, rel_tol=1e-9)
    assert binom_sf(k, n, p) < 1e-19


def test_edge_arguments() -> None:
    assert binom_cdf(-1, 10, 0.3) == 0.0
    assert binom_cdf(10, 10, 0.3) == 1.0
    assert binom_sf(10, 10, 0.3) == 0.0
    assert binom_sf(-1, 10, 0.3) == 1.0
    assert binom_cdf(3, 10, 0.0) == 1.0
    assert binom_cdf(3, 10, 1.0) == 0.0
    assert binom_logpmf(11, 10, 0.5) == -math.inf
    with pytest.raises(ValueError, match=r"must be in \[0, 1\]"):
        binom_cdf(3, 10, 1.5)
    with pytest.raises(TypeError):
        binom_cdf(3.5, 10, 0.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=r"k must satisfy"):
        clopper_pearson(11, 10)
    with pytest.raises(ValueError, match=r"side must be one of"):
        clopper_pearson(1, 10, side="both")  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------------
# Clopper-Pearson
# --------------------------------------------------------------------------------------------

# Exact two-sided 95 % endpoints (roots of the exact tail equations, 1e-12).
CP_REFERENCE = {
    (0, 10): (0.0, 0.30849710781876083),  # 1 - 0.025**(1/10)
    (10, 10): (0.6915028921812392, 1.0),  # 0.025**(1/10)
    (7, 20): (0.15390920478454, 0.59218853453283),
    (530, 1000): (0.49851631792398, 0.56130658805896),
}


@pytest.mark.parametrize(("k", "n"), list(CP_REFERENCE))
def test_clopper_pearson_known_values(k: int, n: int) -> None:
    lo_ref, hi_ref = CP_REFERENCE[(k, n)]
    lo, hi = clopper_pearson(k, n, 0.05)
    assert abs(lo - lo_ref) < 1e-12
    assert abs(hi - hi_ref) < 1e-12
    # conservative rounding: the returned interval contains the true roots up to tail accuracy
    assert lo <= lo_ref + 1e-13
    assert hi >= hi_ref - 1e-13


def test_clopper_pearson_closed_form_edges() -> None:
    for n in (1, 5, 37, 1000):
        a = 0.025
        assert math.isclose(clopper_pearson(0, n)[1], 1 - a ** (1 / n), rel_tol=1e-14)
        assert math.isclose(clopper_pearson(n, n)[0], a ** (1 / n), rel_tol=1e-14)
        assert clopper_pearson(0, n)[0] == 0.0
        assert clopper_pearson(n, n)[1] == 1.0


def test_hardcoded_cp_literals_are_exact_roots() -> None:
    """The literal endpoints solve the exact tail equations (verified with exact rationals)."""
    target = Fraction(1, 40)  # alpha / 2
    for k, n in [(7, 20), (530, 1000)]:
        lo, hi = CP_REFERENCE[(k, n)]
        # P(X >= k | lo) = 0.025 and P(X <= k | hi) = 0.025, both strictly monotone in p
        assert abs(float(exact_tail_ge(k, n, lo) - target)) < 1e-11
        assert abs(float(1 - exact_tail_ge(k + 1, n, hi) - target)) < 1e-11
        eps = 1e-9
        assert exact_tail_ge(k, n, lo - eps) < target < exact_tail_ge(k, n, lo + eps)


def test_clopper_pearson_one_sided_solves_tail_equation() -> None:
    for k, n, alpha in [(7, 20, 0.05), (530, 1000, 0.05), (530, 1000, 0.001), (1, 3, 0.1)]:
        lower = clopper_pearson(k, n, alpha, "lower")
        upper = clopper_pearson(k, n, alpha, "upper")
        assert lower.upper == 1.0
        assert upper.lower == 0.0
        assert math.isclose(float(exact_tail_ge(k, n, lower.lower)), alpha, rel_tol=1e-9)
        assert math.isclose(float(1 - exact_tail_ge(k + 1, n, upper.upper)), alpha, rel_tol=1e-9)
        two = clopper_pearson(k, n, 2 * alpha)
        assert two.lower == lower.lower
        assert two.upper == upper.upper


def test_clopper_pearson_large_n_is_fast_and_ordered() -> None:
    lo, hi = clopper_pearson(53_000, 100_000, 0.05)
    assert lo < 0.53 < hi
    # normal approximation is excellent here; exact interval must be within 1e-4 of it
    half = NormalDist().inv_cdf(0.975) * math.sqrt(0.53 * 0.47 / 100_000)
    assert abs(lo - (0.53 - half)) < 1e-4
    assert abs(hi - (0.53 + half)) < 1e-4


def test_clopper_pearson_coverage_simulation() -> None:
    """Exact coverage computed by enumeration (not sampling) for a grid of p and n <= 200.

    Coverage(p) = sum_k P(X = k | p) * 1{p in CI(k)}; Clopper-Pearson guarantees >= 1 - alpha.
    Tolerance: 1e-9 for the floating-point sum of probabilities.
    """
    rng = random.Random(1234)
    alpha = 0.05
    for _ in range(40):
        n = rng.randint(1, 200)
        p = rng.uniform(0.001, 0.999)
        cis = [clopper_pearson(k, n, alpha) for k in range(n + 1)]
        cover = math.fsum(
            binom_pmf(k, n, p) for k in range(n + 1) if cis[k].lower <= p <= cis[k].upper
        )
        assert cover >= 1 - alpha - 1e-9, (n, p, cover)


def test_clopper_pearson_monte_carlo_coverage() -> None:
    """Sampled coverage with a fixed seed; >= 1 - alpha minus 3 binomial standard errors."""
    gen = np.random.default_rng(42)
    alpha, draws = 0.05, 400
    for _ in range(10):
        n = int(gen.integers(5, 201))
        p = float(gen.uniform(0.05, 0.95))
        ks = gen.binomial(n, p, size=draws)
        hits = 0
        for k in ks:
            lo, hi = clopper_pearson(int(k), n, alpha)
            hits += lo <= p <= hi
        se = math.sqrt(alpha * (1 - alpha) / draws)
        assert hits / draws >= 1 - alpha - 3 * se


# --------------------------------------------------------------------------------------------
# Wilson and exact test
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("k", "n"), [(7, 20), (530, 1000), (0, 10), (10, 10)])
def test_wilson_matches_closed_form_with_stdlib_quantile(k: int, n: int) -> None:
    z = NormalDist().inv_cdf(0.975)
    p = k / n
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    h = z / (1 + z * z / n) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    lo, hi = wilson(k, n, 0.05)
    assert math.isclose(lo, max(0.0, c - h), abs_tol=1e-12)
    assert math.isclose(hi, min(1.0, c + h), abs_tol=1e-12)


def test_wilson_known_values() -> None:
    lo, hi = wilson(7, 20, 0.05)
    assert abs(lo - 0.1811918241010821) < 1e-9
    assert abs(hi - 0.5671457233147637) < 1e-9


@pytest.mark.parametrize(
    ("k", "n", "p0"),
    [(7, 20, 0.25), (15, 20, 0.5), (60, 100, 0.5), (530, 1000, 0.5), (0, 10, 0.3), (10, 10, 0.9)],
)
def test_binomial_test_greater_matches_exact(k: int, n: int, p0: float) -> None:
    ref = float(exact_tail_ge(k, n, p0))
    assert math.isclose(binomial_test_greater(k, n, p0), ref, rel_tol=1e-12)


def test_binomial_test_greater_known_value() -> None:
    # P(X >= 15 | n=20, p=1/2) = (C(20,15)+...+C(20,20)) / 2^20 = 21700 / 1048576
    assert math.isclose(binomial_test_greater(15, 20, 0.5), 21700 / 1048576, rel_tol=1e-14)
    assert binomial_test_greater(0, 10, 0.3) == 1.0


def test_p_value_and_lower_bound_duality() -> None:
    """p-value <= alpha  iff  the one-sided (1 - alpha) lower bound exceeds p0."""
    for k, n in [(60, 100), (530, 1000), (14, 20)]:
        alpha = 0.05
        lcb = clopper_pearson(k, n, alpha, "lower").lower
        for p0 in (lcb - 1e-6, lcb + 1e-6):
            assert (binomial_test_greater(k, n, p0) <= alpha) == (lcb > p0)
