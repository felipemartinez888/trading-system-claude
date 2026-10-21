"""Property tests for the statistics core (hypothesis; run in CI).

Properties: bounds in [0, 1]; lower <= p_hat <= upper; one-sided bounds nest inside two-sided;
intervals shrink with n at fixed p_hat; Bonferroni/Holm levels decrease with more trials;
break-even identity; exact-test / lower-bound duality; norm_ppf inverts norm_cdf.
"""

from __future__ import annotations

import math

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from tsclaude.research.stats.binomial import (
    binom_cdf,
    binom_sf,
    binomial_test_greater,
    clopper_pearson,
    wilson,
)
from tsclaude.research.stats.breakeven import (
    bracket_break_even,
    break_even_win_rate,
    expectancy,
    expectancy_identity_holds,
)
from tsclaude.research.stats.multiple_testing import (
    bonferroni_alpha,
    holm_adjusted_alpha,
    holm_bonferroni,
)
from tsclaude.research.stats.normal import norm_cdf, norm_ppf

ALPHAS = st.sampled_from([0.001, 0.01, 0.05, 0.1, 0.2])
probs = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)


@st.composite
def counts(draw: st.DrawFn, max_n: int = 5000) -> tuple[int, int]:
    n = draw(st.integers(min_value=1, max_value=max_n))
    k = draw(st.integers(min_value=0, max_value=n))
    return k, n


@settings(max_examples=300, deadline=None)
@given(counts(), ALPHAS)
def test_cp_bounds_in_unit_interval_and_bracket_p_hat(kn: tuple[int, int], alpha: float) -> None:
    k, n = kn
    lo, hi = clopper_pearson(k, n, alpha)
    assert 0.0 <= lo <= k / n <= hi <= 1.0
    one_lo = clopper_pearson(k, n, alpha, "lower").lower
    one_hi = clopper_pearson(k, n, alpha, "upper").upper
    assert lo <= one_lo <= k / n <= one_hi <= hi  # one-sided at alpha is tighter than alpha/2
    wl, wh = wilson(k, n, alpha)
    assert 0.0 <= wl <= k / n <= wh <= 1.0


@settings(max_examples=200, deadline=None)
@given(
    st.integers(min_value=1, max_value=50),
    st.integers(min_value=1, max_value=50),
    st.integers(min_value=2, max_value=20),
    ALPHAS,
)
def test_cp_interval_shrinks_as_n_grows_at_fixed_p_hat(
    k: int, extra: int, factor: int, alpha: float
) -> None:
    n = k + extra  # p_hat = k / n in (0, 1)
    lo1, hi1 = clopper_pearson(k, n, alpha)
    lo2, hi2 = clopper_pearson(k * factor, n * factor, alpha)
    assert hi2 - lo2 < hi1 - lo1
    assert lo2 >= lo1
    assert hi2 <= hi1


@settings(max_examples=200, deadline=None)
@given(counts(1000), st.sampled_from([0.01, 0.05, 0.1]), st.sampled_from([0.001, 0.005]))
def test_cp_lower_bound_decreases_with_stricter_alpha(
    kn: tuple[int, int], a1: float, a2: float
) -> None:
    k, n = kn
    assert clopper_pearson(k, n, a2, "lower").lower <= clopper_pearson(k, n, a1, "lower").lower


@settings(max_examples=300, deadline=None)
@given(counts(3000), st.floats(min_value=0.001, max_value=0.999))
def test_tails_complementary_and_monotone(kn: tuple[int, int], p: float) -> None:
    k, n = kn
    assume(k < n)
    c, s = binom_cdf(k, n, p), binom_sf(k, n, p)
    assert 0.0 <= c <= 1.0
    assert 0.0 <= s <= 1.0
    assert math.isclose(c + s, 1.0, abs_tol=1e-13)
    assert binom_cdf(k + 1, n, p) >= c
    assert binom_cdf(k, n, min(1.0, p * 1.01)) <= c + 1e-15  # decreasing in p


@settings(max_examples=200, deadline=None)
@given(counts(2000), ALPHAS)
def test_test_and_bound_duality(kn: tuple[int, int], alpha: float) -> None:
    k, n = kn
    assume(0 < k < n)
    lcb = clopper_pearson(k, n, alpha, "lower").lower
    assert binomial_test_greater(k, n, max(0.0, lcb - 1e-7)) <= alpha + 1e-12
    assert binomial_test_greater(k, n, min(1.0, lcb + 1e-7)) > alpha - 1e-12


@settings(max_examples=200, deadline=None)
@given(
    st.floats(min_value=1e-6, max_value=0.5),
    st.integers(min_value=1, max_value=10_000),
    st.integers(min_value=1, max_value=10_000),
)
def test_adjusted_alpha_decreases_with_more_trials(alpha: float, m: int, extra: int) -> None:
    assert bonferroni_alpha(alpha, m + extra) < bonferroni_alpha(alpha, m) <= alpha
    assert holm_adjusted_alpha(alpha, m + extra, 1) < holm_adjusted_alpha(alpha, m, 1)
    for rank in (1, m):
        assert holm_adjusted_alpha(alpha, m, rank) >= bonferroni_alpha(alpha, m)


@settings(max_examples=200, deadline=None)
@given(st.lists(probs, min_size=1, max_size=30), st.sampled_from([0.01, 0.05, 0.1]))
def test_holm_dominates_bonferroni_and_is_monotone(p_values: list[float], alpha: float) -> None:
    m = len(p_values)
    holm = holm_bonferroni(p_values, alpha)
    for p, rej in zip(p_values, holm, strict=True):
        if p <= alpha / m:
            assert rej
    # rejected set is a prefix of the sorted order: every rejected p <= every non-rejected p
    rejected = [p for p, r in zip(p_values, holm, strict=True) if r]
    kept = [p for p, r in zip(p_values, holm, strict=True) if not r]
    if rejected and kept:
        assert max(rejected) <= min(kept)


@settings(max_examples=500, deadline=None)
@given(probs, st.floats(min_value=0.0, max_value=100.0), st.floats(min_value=0.0, max_value=100.0))
def test_break_even_identity(p: float, w: float, loss: float) -> None:
    assume(w + loss > 1e-9)
    assert expectancy_identity_holds(p, w, loss)
    ps = break_even_win_rate(w, loss)
    assert 0.0 <= ps <= 1.0
    if p > ps + 1e-9:
        assert expectancy(p, w, loss) > -1e-9 * (w + loss)


@settings(max_examples=300, deadline=None)
@given(st.floats(min_value=0.01, max_value=10.0), st.floats(min_value=0.0, max_value=1.0))
def test_bracket_break_even_matches_payoffs(b: float, c: float) -> None:
    assume(b > c)
    assert math.isclose(
        bracket_break_even(b, c), break_even_win_rate(b - c, 1.0 + c), rel_tol=1e-12
    )


@settings(max_examples=500, deadline=None)
@given(st.floats(min_value=1e-300, max_value=1.0, exclude_max=True))
def test_norm_ppf_inverts_cdf(p: float) -> None:
    x = norm_ppf(p)
    assert math.isclose(norm_cdf(x), p, rel_tol=1e-12, abs_tol=1e-300)
