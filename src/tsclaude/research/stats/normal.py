"""Standard normal distribution: CDF and quantile (pure Python, no SciPy).

``norm_cdf`` uses the identity ``Phi(x) = erfc(-x / sqrt(2)) / 2`` (Abramowitz & Stegun 7.1.2,
26.2.29), which keeps full relative precision in the lower tail because ``math.erfc`` does.

``norm_ppf`` uses P. J. Acklam's rational approximation (2003, "An algorithm for computing the
inverse normal cumulative distribution function", relative error < 1.15e-9) followed by one Halley
step on ``Phi(x) - p = 0``, which roughly triples the number of correct digits (to machine
precision in the double range of interest). Upper-tail inputs are handled by the reflection
``ppf(p) = -ppf(1 - p)``; for ``p >= 0.5`` the subtraction ``1 - p`` is exact in IEEE-754
(Sterbenz lemma), so no precision is lost there.
"""

from __future__ import annotations

import math

from tsclaude.research.stats._validate import as_float

_SQRT2 = math.sqrt(2.0)
_SQRT_2PI = math.sqrt(2.0 * math.pi)

# Acklam (2003) coefficients.
_A = (
    -3.969683028665376e01,
    2.209460984245205e02,
    -2.759285104469687e02,
    1.383577518672690e02,
    -3.066479806614716e01,
    2.506628277459239e00,
)
_B = (
    -5.447609879822406e01,
    1.615858368580409e02,
    -1.556989798598866e02,
    6.680131188771972e01,
    -1.328068155288572e01,
)
_C = (
    -7.784894002430293e-03,
    -3.223964580411365e-01,
    -2.400758277161838e00,
    -2.549732539343734e00,
    4.374664141464968e00,
    2.938163982698783e00,
)
_D = (
    7.784695709041462e-03,
    3.224671290700398e-01,
    2.445134137142996e00,
    3.754408661907416e00,
)
_P_LOW = 0.02425


def norm_cdf(x: float) -> float:
    """Standard normal CDF ``Phi(x) = erfc(-x / sqrt(2)) / 2``."""
    xf = float(x)
    if math.isnan(xf):
        raise ValueError("x must not be NaN")
    return 0.5 * math.erfc(-xf / _SQRT2)


def norm_sf(x: float) -> float:
    """Standard normal survival function ``1 - Phi(x) = erfc(x / sqrt(2)) / 2`` (tail-accurate)."""
    xf = float(x)
    if math.isnan(xf):
        raise ValueError("x must not be NaN")
    return 0.5 * math.erfc(xf / _SQRT2)


def norm_pdf(x: float) -> float:
    """Standard normal density ``exp(-x^2/2) / sqrt(2*pi)``."""
    xf = float(x)
    return math.exp(-0.5 * xf * xf) / _SQRT_2PI


def _acklam_lower_half(p: float) -> float:
    """Acklam's approximation for ``0 < p <= 0.5``."""
    if p < _P_LOW:
        q = math.sqrt(-2.0 * math.log(p))
        num = ((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]
        den = (((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0
        return num / den
    q = p - 0.5
    r = q * q
    num = (((((_A[0] * r + _A[1]) * r + _A[2]) * r + _A[3]) * r + _A[4]) * r + _A[5]) * q
    den = ((((_B[0] * r + _B[1]) * r + _B[2]) * r + _B[3]) * r + _B[4]) * r + 1.0
    return num / den


def norm_ppf(p: float) -> float:
    """Standard normal quantile ``Phi^{-1}(p)``.

    Acklam (2003) initial value refined by one Halley step:
    ``e = Phi(x) - p``, ``u = e * sqrt(2*pi) * exp(x^2/2)``, ``x <- x - u / (1 + x*u/2)``.
    Returns ``-inf`` for ``p == 0`` and ``+inf`` for ``p == 1``; raises outside ``[0, 1]``.
    """
    pf = as_float(p, "p")
    if not 0.0 <= pf <= 1.0:
        raise ValueError(f"p must be in [0, 1], got {pf}")
    if pf == 0.0:
        return -math.inf
    if pf == 1.0:
        return math.inf
    if pf > 0.5:
        return -norm_ppf(1.0 - pf)  # exact subtraction for p >= 0.5
    x = _acklam_lower_half(pf)
    e = norm_cdf(x) - pf
    u = e * _SQRT_2PI * math.exp(0.5 * x * x)
    return x - u / (1.0 + 0.5 * x * u)
