"""Student t distribution: references are closed forms (nu = 1, 2, 3, 4), the normal limit
(stdlib ``NormalDist``) and direct numerical integration of the density for non-integer nu."""

from __future__ import annotations

import math
from statistics import NormalDist

import pytest

from tsclaude.research.stats.student_t import regularized_beta, t_cdf, t_isf, t_ppf, t_sf

PROBS = [1e-10, 1e-6, 5e-5, 0.0025, 0.025, 0.05, 0.1, 0.3, 0.5, 0.7, 0.9, 0.975, 0.999]


def test_cauchy_nu_1_closed_form() -> None:
    """nu = 1: F(t) = 1/2 + atan(t)/pi, t_p = tan(pi (p - 1/2)) = -cot(pi p) (stable form)."""
    for t in (-50.0, -3.0, -0.5, 0.0, 0.7, 2.0, 40.0):
        assert math.isclose(t_cdf(t, 1), 0.5 + math.atan(t) / math.pi, rel_tol=1e-13)
    for p in PROBS:
        ref = -1.0 / math.tan(math.pi * p) if p != 0.5 else 0.0
        assert math.isclose(t_ppf(p, 1), ref, rel_tol=1e-11, abs_tol=1e-13)


def test_nu_2_closed_form() -> None:
    """nu = 2: F(t) = 1/2 + t / (2 sqrt(2 + t^2)), t_p = (2p - 1) / sqrt(2 p (1 - p))."""
    for t in (-30.0, -2.0, 0.0, 1.0, 4.0):
        assert math.isclose(t_cdf(t, 2), 0.5 + t / (2 * math.sqrt(2 + t * t)), rel_tol=1e-13)
    for p in PROBS:
        ref = (2 * p - 1) / math.sqrt(2 * p * (1 - p))
        assert math.isclose(t_ppf(p, 2), ref, rel_tol=1e-11, abs_tol=1e-13)


def test_nu_3_closed_form() -> None:
    """nu = 3: F(t) = 1/2 + (theta + sin(theta) cos(theta)) / pi, theta = atan(t / sqrt(3))."""
    for t in (-20.0, -2.5, -0.1, 0.0, 1.0, 3.18244630528371, 10.0):
        th = math.atan(t / math.sqrt(3))
        ref = 0.5 + (th + math.sin(th) * math.cos(th)) / math.pi
        assert math.isclose(t_cdf(t, 3), ref, rel_tol=1e-12)


def test_nu_4_quantile_closed_form() -> None:
    """nu = 4 (Shaw 2006): a = 4p(1-p), q = cos(acos(sqrt(a))/3)/sqrt(a),
    t_p = sign(p - 1/2) * 2 sqrt(q - 1)."""
    for p in PROBS:
        if p == 0.5:
            assert t_ppf(p, 4) == 0.0
            continue
        a = 4 * p * (1 - p)
        q = math.cos(math.acos(math.sqrt(a)) / 3) / math.sqrt(a)
        ref = math.copysign(2 * math.sqrt(q - 1), p - 0.5)
        assert math.isclose(t_ppf(p, 4), ref, rel_tol=1e-9)


def _two_sided_mass(t: float, nu: int) -> float:
    """A(t | nu) = P(|T| <= t) by the exact finite series of Abramowitz & Stegun 26.7.3-4.

    theta = atan(t / sqrt(nu)). Odd nu: A = (2/pi) (theta + sin(theta) [cos(theta) +
    (2/3) cos^3 + (2 4)/(3 5) cos^5 + ...]) up to cos^(nu-2) (A = 2 theta/pi for nu = 1).
    Even nu: A = sin(theta) [1 + (1/2) cos^2 + (1 3)/(2 4) cos^4 + ...] up to cos^(nu-2).
    """
    th = math.atan(t / math.sqrt(nu))
    c, sn = math.cos(th), math.sin(th)
    if nu % 2:
        if nu == 1:
            return 2 * th / math.pi
        term, acc = c, c
        for k in range(3, nu - 1, 2):
            term *= c * c * (k - 1) / k
            acc += term
        return 2 / math.pi * (th + sn * acc)
    term, acc = 1.0, 1.0
    for k in range(2, nu - 1, 2):
        term *= c * c * (k - 1) / k
        acc += term
    return sn * acc


