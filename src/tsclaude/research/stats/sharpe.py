"""Sharpe ratio, Probabilistic Sharpe Ratio and Deflated Sharpe Ratio.

Conventions
-----------
* ``sr`` arguments of ``probabilistic_sharpe_ratio`` / ``deflated_sharpe_ratio`` are **per-period**
  (not annualized) Sharpe ratios measured on the same ``n`` observations; ``var_trials`` is the
  variance of the per-period Sharpe ratios across the trials.
* ``kurtosis`` is the **non-excess** (Pearson) kurtosis, equal to 3 for a normal distribution, as in
  the papers; ``skew`` and ``kurtosis`` are the population (biased) moment ratios of
  ``sample_moments``.

Sharpe ratio: ``SR = mean(r) / std(r, ddof=1)``; annualized by ``sqrt(periods_per_year)`` (Lo 2002
notes this scaling is exact only for i.i.d. returns).

Probabilistic Sharpe Ratio — Bailey, D. H. & López de Prado, M. (2012), "The Sharpe Ratio Efficient
Frontier", Journal of Risk 15(2), eq. for PSR (based on Mertens 2002 for the asymptotic variance of
the Sharpe ratio under non-normal i.i.d. returns)::

    PSR(SR*) = Phi( (SR_hat - SR*) sqrt(n - 1) / sqrt(1 - g3 SR_hat + (g4 - 1)/4 SR_hat^2) )

Deflated Sharpe Ratio — Bailey, D. H. & López de Prado, M. (2014), "The Deflated Sharpe Ratio:
Correcting for Selection Bias, Backtest Overfitting and Non-Normality", Journal of Portfolio
Management 40(5), 94-107: ``DSR = PSR(SR_0)`` with the expected maximum of ``N`` independent trial
Sharpe ratios under the null of no skill::

    SR_0 = sqrt(V[SR]) * ( (1 - gamma) Phi^-1(1 - 1/N) + gamma Phi^-1(1 - 1/(N e)) )

where ``gamma`` is the Euler-Mascheroni constant. The bracket is an asymptotic approximation of
``E[max of N standard normals]``. For ``N = 1`` it is undefined (``Phi^-1(0) = -inf``) while the
exact expectation is 0, so ``SR_0 = 0`` is used and DSR reduces to ``PSR(0)``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import NamedTuple

import numpy as np
from numpy.typing import NDArray

from tsclaude.research.stats._validate import as_float, as_int
from tsclaude.research.stats.normal import norm_cdf, norm_ppf

EULER_MASCHERONI = 0.57721566490153286060651209008240243


class Moments(NamedTuple):
    n: int
    mean: float
    std: float  # ddof = 1
    skew: float  # population g1 = m3 / m2^(3/2)
    kurtosis: float  # population, non-excess: m4 / m2^2


def _as_returns(values: Sequence[float] | NDArray[np.float64]) -> NDArray[np.float64]:
    x = np.asarray(values, dtype=np.float64)
    if x.ndim != 1 or x.size < 2:
        raise ValueError("need a 1-D sequence with at least 2 values")
    if not np.all(np.isfinite(x)):
        raise ValueError("values must be finite")
    return x


def sample_moments(values: Sequence[float] | NDArray[np.float64]) -> Moments:
    """Mean, sample std (ddof=1), population skewness and non-excess kurtosis."""
    x = _as_returns(values)
    mean = float(np.mean(x))
    d = x - mean
    m2 = float(np.mean(d * d))
    if m2 == 0.0:
        raise ValueError("values have zero variance; Sharpe ratio undefined")
    m3 = float(np.mean(d**3))
    m4 = float(np.mean(d**4))
    return Moments(
        n=int(x.size),
        mean=mean,
        std=float(np.std(x, ddof=1)),
        skew=m3 / m2**1.5,
        kurtosis=m4 / (m2 * m2),
    )


def sharpe(values: Sequence[float] | NDArray[np.float64], periods_per_year: float = 1.0) -> float:
    """``mean / std(ddof=1) * sqrt(periods_per_year)`` (``periods_per_year = 1``: per-period)."""
    ppy = as_float(periods_per_year, "periods_per_year")
    if ppy <= 0.0:
        raise ValueError(f"periods_per_year must be > 0, got {ppy}")
    m = sample_moments(values)
    return m.mean / m.std * math.sqrt(ppy)


def _psr_z(sr: float, sr_benchmark: float, n: int, skew: float, kurtosis: float) -> float:
    var_term = 1.0 - skew * sr + (kurtosis - 1.0) / 4.0 * sr * sr
    if var_term <= 0.0:
        raise ValueError(
            "non-positive Sharpe-ratio variance term 1 - g3*SR + (g4-1)/4*SR^2; "
            "check that kurtosis is non-excess (normal = 3)"
        )
    return (sr - sr_benchmark) * math.sqrt(n - 1) / math.sqrt(var_term)


def probabilistic_sharpe_ratio(
    sr: float, sr_benchmark: float, n: int, skew: float = 0.0, kurtosis: float = 3.0
) -> float:
    """``P(true SR > sr_benchmark)`` given observed per-period ``sr`` over ``n`` observations."""
    srf = as_float(sr, "sr")
    srb = as_float(sr_benchmark, "sr_benchmark")
    ni = as_int(n, "n")
    if ni < 2:
        raise ValueError(f"n must be >= 2, got {ni}")
    g3 = as_float(skew, "skew")
    g4 = as_float(kurtosis, "kurtosis")
    if g4 < 1.0:
        raise ValueError(f"non-excess kurtosis must be >= 1, got {g4}")
    return norm_cdf(_psr_z(srf, srb, ni, g3, g4))


def expected_max_sharpe(n_trials: int, var_trials: float) -> float:
    """``SR_0``: approximate expected maximum of ``n_trials`` null Sharpe ratios (module doc)."""
    nt = as_int(n_trials, "n_trials")
    if nt < 1:
        raise ValueError(f"n_trials must be >= 1, got {nt}")
    v = as_float(var_trials, "var_trials")
    if v < 0.0:
        raise ValueError(f"var_trials must be >= 0, got {v}")
    if nt == 1:
        return 0.0
    g = EULER_MASCHERONI
    # Phi^-1(1 - u) = -Phi^-1(u): avoids rounding 1 - u for very large n_trials
    z = -((1.0 - g) * norm_ppf(1.0 / nt) + g * norm_ppf(1.0 / (nt * math.e)))
    return math.sqrt(v) * z


def deflated_sharpe_ratio(
    sr: float,
    n: int,
    skew: float,
    kurtosis: float,
    n_trials: int,
    var_trials: float,
) -> float:
    """``DSR = PSR(SR_0)`` with ``SR_0 = expected_max_sharpe(n_trials, var_trials)``."""
    return probabilistic_sharpe_ratio(
        sr, expected_max_sharpe(n_trials, var_trials), n, skew, kurtosis
    )
