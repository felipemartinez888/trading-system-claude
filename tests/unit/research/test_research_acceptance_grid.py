"""ADR-0012 Amendment 4 item 4: false-certification acceptance grid.

For each reference losing strategy, every n in N_GRID and every caller block length in
{None, 1, 5, 25, 50, sqrt(n), n}: estimate + 2 SE <= alpha (= 0.05, one trial). Estimators and
the exact stratum bounds are documented in ``stats_grid``. Block None and block 1 are the same
computation (asserted below) and block n always exceeds the cap min(3 sqrt(n), n/3), so every
sample fails closed with ``dependence_unresolved`` (rate exactly 0, asserted below); the
simulated settings are therefore {None, 5, 25, 50, sqrt(n)}.
"""

from __future__ import annotations

import dataclasses
import math

import numpy as np
import pytest
from stats_grid import (
    ALPHA,
    FAT,
    GAP,
    N_GRID,
    markov_chain,
    markov_grid,
    tail_strategy_grid,
)

from tsclaude.research.stats.certify import certify


def _sample(name: str, n: int, rng: np.random.Generator) -> tuple[np.ndarray, dict[str, object]]:
    if name == "markov":
        return markov_chain(rng, n), {"p_star": 0.525, "stop_loss_r": 1.05, "max_loss_r": 1.05}
    s = GAP if name == "gap" else FAT
    u = rng.random(n)
    body = np.where(u < (1 - s.p_tail) * s.p_hi_body, s.hi, s.lo)
    r = np.where(u >= 1 - s.p_tail, s.tail, body)
    return r, {"p_star": s.p_star, "stop_loss_r": s.stop_loss_r, "max_loss_r": s.max_loss_r}


@pytest.mark.parametrize("name", ["gap", "fat", "markov"])
def test_block_none_equals_block_one_and_block_n_always_fails_closed(name: str) -> None:
    rng = np.random.default_rng(7)
    base = {"alpha": ALPHA, "n_trials": 1, "min_power": 0.8, "seed": 3, "max_loss_source": "test"}
    for n in (300, 1000):
        r, kw = _sample(name, n, rng)
        c_none = certify(r, mean_block=None, **base, **kw)  # type: ignore[arg-type]
        c_one = certify(r, mean_block=1.0, **base, **kw)  # type: ignore[arg-type]
        assert dataclasses.replace(c_one, mean_block=None) == c_none
        c_n = certify(r, mean_block=float(n), **base, **kw)  # type: ignore[arg-type]
        assert c_n.block_required >= n > c_n.block_cap
        assert c_n.dependence_unresolved
        assert not c_n.certified


def _assert_grid(name: str, grid_fn: object) -> None:
    failures = []
    for n in N_GRID:
        grid = grid_fn(n)  # type: ignore[operator]
        for block, (est, se) in grid.items():
            if est + 2 * se > ALPHA:
                failures.append((n, block, round(est, 4), round(se, 4)))
        assert not failures, f"{name}: cells above alpha: {failures}"


@pytest.mark.slow
def test_acceptance_grid_fat_tail() -> None:
    """Fat-tail losing strategy (E = -0.049R): every cell estimate + 2 SE <= 0.05."""
    _assert_grid("fat", lambda n: tail_strategy_grid(FAT, n, runs=16, seed=20261020 + n))


@pytest.mark.slow
def test_acceptance_grid_persistent_markov() -> None:
    """Persistent-Markov losing strategy: 50 seeded runs per n (zero-hit bound 0.040)."""
    _assert_grid("markov", lambda n: markov_grid(n, runs=50, seed=20261020 + n))


@pytest.mark.slow
def test_acceptance_grid_gap_risk() -> None:
    """Gap-risk losing strategy (E = -0.02R), declared max_loss_r = 44.5: every cell
    estimate + 2 SE <= 0.05 (Amendment 5 closes the case escalated under Amendment 4)."""
    _assert_grid("gap", lambda n: tail_strategy_grid(GAP, n, runs=16, seed=20261020 + n))


def test_markov_reference_is_losing() -> None:
    """Sanity: stationary win rate 0.51 < p* = 0.525 => E = 0.51*0.95 - 0.49*1.05 < 0."""
    assert 0.51 * 0.95 - 0.49 * 1.05 < 0
    assert math.isclose(0.566 * 1.5 - 0.424 - 0.01 * 44.5, -0.02, abs_tol=1e-12)
    assert math.isclose(0.85 * 0.5 - 0.12 * 0.2 - 0.03 * 15, -0.049, abs_tol=1e-12)
