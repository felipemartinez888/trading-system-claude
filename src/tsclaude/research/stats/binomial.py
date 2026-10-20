"""Exact binomial distribution, exact (Clopper-Pearson) confidence bounds and exact tests.

All functions are pure Python (``math`` only) and numerically stable for ``n`` well beyond 100 000.

Probability mass function
-------------------------
``binom_pmf`` uses C. Loader's saddle-point expansion (Loader 2000, "Fast and accurate computation
of binomial probabilities"), the algorithm behind R's ``dbinom``::

    log f(k; n, p) = stirlerr(n) - stirlerr(k) - stirlerr(n-k)
                     - bd0(k, n p) - bd0(n-k, n q) - 0.5 log(2 pi k (n-k) / n)

where ``stirlerr(m) = log m! - [(m + 1/2) log m - m + log sqrt(2 pi)]`` is the Stirling-series error
and ``bd0(x, M) = x log(x/M) + M - x`` is the binomial deviance, evaluated by a series when
``x ~ M`` to avoid cancellation. This keeps ~1e-15 relative accuracy where the naive
``exp(lgamma(...) + k log p + ...)`` loses ~log10(n) digits to cancellation.

Tail probabilities
------------------
The binomial tails are regularized incomplete beta functions (Abramowitz & Stegun 26.5.24;
DLMF 8.17.5)::

    P(X <= k) = I_{1-p}(n - k, k + 1)        P(X >= k + 1) = I_p(k + 1, n - k)

``I_x(a, b)`` is evaluated with the continued fraction of DLMF 8.17.22 (Numerical Recipes 3rd ed.
§6.4, ``betacf``) using the modified Lentz algorithm (Lentz 1976; Thompson & Barnett 1986).
For integer arguments the prefactor ``x^a (1-x)^b / (a B(a, b))`` equals exactly
``p * f(k)`` (lower tail) or ``q * f(k+1)`` (upper tail), so it is computed from the accurate pmf
above. The continued fraction converges quickly for ``x < (a+1)/(a+b+2)``; in that regime it is
evaluated directly and the other tail is obtained as the complement. Because that regime is always
the *smaller* tail, each tail keeps full relative precision exactly where it is small.

Exact confidence bounds
-----------------------
Clopper & Pearson (1934). For ``k`` successes in ``n`` trials the one-sided ``1 - a`` bounds are::

    lower: p_L solves P(X >= k | p_L) = a     (p_L = 0 when k = 0)
    upper: p_U solves P(X <= k | p_U) = a     (p_U = 1 when k = n)

and the two-sided ``1 - alpha`` interval uses ``a = alpha / 2`` on each side. The tail equations
are solved by bisection on [0, 1] (both tails are strictly monotone in ``p``), iterated until the
bracket is narrower than 1e-12 *relative* to the bound (hence also 1e-12 absolute); the endpoint
returned is the one on the conservative side of the bracket (lower bounds are never rounded up,
upper bounds never rounded down, up to the ~1e-15 accuracy of the tail evaluation). The edge
cases have closed forms used directly: ``k = 0 -> p_U = 1 - a^(1/n)``, ``k = n -> p_L = a^(1/n)``
(correctly rounded to within an ulp).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import NamedTuple

from tsclaude.research.stats._validate import (
    Side,
    as_int,
    check_count,
    check_open_prob,
    check_prob,
    check_side,
)
from tsclaude.research.stats.normal import norm_ppf

_LOG_SQRT_2PI = 0.5 * math.log(2.0 * math.pi)
_LOG_2PI = math.log(2.0 * math.pi)
_CF_EPS = 1e-16
_CF_TINY = 1e-300
_CF_MAX_ITER = 1_000_000
_BISECT_REL_TOL = 1e-12
_LOG_UNDERFLOW = -745.2  # below log(min subnormal double) the result is exactly 0.0


class Interval(NamedTuple):
    """A closed interval ``[lower, upper]`` of probabilities."""

    lower: float
    upper: float


# ---------------------------------------------------------------------------------------------
# Probability mass function (Loader 2000)
# ---------------------------------------------------------------------------------------------


def _stirlerr(m: int) -> float:
    """``log m! - ((m + 1/2) log m - m + log sqrt(2 pi))`` for integer ``m >= 1``.

    For ``m > 15`` the asymptotic Stirling series
    ``1/(12m) - 1/(360m^3) + 1/(1260m^5) - 1/(1680m^7) + 1/(1188m^9)`` is used (truncation error
    below ``691/(360360 m^11) < 1e-15`` for ``m > 15``); otherwise the definition with the exact
    integer ``m!`` (absolute error ~1e-14 from the final subtraction, i.e. ~1e-14 relative in the
    pmf).
    """
    if m <= 15:
        return math.log(math.factorial(m)) - (m + 0.5) * math.log(m) + m - _LOG_SQRT_2PI
    mm = float(m) * float(m)
    s0, s1, s2, s3, s4 = 1 / 12, 1 / 360, 1 / 1260, 1 / 1680, 1 / 1188
    return (s0 - (s1 - (s2 - (s3 - s4 / mm) / mm) / mm) / mm) / m


def _bd0(x: float, big_m: float) -> float:
    """Binomial deviance ``x log(x/M) + M - x`` (Loader 2000), stable when ``x ~ M``.

    With ``v = (x - M)/(x + M)``: ``bd0 = (x - M) v + 2x (v^3/3 + v^5/5 + ...)``.
    """
    if abs(x - big_m) < 0.1 * (x + big_m):
        v = (x - big_m) / (x + big_m)
        s = (x - big_m) * v
        ej = 2.0 * x * v
        v2 = v * v
        j = 1
        while True:
            ej *= v2
            s1 = s + ej / (2 * j + 1)
            if s1 == s:
                return s1
            s = s1
            j += 1
    return x * math.log(x / big_m) + big_m - x


def binom_logpmf(k: int, n: int, p: float) -> float:
    """``log P(X = k)`` for ``X ~ Binomial(n, p)``; ``-inf`` where the mass is zero."""
    ki = as_int(k, "k")
    ni = as_int(n, "n")
    if ni < 0:
        raise ValueError(f"n must be >= 0, got {ni}")
    pf = check_prob(p)
    if ki < 0 or ki > ni:
        return -math.inf
    q = 1.0 - pf
    if pf == 0.0:
        return 0.0 if ki == 0 else -math.inf
    if q == 0.0:
        return 0.0 if ki == ni else -math.inf
    if ki == 0:
        # n log q, with log q = log1p(-p) exact for small p
        return ni * math.log1p(-pf)
    if ki == ni:
        return ni * math.log(pf)
    lc = (
        _stirlerr(ni)
        - _stirlerr(ki)
        - _stirlerr(ni - ki)
        - _bd0(float(ki), ni * pf)
        - _bd0(float(ni - ki), ni * q)
    )
    lf = _LOG_2PI + math.log(ki) + math.log1p(-ki / ni)
    return lc - 0.5 * lf


def binom_pmf(k: int, n: int, p: float) -> float:
    """``P(X = k)`` for ``X ~ Binomial(n, p)`` (Loader's saddle-point algorithm)."""
    return math.exp(binom_logpmf(k, n, p))


# ---------------------------------------------------------------------------------------------
# Incomplete beta continued fraction and the two tails
# ---------------------------------------------------------------------------------------------


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for ``I_x(a, b)`` (DLMF 8.17.22) by the modified Lentz method.

    Returns ``h`` such that ``I_x(a, b) = x^a (1-x)^b / (a B(a, b)) * h``. Converges rapidly for
    ``x < (a + 1)/(a + b + 2)``. Raises ``ArithmeticError`` if it fails to converge.
    """
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _CF_TINY:
        d = _CF_TINY
    d = 1.0 / d
    h = d
    for m in range(1, _CF_MAX_ITER + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < _CF_TINY:
            d = _CF_TINY
        c = 1.0 + aa / c
        if abs(c) < _CF_TINY:
            c = _CF_TINY
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < _CF_TINY:
            d = _CF_TINY
        c = 1.0 + aa / c
        if abs(c) < _CF_TINY:
            c = _CF_TINY
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) <= _CF_EPS:
            return h
    raise ArithmeticError(f"incomplete beta continued fraction did not converge (a={a}, b={b})")


def _tails(k: int, n: int, p: float) -> tuple[float, float]:
    """Return ``(P(X <= k), P(X > k))`` for ``0 <= k < n`` and ``0 < p < 1``.

    The tail whose continued fraction converges fast (always the smaller one) is computed directly
    and the other one as its complement.
    """
    q = 1.0 - p
    # I_p(k+1, n-k) converges fast iff p < (a+1)/(a+b+2) = (k+2)/(n+3); I_q(n-k, k+1) otherwise.
    if p >= (k + 2.0) / (n + 3.0):
        # lower tail I_q(n-k, k+1); prefactor q^(n-k) p^(k+1) / ((n-k) B(n-k, k+1)) = p * f(k)
        log_front = math.log(p) + binom_logpmf(k, n, p)
        lower = 0.0
        if log_front > _LOG_UNDERFLOW:
            lower = math.exp(log_front) * _betacf(float(n - k), float(k + 1), q)
        lower = min(max(lower, 0.0), 1.0)
        return lower, 1.0 - lower
    # upper tail I_p(k+1, n-k); prefactor p^(k+1) q^(n-k) / ((k+1) B(k+1, n-k)) = q * f(k+1)
    log_front = math.log(q) + binom_logpmf(k + 1, n, p)
    upper = 0.0
    if log_front > _LOG_UNDERFLOW:
        upper = math.exp(log_front) * _betacf(float(k + 1), float(n - k), p)
    upper = min(max(upper, 0.0), 1.0)
    return 1.0 - upper, upper


def _validate_tail_args(k: object, n: object, p: object) -> tuple[int, int, float]:
    ki = as_int(k, "k")
    ni = as_int(n, "n")
    if ni < 0:
        raise ValueError(f"n must be >= 0, got {ni}")
    return ki, ni, check_prob(p)


def binom_cdf(k: int, n: int, p: float) -> float:
    """``P(X <= k)`` for ``X ~ Binomial(n, p)``; any integer ``k`` (``k < 0``: 0; ``k >= n``: 1)."""
    ki, ni, pf = _validate_tail_args(k, n, p)
    if ki < 0:
        return 0.0
    if ki >= ni:
        return 1.0
    if pf == 0.0:
        return 1.0
    if pf == 1.0:
        return 0.0
    return _tails(ki, ni, pf)[0]


def binom_sf(k: int, n: int, p: float) -> float:
    """``P(X > k) = P(X >= k + 1)`` for ``X ~ Binomial(n, p)``; any integer ``k``."""
    ki, ni, pf = _validate_tail_args(k, n, p)
    if ki < 0:
        return 1.0
    if ki >= ni:
        return 0.0
    if pf == 0.0:
        return 0.0
    if pf == 1.0:
        return 1.0
    return _tails(ki, ni, pf)[1]


# ---------------------------------------------------------------------------------------------
# Exact test and Clopper-Pearson bounds
# ---------------------------------------------------------------------------------------------


def binomial_test_greater(k: int, n: int, p0: float) -> float:
    """Exact one-sided p-value for ``H0: p <= p0`` vs ``H1: p > p0``: ``P(X >= k | p0)``.

    The supremum of ``P(X >= k | p)`` over the composite null ``p <= p0`` is attained at ``p0``
    because the upper tail is increasing in ``p``.
    """
    ki, ni = check_count(k, n)
    p0f = check_prob(p0, "p0")
    return binom_sf(ki - 1, ni, p0f)


def _bisect(f: Callable[[float], float], increasing: bool, conservative_low: bool) -> float:
    """Find the root of a strictly monotone ``f`` on ``(0, 1)`` by bisection.

    Invariant: ``f(lo)`` and ``f(hi)`` are on opposite sides of 0. Iterates until
    ``hi - lo <= 1e-12 * hi`` (or the floats are adjacent) and returns ``lo`` if
    ``conservative_low`` else ``hi``.
    """
    lo, hi = 0.0, 1.0
    while True:
        mid = 0.5 * (lo + hi)
        if mid <= lo or mid >= hi or hi - lo <= _BISECT_REL_TOL * hi:
            break
        val = f(mid)
        below_root = val < 0.0 if increasing else val > 0.0
        if below_root:
            lo = mid
        else:
            hi = mid
    return lo if conservative_low else hi


def _cp_lower(k: int, n: int, a: float) -> float:
    """Exact one-sided lower bound: ``p_L`` with ``P(X >= k | p_L) = a``."""
    if k == 0:
        return 0.0
    if k == n:
        return float(a ** (1.0 / n))
    # P(X >= k | p) = sf(k-1) is increasing in p; points with sf < a lie below the root.
    return _bisect(lambda p: binom_sf(k - 1, n, p) - a, increasing=True, conservative_low=True)


def _cp_upper(k: int, n: int, a: float) -> float:
    """Exact one-sided upper bound: ``p_U`` with ``P(X <= k | p_U) = a``."""
    if k == n:
        return 1.0
    if k == 0:
        return float(-math.expm1(math.log(a) / n))  # 1 - a^(1/n) without cancellation
    # P(X <= k | p) is decreasing in p; points with cdf > a lie below the root.
    return _bisect(lambda p: binom_cdf(k, n, p) - a, increasing=False, conservative_low=False)


def clopper_pearson(k: int, n: int, alpha: float = 0.05, side: Side = "two-sided") -> Interval:
    """Exact Clopper-Pearson confidence interval for a binomial proportion.

    ``side='two-sided'``: ``[p_L(alpha/2), p_U(alpha/2)]``, coverage >= 1 - alpha.
    ``side='lower'``: ``[p_L(alpha), 1]`` — one-sided lower confidence bound at level 1 - alpha.
    ``side='upper'``: ``[0, p_U(alpha)]`` — one-sided upper confidence bound at level 1 - alpha.

    Coverage is guaranteed to be at least the nominal level for every ``p`` (Clopper & Pearson
    1934); it is conservative because of discreteness.
    """
    ki, ni = check_count(k, n)
    a = check_open_prob(alpha, "alpha")
    s = check_side(side)
    if s == "two-sided":
        return Interval(_cp_lower(ki, ni, a / 2.0), _cp_upper(ki, ni, a / 2.0))
    if s == "lower":
        return Interval(_cp_lower(ki, ni, a), 1.0)
    return Interval(0.0, _cp_upper(ki, ni, a))


def wilson(k: int, n: int, alpha: float = 0.05) -> Interval:
    """Wilson (1927) score interval — **for display only**, never for certification.

    With ``z = Phi^{-1}(1 - alpha/2)`` and ``p = k/n``::

        centre = (p + z^2/(2n)) / (1 + z^2/n)
        half   = z / (1 + z^2/n) * sqrt(p(1-p)/n + z^2/(4 n^2))

    Its coverage can fall below nominal for some ``p``; Clopper-Pearson is used for decisions.
    """
    ki, ni = check_count(k, n)
    a = check_open_prob(alpha, "alpha")
    z = -norm_ppf(a / 2.0)  # = z_{1-a/2}; no 1 - a rounding for tiny a
    phat = ki / ni
    z2n = z * z / ni
    denom = 1.0 + z2n
    centre = (phat + z2n / 2.0) / denom
    half = z / denom * math.sqrt(phat * (1.0 - phat) / ni + z2n / (4.0 * ni))
    # At k = 0 (k = n) the lower (upper) end is exactly 0 (1) algebraically:
    # centre = half = (z^2/2n)/(1 + z^2/n); set it exactly instead of trusting rounding.
    lower = 0.0 if ki == 0 else max(0.0, centre - half)
    upper = 1.0 if ki == ni else min(1.0, centre + half)
    return Interval(lower, upper)
