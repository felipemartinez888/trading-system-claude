"""Stationary bootstrap, automatic block length, Sharpe / PSR / DSR."""

from __future__ import annotations

import itertools
import math
from statistics import NormalDist

import numpy as np
import pytest

from tsclaude.research.stats.bootstrap import (
    bootstrap_ci,
    bootstrap_distribution,
    bootstrap_statistics,
    optimal_block_length,
    percentile_bounds,
    replicates_degenerate,
    row_mean,
    stationary_bootstrap_indices,
)
from tsclaude.research.stats.sharpe import (
    EULER_MASCHERONI,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    probabilistic_sharpe_ratio,
    sample_moments,
    sharpe,
)

STD = NormalDist()

# --------------------------------------------------------------------------------------------
# Stationary bootstrap
# --------------------------------------------------------------------------------------------


def test_indices_shape_range_and_block_structure() -> None:
    idx = stationary_bootstrap_indices(50, 4.0, np.random.default_rng(0), n_resamples=300)
    assert idx.shape == (300, 50)
    assert idx.min() >= 0
    assert idx.max() < 50
    # every step either continues the block circularly or starts a new one
    cont = (idx[:, 1:] - idx[:, :-1]) % 50 == 1
    starts = 1 + (~cont).sum(axis=1)
    # mean block length = n / (#blocks); expected #new-block flags = 1 + (n - 1) q (q = 1/4)
    # (a uniform restart lands on the continuation index with prob 1/n, slightly hiding starts)
    expected_blocks = 1 + 49 * 0.25 * (1 - 1 / 50)
    assert abs(starts.mean() - expected_blocks) < 0.5


def test_iid_bootstrap_when_mean_block_is_one() -> None:
    idx = stationary_bootstrap_indices(1000, 1.0, np.random.default_rng(3), n_resamples=20)
    counts = np.bincount(idx.ravel(), minlength=1000)
    # uniform multinomial: mean count 20, chi-square-like spread; sanity bound only
    assert counts.mean() == 20
    assert counts.max() < 50


def test_mean_block_length_matches_geometric_mean() -> None:
    n, mb = 5000, 8.0
    idx = stationary_bootstrap_indices(n, mb, np.random.default_rng(1), n_resamples=40)
    cont = (idx[:, 1:] - idx[:, :-1]) % n == 1
    observed = n / (1 + (~cont).sum(axis=1)).mean()
    assert abs(observed - mb) < 0.4


def test_bootstrap_is_deterministic_by_seed() -> None:
    x = np.random.default_rng(9).standard_normal(300)
    a = bootstrap_ci(x, np.mean, 0.05, 500, 5.0, seed=123)
    b = bootstrap_ci(x, np.mean, 0.05, 500, 5.0, seed=123)
    c = bootstrap_ci(x, np.mean, 0.05, 500, 5.0, seed=124)
    assert a == b
    assert a != c
    v = bootstrap_ci(x, row_mean, 0.05, 500, 5.0, seed=123, vectorized=True)
    assert v == a  # same index stream -> same replicates whichever statistic form is used


def test_percentile_bounds_are_conservative_order_statistics() -> None:
    reps = np.arange(1.0, 101.0)  # 1..100
    lo, hi = percentile_bounds(reps, 0.05)
    # target positions 0.025*99 = 2.475 -> lower order statistic index 2 (value 3);
    # 0.975*99 = 96.525 -> higher index 97 (value 98)
    assert (lo, hi) == (3.0, 98.0)
    assert percentile_bounds(reps, 0.1, "lower") == (10.0, math.inf)  # 0.1*99=9.9 -> idx 9
    assert percentile_bounds(reps, 0.1, "upper") == (-math.inf, 91.0)  # 0.9*99=89.1 -> idx 90


def test_bootstrap_mean_ci_coverage_iid_normal() -> None:
    """i.i.d. N(0.3, 1), n = 80, 95 % percentile CI: coverage near nominal in 300 replications.

    The percentile interval for the mean is slightly liberal at small n (its width uses the
    plug-in sd), so the band is [0.90, 0.99]: about nominal, which is what the brief asks.
    """
    gen = np.random.default_rng(2026)
    reps, hits, mu = 300, 0, 0.3
    for i in range(reps):
        x = gen.normal(mu, 1.0, size=80)
        lo, hi = bootstrap_ci(x, row_mean, 0.05, 1000, None, seed=i, vectorized=True)
        hits += lo <= mu <= hi
    assert 0.90 <= hits / reps <= 0.99


