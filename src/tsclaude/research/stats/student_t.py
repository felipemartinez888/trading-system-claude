"""Student t distribution (real degrees of freedom) without SciPy.

Regularized incomplete beta (DLMF 8.17.22; Numerical Recipes 3rd ed. §6.4)::

    I_x(a, b) = x^a (1-x)^b / (a B(a, b)) * CF(a, b, x)            for x < (a+1)/(a+b+2)
    I_x(a, b) = 1 - I_{1-x}(b, a)                                    otherwise

with the continued fraction ``CF`` evaluated by the modified Lentz method (``binomial._betacf``)
and ``log B(a, b) = lgamma(b) - [lgamma(a + b) - lgamma(a)]`` (``a`` the larger argument). For
``a >= 20`` the bracket is evaluated without cancellation from Stirling's series,
``b log a + (a + b - 1/2) log1p(b/a) - b + c(a + b) - c(a)`` with
``c(x) = 1/(12x) - 1/(360x^3) + 1/(1260x^5)`` (truncation error < 1/(1680 x^7)); otherwise
directly with ``lgamma``. Both ``x`` and ``1 - x`` are passed
in explicitly so that neither is formed by a cancelling subtraction.

Student t with ``nu`` degrees of freedom (Abramowitz & Stegun 26.7.1, 26.5.27): for ``t >= 0``::

    P(T > t) = 1/2 * I_{nu/(nu + t^2)}(nu/2, 1/2)

and ``P(T > -t) = 1 - P(T > t)``. Quantiles are found by bisection on the survival function
(strictly decreasing) after bracketing, to a relative width of 1e-14; upper-tail quantiles are
computed from ``q = 1 - p`` directly (``t_isf``) so tiny tail probabilities keep full precision.
``nu = inf`` is the standard normal.
"""

from __future__ import annotations

import math

from tsclaude.research.stats._validate import as_float
from tsclaude.research.stats.binomial import _betacf
from tsclaude.research.stats.normal import norm_cdf, norm_ppf, norm_sf

_BISECT_REL_TOL = 1e-14


def _check_df(df: object) -> float:
    nu = float(df)  # type: ignore[arg-type]
    if math.isnan(nu) or nu <= 0.0:
        raise ValueError(f"degrees of freedom must be > 0, got {df}")
    return nu


def _stirling_corr(x: float) -> float:
    x2 = x * x
    return (1.0 / 12.0 - (1.0 / 360.0 - 1.0 / (1260.0 * x2)) / x2) / x


def _lgamma_diff(a: float, b: float) -> float:
    """``lgamma(a + b) - lgamma(a)``, accurate for large ``a`` (see module docstring)."""
    if a < 20.0:
        return math.lgamma(a + b) - math.lgamma(a)
    return (
        b * math.log(a)
        + (a + b - 0.5) * math.log1p(b / a)
        - b
        + _stirling_corr(a + b)
        - _stirling_corr(a)
    )


def _log_beta(a: float, b: float) -> float:
    big, small = (a, b) if a >= b else (b, a)
    return math.lgamma(small) - _lgamma_diff(big, small)


def regularized_beta(a: float, b: float, x: float, y: float | None = None) -> float:
    """``I_x(a, b)`` for ``a, b > 0`` and ``0 <= x <= 1``; ``y = 1 - x`` may be passed exactly."""
    af, bf = as_float(a, "a"), as_float(b, "b")
    if af <= 0.0 or bf <= 0.0:
        raise ValueError(f"a and b must be > 0, got a={af}, b={bf}")
    xf = as_float(x, "x")
    yf = 1.0 - xf if y is None else as_float(y, "y")
    if not (0.0 <= xf <= 1.0 and 0.0 <= yf <= 1.0):
        raise ValueError(f"x and 1 - x must be in [0, 1], got x={xf}, y={yf}")
    if xf == 0.0:
        return 0.0
    if yf == 0.0:
        return 1.0
    log_beta = _log_beta(af, bf)
    # log x and log(1 - x) from whichever of x, y is small (no rounding of a value near 1)
    log_x = math.log(xf) if xf <= 0.5 else math.log1p(-yf)
    log_y = math.log(yf) if yf <= 0.5 else math.log1p(-xf)
    if xf < (af + 1.0) / (af + bf + 2.0):
        front = math.exp(af * log_x + bf * log_y - log_beta) / af
        return min(1.0, max(0.0, front * _betacf(af, bf, xf)))
    front = math.exp(bf * log_y + af * log_x - log_beta) / bf
    return min(1.0, max(0.0, 1.0 - front * _betacf(bf, af, yf)))


def t_sf(t: float, df: float) -> float:
    """``P(T > t)`` for Student t with ``df`` degrees of freedom (``df = inf``: normal)."""
    tf = float(t)
    if math.isnan(tf):
        raise ValueError("t must not be NaN")
    nu = _check_df(df)
    if math.isinf(nu):
        return norm_sf(tf)
    if math.isinf(tf):
        return 0.0 if tf > 0 else 1.0
    t2 = tf * tf
    half_tail = 0.5 * regularized_beta(nu / 2.0, 0.5, nu / (nu + t2), t2 / (nu + t2))
    return half_tail if tf >= 0.0 else 1.0 - half_tail


def t_cdf(t: float, df: float) -> float:
    """``P(T <= t)``; computed from the smaller tail for accuracy."""
    tf = float(t)
    nu = _check_df(df)
    if math.isinf(nu):
        return norm_cdf(tf)
    return t_sf(-tf, nu)


def t_isf(q: float, df: float) -> float:
    """Upper-tail quantile: the ``t`` with ``P(T > t) = q`` (``0 < q < 1``)."""
    qf = as_float(q, "q")
    if not 0.0 < qf < 1.0:
        raise ValueError(f"q must be in (0, 1), got {qf}")
    nu = _check_df(df)
    if math.isinf(nu):
        return -norm_ppf(qf)
    if qf > 0.5:
        return -t_isf(1.0 - qf, nu)
    if qf == 0.5:
        return 0.0
    lo, hi = 0.0, max(1.0, -norm_ppf(qf))
    while t_sf(hi, nu) > qf:  # t tails are heavier than normal: expand the bracket
        lo, hi = hi, 2.0 * hi
        if hi > 1e300:
            raise ArithmeticError("t quantile bracket overflow")
    while hi - lo > _BISECT_REL_TOL * hi:
        mid = 0.5 * (lo + hi)
        if mid <= lo or mid >= hi:
            break
        if t_sf(mid, nu) > qf:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def t_ppf(p: float, df: float) -> float:
    """Quantile ``t_p`` with ``P(T <= t_p) = p``."""
    pf = as_float(p, "p")
    if not 0.0 < pf < 1.0:
        raise ValueError(f"p must be in (0, 1), got {pf}")
    return -t_isf(pf, df)
