"""Independent exact references for the statistics tests (``fractions`` + integer arithmetic only).

Nothing here imports the code under test.
"""

from __future__ import annotations

from fractions import Fraction
from math import comb


def exact_tail_ge(k: int, n: int, p: float | Fraction) -> Fraction:
    """Exact ``P(X >= k)`` for ``X ~ Binomial(n, p)`` with ``p`` taken as an exact rational."""
    pf = Fraction(p)
    a, d = pf.numerator, pf.denominator
    b = d - a
    if k <= 0:
        return Fraction(1)
    if k > n:
        return Fraction(0)
    num = sum(comb(n, i) * a**i * b ** (n - i) for i in range(k, n + 1))
    return Fraction(num, d**n)


def exact_cdf(k: int, n: int, p: float | Fraction) -> Fraction:
    """Exact ``P(X <= k)``."""
    return 1 - exact_tail_ge(k + 1, n, p)


def _upper_tail_numerators(n: int, p: Fraction) -> tuple[list[int], int]:
    """Integers ``S[k] = d^n P(X >= k)`` for ``k = 0..n+1`` and the denominator ``d^n``."""
    a, d = p.numerator, p.denominator
    b = d - a
    terms = [comb(n, i) * a**i * b ** (n - i) for i in range(n + 1)]
    suffix = [0] * (n + 2)
    for i in range(n, -1, -1):
        suffix[i] = suffix[i + 1] + terms[i]
    return suffix, d**n


def exact_power(n: int, p0: Fraction, p1: Fraction, alpha: Fraction) -> Fraction:
    """Exact power at ``p1`` of the non-randomized level-``alpha`` one-sided test of ``p <= p0``.

    Critical value: smallest ``k`` with ``P(X >= k | p0) <= alpha`` (all in exact integers).
    """
    s0, d0 = _upper_tail_numerators(n, p0)
    k = next(i for i in range(n + 2) if s0[i] * alpha.denominator <= alpha.numerator * d0)
    s1, d1 = _upper_tail_numerators(n, p1)
    return Fraction(s1[k], d1)