def test_block_bootstrap_widens_ci_for_autocorrelated_series() -> None:
    gen = np.random.default_rng(5)
    n, phi = 2000, 0.7
    e = gen.standard_normal(n)
    x = np.empty(n)
    x[0] = e[0]
    for t in range(1, n):
        x[t] = phi * x[t - 1] + e[t]
    iid = bootstrap_ci(x, row_mean, 0.05, 2000, None, seed=1, vectorized=True)
    blk = bootstrap_ci(x, row_mean, 0.05, 2000, 30.0, seed=1, vectorized=True)
    # long-run variance ratio (1 + phi)/(1 - phi) = 5.67 -> width ratio ~ 2.4
    ratio = (blk.upper - blk.lower) / (iid.upper - iid.lower)
    assert 1.8 < ratio < 3.0


def test_optimal_block_length_ar1_against_closed_form() -> None:
    """For AR(1): G/g(0) -> 2 phi / (1 - phi^2) so b_opt = (2 phi/(1 - phi^2))^(2/3) n^(1/3).

    (From R(k) = s^2 phi^|k|: sum |k| R(k) = 2 s^2 phi/(1-phi)^2, sum R(k) = s^2 (1+phi)/(1-phi).)
    Estimator noise at n = 20 000 is ~10 %; tolerance 30 %.
    """
    gen = np.random.default_rng(77)
    n = 20_000
    for phi in (0.3, 0.5, 0.8):
        e = gen.standard_normal(n)
        x = np.empty(n)
        x[0] = e[0]
        for t in range(1, n):
            x[t] = phi * x[t - 1] + e[t]
        theory = (2 * phi / (1 - phi * phi)) ** (2 / 3) * n ** (1 / 3)
        assert abs(optimal_block_length(x) / theory - 1) < 0.3


def test_optimal_block_length_white_noise_and_edges() -> None:
    x = np.random.default_rng(1).standard_normal(5000)
    assert optimal_block_length(x) < 3.0
    assert optimal_block_length([1.0, 1.0, 1.0, 1.0, 1.0]) == 1.0
    assert optimal_block_length([1.0, 2.0]) == 1.0
    with pytest.raises(ValueError, match=r"mean_block must be >= 1"):
        stationary_bootstrap_indices(10, 0.5, np.random.default_rng(0))


# --------------------------------------------------------------------------------------------
# Sharpe, PSR, DSR
# --------------------------------------------------------------------------------------------


def test_sharpe_and_moments_hand_computed() -> None:
    x = [1.0, 2.0, 3.0, 4.0]
    # mean 2.5, sample sd sqrt(5/3); population skew 0; m2 = 1.25, m4 = 2.5625 -> kurt 1.64
    assert math.isclose(sharpe(x), 2.5 / math.sqrt(5 / 3), rel_tol=1e-14)
    assert math.isclose(sharpe(x, 252), 2.5 / math.sqrt(5 / 3) * math.sqrt(252), rel_tol=1e-14)
    m = sample_moments(x)
    assert m.skew == pytest.approx(0.0, abs=1e-15)
    assert math.isclose(m.kurtosis, 2.5625 / 1.5625, rel_tol=1e-14)
    with pytest.raises(ValueError, match=r"zero variance"):
        sharpe([1.0, 1.0])


def test_psr_closed_form() -> None:
    sr, n, g3, g4 = 0.1, 500, -0.5, 6.0
    z = (sr - 0.02) * math.sqrt(n - 1) / math.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr * sr)
    assert math.isclose(probabilistic_sharpe_ratio(sr, 0.02, n, g3, g4), STD.cdf(z), rel_tol=1e-12)
    assert probabilistic_sharpe_ratio(0.0, 0.0, 100) == 0.5


