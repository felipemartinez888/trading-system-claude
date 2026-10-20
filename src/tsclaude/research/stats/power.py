"""Sample size for the one-sided win-rate test ``H0: p <= p*`` vs ``H1: p = p_alt > p*``.

Normal approximation (ADR-0012; e.g. Chow, Shao & Wang, "Sample Size Calculations in Clinical
Research", §4.1, one-sample proportion)::

    n_min = ceil( (z_{1-alpha} sqrt(p*(1-p*)) + z_{power} sqrt(p_alt(1-p_alt)))^2 / (p_alt-p*)^2 )

Exact (``n_min_exact``): the smallest ``n`` at which the *non-randomized* exact binomial test at
level ``alpha`` rejects with probability at least ``power`` when ``p = p_alt``. For each ``n`` the
test rejects iff ``X >= k_c(n)``, where ``k_c(n)`` is the smallest ``k`` with
``P(X >= k | p*) <= alpha``; its power is ``P(X >= k_c(n) | p_alt)``. Because of discreteness
this power is a saw-tooth in ``n`` (not monotone), so the answer is found by an explicit scan, made
fast and rigorous as follows.

*Lower bound for the scan start.* The randomized UMP test of size exactly ``alpha`` (Lehmann &
Romano, "Testing Statistical Hypotheses", 3rd ed., Thm 3.4.1) rejects when ``X >= k_c`` and with
probability ``gamma = (alpha - P(X >= k_c | p*)) / P(X = k_c - 1 | p*)`` when ``X = k_c - 1``.
(i) Its power is non-decreasing in ``n``: a test using the first ``n`` trials is a level-``alpha``
test for ``n + 1`` trials, and the UMP test for ``n + 1`` trials is at least as powerful.
(ii) It dominates the non-randomized test, which is also level ``alpha``. Hence every ``n`` below
``N_r`` (the first ``n`` where the randomized power reaches ``power``, found by bisection thanks to
(i)) has non-randomized power below target, and the scan for ``n_min_exact`` can start at ``N_r``
without skipping any candidate. During the scan ``k_c`` is updated incrementally using
``k_c(n) <= k_c(n+1) <= k_c(n) + 1`` (both follow from ``P_{n}(X >= k) <= P_{n+1}(X >= k) <=
P_{n}(X >= k - 1)``).
"""

from __future__ import annotations

import math
from typing import NamedTuple

from tsclaude.research.stats._validate import as_float, as_int, check_open_prob
from tsclaude.research.stats.binomial import binom_pmf, binom_sf
from tsclaude.research.stats.normal import norm_cdf, norm_ppf

DEFAULT_MAX_N = 10_000_000


class ExactPower(NamedTuple):
    """Rejection region ``X >= k_crit`` of the exact test and its power at ``p_alt``."""

    n: int
    k_crit: int
    size: float
    power: float


def _check_effect(p_star: object, p_alt: object) -> tuple[float, float]:
    ps = as_float(p_star, "p_star")
    pa = as_float(p_alt, "p_alt")
    if not 0.0 <= ps < 1.0:
        raise ValueError(f"p_star must be in [0, 1), got {ps}")
    if not ps < pa <= 1.0:
        raise ValueError(f"p_alt must satisfy p_star < p_alt <= 1, got p_star={ps}, p_alt={pa}")
    return ps, pa


def n_min_one_sided(p_star: float, p_alt: float, alpha: float = 0.05, power: float = 0.80) -> int:
    """Normal-approximation sample size (ADR-0012 formula), rounded up."""
    ps, pa = _check_effect(p_star, p_alt)
    a = check_open_prob(alpha, "alpha")
    pw = check_open_prob(power, "power")
    z_a = norm_ppf(1.0 - a)
    z_b = norm_ppf(pw)
    num = z_a * math.sqrt(ps * (1.0 - ps)) + z_b * math.sqrt(pa * (1.0 - pa))
    if num <= 0.0:
        return 1
    # Guard against ceil() overshooting by one because of rounding in an exactly integral value.
    raw = (num / (pa - ps)) ** 2
    return max(1, math.ceil(raw - 1e-9 * raw))


