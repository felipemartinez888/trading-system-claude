"""Acceptance-grid estimators for ADR-0012 Amendment 4 item 4 (shared by tests and reporting).

Reference losing strategies (true expectancy < 0):

* ``gap``: +1.5R w.p. 0.566, -1R w.p. 0.424, -44.5R w.p. 0.01 (E = -0.02R); design p* = 0.4,
  stop_loss_r = 1.0.
* ``fat``: +0.5R w.p. 0.85, -0.2R w.p. 0.12, -15R w.p. 0.03 (E = -0.049R); realized p*.
* ``markov``: win/loss chain, P(win -> win) = 0.98, stationary win rate 0.51, +0.95R / -1.05R
  (E < 0 since 0.51 < p* = 0.525); design p* = 0.525, stop_loss_r = 1.05.

Estimators. For ``gap`` and ``fat`` the false-certification rate is stratified on the number K of
tail trades, K ~ Binomial(n, p_tail), with exact weights. Given K, the other n - K outcomes are
i.i.d. two-valued: with W the count of the higher body value the sample *multiset* is fixed by
(K, W), hence so are its mean and standard deviation s. Certification requires the t-type
expectancy bound ``mean - t_{1-a, n_eff-1} s / sqrt(n_eff) > 0`` (the certified bound is the
minimum of it and the bootstrap one); since n_eff <= n and the t quantile decreases with the
degrees of freedom, it implies ``mean - t_{1-a, n-1} s / sqrt(n) > 0``, a deterministic function
of (K, W) for every caller block. Its probability ``u_K`` (exact binomial sum over W) bounds
P(certified | K). Strata with ``w_K u_K >= SIM_MASS`` (the main contributors) are simulated with
``certify`` itself (seeded runs, all caller blocks on the same samples); every other stratum
contributes its exact bound ``w_K u_K``. For ``markov`` the rate is a plain seeded Monte Carlo.

Each cell reports ``(estimate, se)`` and passes iff ``estimate + 2 se <= alpha``; when no run
certifies, ``se`` uses a one-hit floor ``sqrt((1/R)(1 - 1/R)/R)`` (per simulated stratum, or per
cell for plain MC) so zero hits never yield a zero bound. Every reference strategy is run with
``max_loss_r`` equal to its true worst loss (Amendment 5.3).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from tsclaude.research.stats.certify import certify
from tsclaude.research.stats.student_t import t_isf

ALPHA = 0.05
N_GRID = (300, 310, 350, 400, 500, 700, 1000)
SIM_MASS = 0.005


@dataclass(frozen=True)
class TailStrategy:
    name: str
    hi: float  # body value 1
    lo: float  # body value 2
    p_hi_body: float  # P(hi | body)
    tail: float  # tail value (negative)
    p_tail: float
    p_star: float | None
    stop_loss_r: float | None
    max_loss_r: float  # true worst loss (Amendment 5.3)


GAP = TailStrategy("gap", 1.5, -1.0, 0.566 / 0.99, -44.5, 0.01, 0.4, 1.0, 44.5)
FAT = TailStrategy("fat", 0.5, -0.2, 0.85 / 0.97, -15.0, 0.03, None, None, 15.0)


def caller_blocks(n: int) -> list[float | None]:
    """Distinct simulated caller block settings (None == 1 exactly; n always exceeds the cap)."""
    return [None, 5.0, 25.0, 50.0, math.sqrt(n)]


def _certified(
    r: np.ndarray,
    p_star: float | None,
    stop: float | None,
    block: float | None,
    seed: int,
    max_loss: float,
) -> bool:
    return certify(
        r,
        p_star=p_star,
        stop_loss_r=stop,
        max_loss_r=max_loss,
        max_loss_source="reference strategy: true worst loss",
        alpha=ALPHA,
        n_trials=1,
        min_power=0.8,
        mean_block=block,
        seed=seed,
    ).certified


def _log_binom_pmf(w: np.ndarray, m: int, p: float) -> np.ndarray:
    """Binomial log-pmf via ``math.lgamma`` (independent of the code under test)."""
    lg = np.array([math.lgamma(int(x) + 1) + math.lgamma(m - int(x) + 1) for x in w])
    out: np.ndarray = math.lgamma(m + 1) - lg + w * math.log(p) + (m - w) * math.log1p(-p)
    return out


def exact_stratum_bound(s: TailStrategy, n: int, k: int) -> float:
    """``u_K``: exact upper bound on P(certified | K = k) (module docstring)."""
    t = t_isf(ALPHA, n - 1)
    m = n - k
    w = np.arange(m + 1, dtype=np.float64)
    total = s.hi * w + s.lo * (m - w) + s.tail * k
    mean = total / n
    sumsq = s.hi**2 * w + s.lo**2 * (m - w) + s.tail**2 * k
    sd = np.sqrt(np.maximum((sumsq - n * mean**2) / (n - 1), 0.0))
    ok = mean - t * sd / math.sqrt(n) > 0.0
    if not bool(ok.any()):
        return 0.0
    return float(np.exp(_log_binom_pmf(w[ok], m, s.p_hi_body)).sum())


def _t_condition(r: np.ndarray, n: int) -> bool:
    return bool(np.mean(r) - t_isf(ALPHA, n - 1) * np.std(r, ddof=1) / math.sqrt(n) > 0.0)


def tail_strategy_grid(
    s: TailStrategy, n: int, runs: int, seed: int
) -> dict[float | None, tuple[float, float]]:
    """Stratified estimate (and SE) of the false-certification rate for each caller block."""
    weights = [math.comb(n, k) * s.p_tail**k * (1 - s.p_tail) ** (n - k) for k in range(n + 1)]
    ks = [k for k in range(n + 1) if weights[k] > 1e-15]
    bounds = {k: exact_stratum_bound(s, n, k) for k in ks}
    simulate = [k for k in ks if weights[k] * bounds[k] >= SIM_MASS]
    bounded = sum(weights[k] * bounds[k] for k in ks if k not in simulate)
    blocks = caller_blocks(n)
    est = dict.fromkeys(blocks, bounded)
    var = dict.fromkeys(blocks, 0.0)
    rng = np.random.default_rng(seed)
    for k in simulate:
        hits = dict.fromkeys(blocks, 0)
        for i in range(runs):
            body = np.where(rng.random(n - k) < s.p_hi_body, s.hi, s.lo)
            r = np.concatenate([body, np.full(k, s.tail)])
            rng.shuffle(r)
            t_cond = _t_condition(r, n)
            for b in blocks:
                ok = _certified(r, s.p_star, s.stop_loss_r, b, 1000 * k + i, s.max_loss_r)
                # the exact stratum bound relies on: certified => deterministic t condition
                if ok and not t_cond:
                    raise AssertionError(f"certified sample violates the t condition (k={k})")
                hits[b] += ok
        for b in blocks:
            q = hits[b] / runs
            est[b] += weights[k] * q
            # one-hit floor: a stratum with no certified run still contributes variance
            var[b] += weights[k] ** 2 * max(q * (1 - q), (1 / runs) * (1 - 1 / runs)) / runs
    return {b: (est[b], math.sqrt(var[b])) for b in blocks}


def markov_chain(
    rng: np.random.Generator, n: int, pi: float = 0.51, stay: float = 0.98
) -> np.ndarray:
    a = 1.0 - stay
    b = a * pi / (1.0 - pi)
    u = rng.random(n)
    s = np.empty(n, dtype=bool)
    s[0] = u[0] < pi
    for t in range(1, n):
        s[t] = (u[t] >= a) if s[t - 1] else (u[t] < b)
    return np.where(s, 0.95, -1.05)


def markov_grid(n: int, runs: int, seed: int) -> dict[float | None, tuple[float, float]]:
    """Plain seeded Monte Carlo for the persistent-Markov strategy."""
    blocks = caller_blocks(n)
    hits = dict.fromkeys(blocks, 0)
    rng = np.random.default_rng(seed)
    for i in range(runs):
        r = markov_chain(rng, n)
        for b in blocks:
            hits[b] += _certified(r, 0.525, 1.05, b, i, 1.05)
    out = {}
    for b in blocks:
        q = hits[b] / runs
        floor = (1 / runs) * (1 - 1 / runs)
        out[b] = (q, math.sqrt(max(q * (1 - q), floor) / runs))
    return out


def worst_cell(grid: dict[float | None, tuple[float, float]]) -> float:
    return max(e + 2 * s for e, s in grid.values())


GridFn = Callable[[int], dict[float | None, tuple[float, float]]]