def test_integer_nu_against_exact_finite_series() -> None:
    for nu in (1, 2, 3, 5, 10, 30, 120):
        for t in (0.0, 0.4, 1.0, 2.0, 3.5, 8.0):
            ref = (1.0 - _two_sided_mass(t, nu)) / 2.0
            assert math.isclose(t_sf(t, nu), ref, rel_tol=1e-12, abs_tol=1e-15), (nu, t)
        # two-sided 95 % critical value: A(t) = 0.95
        crit = t_isf(0.025, nu)
        assert math.isclose(_two_sided_mass(crit, nu), 0.95, rel_tol=1e-13)


def test_large_nu_approaches_normal() -> None:
    z = NormalDist()
    for p in (1e-6, 0.005, 0.05, 0.5, 0.9):
        assert math.isclose(t_ppf(p, 1e8), z.inv_cdf(p), rel_tol=1e-6, abs_tol=1e-9)
        assert t_ppf(p, math.inf) == pytest.approx(z.inv_cdf(p), rel=1e-9, abs=1e-12)
    # heavier tails for finite nu
    assert t_isf(0.005, 299) > -z.inv_cdf(0.005)


def test_non_integer_nu_against_numerical_integration() -> None:
    """P(T > t) = c sqrt(nu) * integral_{atan(t/sqrt(nu))}^{pi/2} cos^(nu-1)(theta) d theta,
    c = Gamma((nu+1)/2) / (Gamma(nu/2) sqrt(nu pi)) (substitution x = sqrt(nu) tan(theta));
    composite Simpson on the finite interval (smooth integrand for nu >= 1)."""
    for nu in (2.5, 3.5, 7.3, 54.6):  # nu - 1 >= 1.5 keeps Simpson accurate
        c = math.exp(math.lgamma((nu + 1) / 2) - math.lgamma(nu / 2)) / math.sqrt(nu * math.pi)
        for t in (0.0, 0.8, 2.1, 4.0):
            lo, hi, steps = math.atan(t / math.sqrt(nu)), math.pi / 2, 20_000
            h = (hi - lo) / steps
            acc = math.cos(lo) ** (nu - 1) + math.cos(hi) ** (nu - 1)
            for i in range(1, steps):
                acc += (4 if i % 2 else 2) * math.cos(lo + i * h) ** (nu - 1)
            ref = c * math.sqrt(nu) * acc * h / 3
            assert math.isclose(t_sf(t, nu), ref, rel_tol=1e-9), (nu, t)


def test_symmetry_monotonicity_and_round_trip() -> None:
    for nu in (1.0, 3.7, 29.0, 1999.0):
        prev = math.inf
        for q in (1e-12, 1e-8, 1e-4, 0.01, 0.2, 0.5):
            x = t_isf(q, nu)
            assert x < prev
            prev = x
            assert math.isclose(t_sf(x, nu), q, rel_tol=1e-10)
            assert math.isclose(t_ppf(q, nu), -x, rel_tol=1e-12, abs_tol=1e-15)
        assert math.isclose(t_cdf(1.3, nu) + t_cdf(-1.3, nu), 1.0, rel_tol=1e-14)


def test_regularized_beta_known_values_and_validation() -> None:
    # I_x(1, 1) = x;  I_x(a, 1) = x^a;  I_x(1, b) = 1 - (1 - x)^b
    for x in (0.0, 0.1, 0.5, 0.93, 1.0):
        assert math.isclose(regularized_beta(1, 1, x), x, abs_tol=1e-15)
        assert math.isclose(regularized_beta(2.5, 1, x), x**2.5, abs_tol=1e-15)
        assert math.isclose(regularized_beta(1, 3.5, x), 1 - (1 - x) ** 3.5, abs_tol=1e-15)
    with pytest.raises(ValueError, match=r"must be > 0"):
        regularized_beta(0.0, 1.0, 0.5)
    with pytest.raises(ValueError, match=r"degrees of freedom"):
        t_sf(1.0, 0.0)
    with pytest.raises(ValueError, match=r"q must be in"):
        t_isf(0.0, 3.0)