def test_dsr_published_example_bailey_lopez_de_prado_2014() -> None:
    """Bailey & López de Prado (2014), JPM, numerical example (their §"A numerical example").

    Annualized SR 2.5 on daily data (250/yr) over 5 years (T = 1250), skew -3, kurtosis 10,
    N = 100 trials whose annualized SR variance is 0.5. The paper reports E[max SR] ~ 1.79
    annualized (SR_0 = 0.1132 daily) and DSR ~ 0.90. Recomputed here from the closed forms
    with stdlib NormalDist: SR_0 = sqrt(0.5/250) * ((1-g) z(0.99) + g z(1 - 1/(100 e))).
    """
    g = 0.5772156649015329
    v = 0.5 / 250
    sr0 = math.sqrt(v) * (
        (1 - g) * STD.inv_cdf(1 - 1 / 100) + g * STD.inv_cdf(1 - 1 / (100 * math.e))
    )
    assert math.isclose(sr0 * math.sqrt(250), 1.7894, abs_tol=1e-3)  # paper: 1.79
    assert math.isclose(expected_max_sharpe(100, v), sr0, rel_tol=1e-12)
    sr = 2.5 / math.sqrt(250)
    z = (sr - sr0) * math.sqrt(1249) / math.sqrt(1 + 3 * sr + 9 / 4 * sr * sr)
    dsr = deflated_sharpe_ratio(sr, 1250, -3.0, 10.0, 100, v)
    assert math.isclose(dsr, STD.cdf(z), rel_tol=1e-12)
    assert math.isclose(dsr, 0.9004, abs_tol=1e-3)  # paper: 0.9004


def test_dsr_limits() -> None:
    sr, n, g3, g4 = 0.08, 1000, 0.0, 3.0
    assert deflated_sharpe_ratio(sr, n, g3, g4, 1, 0.01) == probabilistic_sharpe_ratio(
        sr, 0.0, n, g3, g4
    )
    values = [deflated_sharpe_ratio(sr, n, g3, g4, m, 0.001) for m in (1, 2, 5, 10, 100, 1000)]
    assert all(b < a for a, b in itertools.pairwise(values))
    assert expected_max_sharpe(50, 0.0) == 0.0
    assert abs(EULER_MASCHERONI - 0.5772156649015329) < 1e-16


def test_expected_max_approximation_vs_simulation() -> None:
    """E[max of N std normals]: the approximation is within 0.1 of a Monte Carlo estimate."""
    gen = np.random.default_rng(0)
    for n_trials in (10, 100, 1000):
        mc = float(gen.standard_normal((4000, n_trials)).max(axis=1).mean())
        assert abs(expected_max_sharpe(n_trials, 1.0) - mc) < 0.1


def test_expected_max_sharpe_huge_trial_count_is_finite() -> None:
    """F4 analogue: 1 - 1/N rounds to 1.0 for N > 2^53; the reflected quantile stays finite."""
    v = expected_max_sharpe(10**17, 1.0)
    assert math.isfinite(v)
    assert 8.0 < v < 9.5  # E[max of 1e17 normals] ~ sqrt(2 ln N) - ... ~ 8.6


def test_bootstrap_statistics_share_one_stream_and_allow_marked_neg_inf() -> None:
    x = np.random.default_rng(4).standard_normal(120)
    both = bootstrap_statistics(
        x, {"mean": row_mean, "max": lambda s: np.max(s, axis=1)}, n_resamples=300, seed=9
    )
    assert np.array_equal(
        both["mean"], bootstrap_distribution(x, row_mean, n_resamples=300, seed=9, vectorized=True)
    )

    def undefined_half(s: np.ndarray) -> np.ndarray:
        out = np.mean(s, axis=1)
        out[::2] = -math.inf
        return out

    reps = bootstrap_statistics(
        x, {"u": undefined_half}, n_resamples=100, seed=1, allow_neg_inf=("u",)
    )
    assert percentile_bounds(reps["u"], 0.05, "lower").lower == -math.inf
    with pytest.raises(ValueError, match=r"non-finite"):
        bootstrap_statistics(x, {"u": undefined_half}, n_resamples=100, seed=1)


def test_replicates_degenerate() -> None:
    assert replicates_degenerate(np.full(10, 0.3))
    assert not replicates_degenerate(np.array([0.3, 0.3, 0.31]))
