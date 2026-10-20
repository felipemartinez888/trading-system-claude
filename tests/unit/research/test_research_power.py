"""Sample-size calculations: normal approximation and exact search.

The exact reference (``stats_exact_ref.exact_power``) uses rational arithmetic with exact
``p*``/``p_alt``/``alpha`` and a direct scan; it shares no code with the implementation.
"""

from __future__ import annotations

import itertools
import math
from fractions import Fraction
from statistics import NormalDist

import pytest
from stats_exact_ref import exact_power

from tsclaude.research.stats.power import (
    exact_test_power,
    n_min_exact,
    n_min_one_sided,
    normal_approx_power,
)

Z = NormalDist().inv_cdf


def test_adr_0012_worked_example() -> None:
    """ADR-0012: p* = 0.52, p = 0.60, alpha = 0.05 one-sided, 80 % power -> n_min ~ 240.

    (1.644854 * 0.499600 + 0.841621 * 0.489898)^2 / 0.0064 = 237.96 -> 238.
    """
    raw = (Z(0.95) * math.sqrt(0.52 * 0.48) + Z(0.80) * math.sqrt(0.6 * 0.4)) ** 2 / 0.08**2
    assert math.isclose(raw, 237.96, abs_tol=0.01)
    assert n_min_one_sided(0.52, 0.60, 0.05, 0.80) == math.ceil(raw) == 238


@pytest.mark.parametrize(
    ("p_star", "p_alt", "alpha", "power"),
    [
        (Fraction(52, 100), Fraction(60, 100), Fraction(5, 100), 0.80),
        (Fraction(1, 2), Fraction(7, 10), Fraction(1, 100), 0.90),
        (Fraction(3, 10), Fraction(4, 10), Fraction(5, 100), 0.80),
        (Fraction(9, 10), Fraction(99, 100), Fraction(5, 100), 0.80),
        (Fraction(21, 40), Fraction(3, 5), Fraction(1, 400), 0.80),  # bracket b=1,c=.05; m=20
    ],
)
def test_n_min_exact_is_the_first_n_reaching_power(
    p_star: Fraction, p_alt: Fraction, alpha: Fraction, power: float
) -> None:
    """At the returned n the exact power >= target; at every n in a window below it is not.

    The window [n - 40, n - 1] is checked exhaustively with exact rational arithmetic; the
    implementation's proof (randomized-UMP bound, module docstring) covers n < window.
    """
    n = n_min_exact(float(p_star), float(p_alt), float(alpha), power)
    assert exact_power(n, p_star, p_alt, alpha) >= power
    for m in range(max(1, n - 40), n):
        assert exact_power(m, p_star, p_alt, alpha) < power, m


def test_exact_power_saw_tooth_means_later_n_can_dip_below_target() -> None:
    """Document the discreteness: power is not monotone in n, so a scan is required."""
    ps, pa, a = Fraction(1, 2), Fraction(7, 10), Fraction(5, 100)
    powers = [exact_power(n, ps, pa, a) for n in range(20, 80)]
    dips = sum(1 for x, y in itertools.pairwise(powers) if y < x)
    assert dips > 0


def test_exact_test_power_matches_exact_reference() -> None:
    for n in (10, 57, 245):
        res = exact_test_power(n, 0.52, 0.60, 0.05)
        assert res.size <= 0.05
        ref = exact_power(n, Fraction(52, 100), Fraction(60, 100), Fraction(5, 100))
        assert math.isclose(res.power, float(ref), rel_tol=1e-12)


def test_normal_approx_power_inverts_n_min() -> None:
    n = n_min_one_sided(0.52, 0.60, 0.05, 0.80)
    assert normal_approx_power(n, 0.52, 0.60, 0.05) >= 0.80
    assert normal_approx_power(n - 1, 0.52, 0.60, 0.05) < 0.80


def test_n_min_grows_with_stricter_alpha_and_smaller_margin() -> None:
    assert n_min_exact(0.52, 0.60, 0.05 / 50) > n_min_exact(0.52, 0.60, 0.05)
    assert n_min_exact(0.52, 0.56) > n_min_exact(0.52, 0.60)
    assert n_min_one_sided(0.52, 0.56) > n_min_one_sided(0.52, 0.60)


def test_n_min_exact_large_n_and_unreachable() -> None:
    n = n_min_exact(0.50, 0.51, 0.05, 0.80)
    assert 15_000 < n < 16_000
    with pytest.raises(ValueError, match=r"not reachable"):
        n_min_exact(0.50, 0.5001, 0.05, 0.80, max_n=1000)
    with pytest.raises(ValueError, match=r"p_alt must satisfy"):
        n_min_exact(0.6, 0.5)