def normal_approx_power(n: int, p_star: float, p_alt: float, alpha: float = 0.05) -> float:
    """Normal-approximation power of the one-sided test (inverse of ``n_min_one_sided``)."""
    ni = as_int(n, "n")
    ps, pa = _check_effect(p_star, p_alt)
    a = check_open_prob(alpha, "alpha")
    sd_alt = math.sqrt(pa * (1.0 - pa))
    if sd_alt == 0.0:
        return 1.0
    z = ((pa - ps) * math.sqrt(ni) - norm_ppf(1.0 - a) * math.sqrt(ps * (1.0 - ps))) / sd_alt
    return norm_cdf(z)


def critical_value(n: int, p0: float, alpha: float) -> int:
    """Smallest ``k`` in ``[0, n + 1]`` with ``P(X >= k | n, p0) <= alpha`` (``n + 1``: never)."""
    lo, hi = 0, n + 1  # P(X >= 0) = 1 > alpha; P(X >= n+1) = 0 <= alpha
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if binom_sf(mid - 1, n, p0) <= alpha:
            hi = mid
        else:
            lo = mid
    return hi


def exact_test_power(n: int, p_star: float, p_alt: float, alpha: float = 0.05) -> ExactPower:
    """Size and power at ``p_alt`` of the non-randomized exact one-sided binomial test."""
    ni = as_int(n, "n")
    if ni < 1:
        raise ValueError(f"n must be >= 1, got {ni}")
    ps, pa = _check_effect(p_star, p_alt)
    a = check_open_prob(alpha, "alpha")
    k = critical_value(ni, ps, a)
    return ExactPower(ni, k, binom_sf(k - 1, ni, ps), binom_sf(k - 1, ni, pa))


def _randomized_power(n: int, p0: float, p1: float, alpha: float) -> float:
    """Power of the size-``alpha`` randomized UMP test (upper bound of the exact test's power)."""
    k = critical_value(n, p0, alpha)
    size = binom_sf(k - 1, n, p0)
    if k == 0:
        return 1.0
    mass = binom_pmf(k - 1, n, p0)
    gamma = 0.0 if mass <= 0.0 else min(1.0, max(0.0, (alpha - size) / mass))
    return min(1.0, binom_sf(k - 1, n, p1) + gamma * binom_pmf(k - 1, n, p1))


def n_min_exact(
    p_star: float,
    p_alt: float,
    alpha: float = 0.05,
    power: float = 0.80,
    *,
    max_n: int = DEFAULT_MAX_N,
) -> int:
    """Smallest ``n`` whose exact level-``alpha`` test has power ``>= power`` at ``p_alt``.

    Raises ``ValueError`` if no ``n <= max_n`` qualifies (tiny margins need huge samples).
    """
    ps, pa = _check_effect(p_star, p_alt)
    a = check_open_prob(alpha, "alpha")
    pw = check_open_prob(power, "power")
    mx = as_int(max_n, "max_n")
    if mx < 1:
        raise ValueError(f"max_n must be >= 1, got {mx}")

    # 1) N_r = smallest n with randomized power >= target (monotone in n -> bisection).
    hi = min(mx, max(1, n_min_one_sided(ps, pa, a, pw)))
    while _randomized_power(hi, ps, pa, a) < pw:
        if hi >= mx:
            raise ValueError(f"power {pw} at p_alt={pa} not reachable with n <= max_n={mx}")
        hi = min(mx, 2 * hi)
    lo = 0  # sentinel: power at n = 0 is treated as < target
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if _randomized_power(mid, ps, pa, a) >= pw:
            hi = mid
        else:
            lo = mid
    n = hi

    # 2) Scan upward with the non-randomized test from N_r.
    k = critical_value(n, ps, a)
    while True:
        if binom_sf(k - 1, n, pa) >= pw:
            return n
        if n >= mx:
            raise ValueError(f"power {pw} at p_alt={pa} not reachable with n <= max_n={mx}")
        n += 1
        if binom_sf(k - 1, n, ps) > a:  # k_c(n) in {k, k + 1}
            k += 1
