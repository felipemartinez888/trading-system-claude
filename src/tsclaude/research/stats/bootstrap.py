"""Stationary block bootstrap (Politis & Romano 1994) and automatic block length.

Stationary bootstrap
--------------------
Politis, D. N. & Romano, J. P. (1994), "The Stationary Bootstrap", JASA 89(428), 1303-1313.
A resample of length ``n`` is a concatenation of blocks taken from the series wrapped on a circle.
Each block starts at an index drawn uniformly from ``{0..n-1}``; block lengths are i.i.d.
Geometric with mean ``mean_block`` (success probability ``q = 1 / mean_block``). Equivalently, for
``t >= 1``::

    with prob. q:     I_t = U_t                (start a new block, U_t ~ Uniform{0..n-1})
    with prob. 1 - q: I_t = (I_{t-1} + 1) mod n (continue the current block)

and ``I_0 = U_0``. ``mean_block = 1`` reproduces the ordinary i.i.d. bootstrap (Efron 1979).
The resampled series is strictly stationary conditional on the data, which is why it is preferred
to fixed-length blocks for dependent trade sequences (overlapping or clustered trades).

Percentile intervals
--------------------
For ``B`` resampled statistics ``T*_1..T*_B``, the percentile interval at level ``1 - alpha`` is
``[Q(alpha/2), Q(1 - alpha/2)]`` (Efron & Tibshirani 1993, §13.3); one-sided lower/upper bounds use
``Q(alpha)`` / ``Q(1 - alpha)``. Quantiles of the empirical distribution are taken *conservatively*:
the lower endpoint uses the order statistic at or below the target rank (``numpy`` method
``"lower"``) and the upper endpoint the one at or above it (method ``"higher"``), so discreteness
never narrows the interval.

Determinism: all randomness comes from ``numpy.random.Generator(PCG64(seed))``, whose stream is
platform-independent; resamples are generated in row chunks whose size depends only on ``n``.

Automatic block length
----------------------
Politis, D. N. & White, H. (2004), "Automatic Block-Length Selection for the Dependent Bootstrap",
Econometric Reviews 23(1), 53-70, with the correction of Patton, A., Politis, D. N. & White, H.
(2009), Econometric Reviews 28(4), 372-375 (the stationary-bootstrap constant ``D_SB`` was off by a
factor; the corrected value is ``D_SB = 2 g(0)^2``)::

    b_opt = (2 G^2 / D_SB)^(1/3) n^(1/3),
    G     = sum_{|k| <= M} lambda(k/M) |k| R(k),      g(0) = sum_{|k| <= M} lambda(k/M) R(k),

with sample autocovariances ``R(k) = n^-1 sum_t (x_t - xbar)(x_{t+k} - xbar)`` and the flat-top
kernel ``lambda(t) = 1`` for ``|t| <= 1/2``, ``2 (1 - |t|)`` for ``1/2 < |t| <= 1``, else 0.
The bandwidth ``M = 2 m_hat`` where ``m_hat`` is the smallest lag after which ``K_N`` consecutive
sample autocorrelations are insignificant, ``|rho(m_hat + k)| < c sqrt(log10(n)/n)`` for
``k = 1..K_N`` (``c = 2``, ``K_N = max(5, ceil(sqrt(log10 n)))``); ``m_hat`` and ``M`` are capped at
``m_max = ceil(sqrt(n)) + K_N``. As in Patton's reference implementation, ``b_opt`` is capped at
``ceil(min(3 sqrt(n), n/3))``; this module also floors it at 1.

Block-sum evaluation
--------------------
For statistics that are functions of resample **sums** of per-observation columns (means, counts,
sums of wins and of losses), ``stationary_bootstrap_sums`` evaluates the same stationary bootstrap
without materializing indices: block lengths ``l_j ~ Geometric(q)`` and starts ``s_j ~
Uniform{0..n-1}`` are drawn (the Politis-Romano construction), blocks are concatenated and the last
one truncated so the total is exactly ``n``, and each block sum is a difference of circular prefix
sums ``P[s_j + l_j] - P[s_j]`` over the doubled series. Cost is ``O(n / mean_block)`` per resample
instead of ``O(n)``. For ``mean_block = 1`` (i.i.d. bootstrap) indices are drawn directly.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from tsclaude.research.stats._validate import as_float, as_int, check_open_prob, check_side
from tsclaude.research.stats.binomial import Interval

FloatArray = NDArray[np.float64]
IntArray = NDArray[np.int64]
Statistic = Callable[[FloatArray], float]
VectorizedStatistic = Callable[[FloatArray], FloatArray]

_CHUNK_ELEMENTS = 2_000_000


def _check_mean_block(mean_block: object, n: int) -> float:
    mb = as_float(mean_block, "mean_block")
    if mb < 1.0:
        raise ValueError(f"mean_block must be >= 1, got {mb}")
    return min(mb, float(n)) if n > 0 else mb


def stationary_bootstrap_indices(
    n: int, mean_block: float, rng: np.random.Generator, n_resamples: int = 1
) -> IntArray:
    """Index matrix of shape ``(n_resamples, n)`` for the stationary bootstrap.

    Row ``b`` gives the positions of the original series that form resample ``b``.
    """
    ni = as_int(n, "n")
    if ni < 1:
        raise ValueError(f"n must be >= 1, got {ni}")
    nr = as_int(n_resamples, "n_resamples")
    if nr < 1:
        raise ValueError(f"n_resamples must be >= 1, got {nr}")
    q = 1.0 / _check_mean_block(mean_block, ni)
    starts = rng.integers(0, ni, size=(nr, ni), dtype=np.int64)
    if q >= 1.0:
        return starts
    new_block = rng.random(size=(nr, ni)) < q
    new_block[:, 0] = True
    pos = np.arange(ni, dtype=np.int64)
    # position of the most recent block start at or before t
    last = np.maximum.accumulate(np.where(new_block, pos, 0), axis=1)
    start_at_last = np.take_along_axis(starts, last, axis=1)
    out: IntArray = (start_at_last + (pos - last)) % ni
    return out


def _as_series(values: Sequence[float] | FloatArray) -> FloatArray:
    x = np.asarray(values, dtype=np.float64)
    if x.ndim != 1 or x.size == 0:
        raise ValueError("values must be a non-empty 1-D sequence")
    if not np.all(np.isfinite(x)):
        raise ValueError("values must be finite")
    return x


def _resample_chunks(
    x: FloatArray, n_resamples: int, mean_block: float | None, seed: int
) -> Iterator[FloatArray]:
    """Yield resampled rows in chunks; the stream depends only on ``(n, n_resamples, mb, seed)``."""
    n = int(x.size)
    nr = as_int(n_resamples, "n_resamples")
    if nr < 1:
        raise ValueError(f"n_resamples must be >= 1, got {nr}")
    mb = 1.0 if mean_block is None else _check_mean_block(mean_block, n)
    rng = np.random.Generator(np.random.PCG64(as_int(seed, "seed")))
    rows = max(1, _CHUNK_ELEMENTS // n)
    done = 0
    while done < nr:
        r = min(rows, nr - done)
        yield x[stationary_bootstrap_indices(n, mb, rng, r)]
        done += r


def _check_replicates(out: FloatArray, allow_neg_inf: bool) -> FloatArray:
    bad = np.isnan(out) | (out == math.inf)
    if not allow_neg_inf:
        bad |= out == -math.inf
    if np.any(bad):
        raise ValueError("statistic returned non-finite values on some resamples")
    out.sort()
    return out


def bootstrap_statistics(
    values: Sequence[float] | FloatArray,
    statistics: Mapping[str, VectorizedStatistic],
    *,
    n_resamples: int = 10_000,
    mean_block: float | None = None,
    seed: int = 0,
    allow_neg_inf: Sequence[str] = (),
) -> dict[str, FloatArray]:
    """Sorted replicates of several vectorized statistics computed on the **same** resamples.

    Each statistic receives a chunk ``(rows, n)`` and returns one value per row. Statistics named
    in ``allow_neg_inf`` may return ``-inf`` to mark a resample on which they are undefined and
    must count *against* the hypothesis (a lower bound then becomes ``-inf`` if more than the
    tail fraction of resamples is undefined). NaN and ``+inf`` are always rejected.
    """
    x = _as_series(values)
    nr = as_int(n_resamples, "n_resamples")
    outs = {name: np.empty(nr, dtype=np.float64) for name in statistics}
    done = 0
    for sample in _resample_chunks(x, nr, mean_block, seed):
        r = sample.shape[0]
        for name, stat in statistics.items():
            vals = np.asarray(stat(sample), dtype=np.float64)
            if vals.shape != (r,):
                raise ValueError("vectorized statistic must return one value per resample row")
            outs[name][done : done + r] = vals
        done += r
    return {name: _check_replicates(o, name in allow_neg_inf) for name, o in outs.items()}


def bootstrap_distribution(
    values: Sequence[float] | FloatArray,
    statistic: Statistic | VectorizedStatistic,
    *,
    n_resamples: int = 10_000,
    mean_block: float | None = None,
    seed: int = 0,
    vectorized: bool = False,
) -> FloatArray:
    """Bootstrap replicates ``T*_1..T*_B`` of ``statistic`` (sorted ascending).

    ``mean_block=None`` means the i.i.d. bootstrap (``mean_block = 1``). With ``vectorized=True``
    the statistic receives a 2-D chunk ``(rows, n)`` and must return one value per row.
    """
    x = _as_series(values)
    nr = as_int(n_resamples, "n_resamples")
    out = np.empty(max(nr, 0), dtype=np.float64)
    done = 0
    for sample in _resample_chunks(x, nr, mean_block, seed):
        r = sample.shape[0]
        if vectorized:
            vals = np.asarray(statistic(sample), dtype=np.float64)
            if vals.shape != (r,):
                raise ValueError("vectorized statistic must return one value per resample row")
            out[done : done + r] = vals
        else:
            for i in range(r):
                out[done + i] = float(statistic(sample[i]))
        done += r
    return _check_replicates(out, allow_neg_inf=False)


def replicates_degenerate(replicates: FloatArray, rel_tol: float = 1e-12) -> bool:
    """True when all bootstrap replicates are equal up to floating rounding (zero-width bootstrap).

    "Equal" means ``max - min <= rel_tol * max(1, |max|, |min|)``; all ``-inf`` counts as equal.
    """
    reps = np.asarray(replicates, dtype=np.float64)
    if reps.size == 0:
        return True
    lo, hi = float(np.min(reps)), float(np.max(reps))
    if lo == hi:
        return True
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return False
    return hi - lo <= rel_tol * max(1.0, abs(lo), abs(hi))


def _order_stat(sorted_reps: FloatArray, q: float, round_up: bool) -> float:
    """Order statistic at virtual index ``q (B - 1)``, floored (lower) or ceiled (upper).

    Identical to ``numpy.quantile(method="lower" / "higher")`` but with no arithmetic on the
    values, so ``-inf`` replicates are handled exactly.
    """
    pos = q * (sorted_reps.size - 1)
    idx = math.ceil(pos) if round_up else math.floor(pos)
    return float(sorted_reps[min(max(idx, 0), sorted_reps.size - 1)])


def percentile_bounds(
    replicates: FloatArray,
    alpha: float,
    side: Literal["two-sided", "lower", "upper"] = "two-sided",
) -> Interval:
    """Conservative percentile interval from bootstrap replicates (see module docstring).

    One-sided intervals are open on the other side (``-inf`` / ``+inf``).
    """
    a = check_open_prob(alpha, "alpha")
    s = check_side(side)
    reps = np.sort(np.asarray(replicates, dtype=np.float64))
    if reps.size == 0 or np.any(np.isnan(reps)):
        raise ValueError("replicates must be non-empty and free of NaN")
    if s == "two-sided":
        return Interval(_order_stat(reps, a / 2.0, False), _order_stat(reps, 1.0 - a / 2.0, True))
    if s == "lower":
        return Interval(_order_stat(reps, a, False), math.inf)
    return Interval(-math.inf, _order_stat(reps, 1.0 - a, True))


def bootstrap_ci(
    values: Sequence[float] | FloatArray,
    statistic: Statistic | VectorizedStatistic,
    alpha: float = 0.05,
    n_resamples: int = 10_000,
    mean_block: float | None = None,
    seed: int = 0,
    *,
    side: Literal["two-sided", "lower", "upper"] = "two-sided",
    vectorized: bool = False,
) -> Interval:
    """Stationary-bootstrap percentile confidence interval; deterministic for a given ``seed``."""
    reps = bootstrap_distribution(
        values,
        statistic,
        n_resamples=n_resamples,
        mean_block=mean_block,
        seed=seed,
        vectorized=vectorized,
    )
    return percentile_bounds(reps, alpha, side)


def row_mean(sample: FloatArray) -> FloatArray:
    """Vectorized mean per resample row (for ``vectorized=True``)."""
    out: FloatArray = np.mean(sample, axis=1)
    return out


def draw_stationary_blocks(
    rng: np.random.Generator, rows: int, n: int, mean_block: float
) -> tuple[NDArray[np.int32], NDArray[np.int32]]:
    """Starts and truncated lengths of stationary-bootstrap blocks, each array ``(rows, K)``.

    Lengths are ``Geometric(1 / mean_block)``; blocks are drawn until every row covers ``n``
    observations, then truncated so each row's lengths sum to exactly ``n`` (unused blocks get
    length 0). Deterministic for a given generator state.
    """
    q = 1.0 / _check_mean_block(mean_block, n)
    k = math.ceil(n * q + 6.0 * math.sqrt(n * q) + 8.0)
    log1mq = math.log1p(-q) if q < 1.0 else -math.inf

    def geometric(shape: tuple[int, int]) -> NDArray[np.int32]:
        # inverse CDF: P(floor(ln(1-U)/ln(1-q)) + 1 > j) = (1-q)^j, i.e. exactly Geometric(q)
        if q >= 1.0:
            return np.ones(shape, dtype=np.int32)
        raw = np.floor(np.log1p(-rng.random(shape)) / log1mq) + 1.0
        out: NDArray[np.int32] = np.minimum(raw, n).astype(np.int32)
        return out

    lengths = geometric((rows, k))
    starts = rng.integers(0, n, size=(rows, k), dtype=np.int32)
    while bool(np.any(lengths.sum(axis=1, dtype=np.int64) < n)):  # top up (rare)
        lengths = np.concatenate([lengths, geometric((rows, k))], axis=1)
        starts = np.concatenate([starts, rng.integers(0, n, size=(rows, k), dtype=np.int32)], 1)
    before = np.cumsum(lengths, axis=1, dtype=np.int32) - lengths
    eff: NDArray[np.int32] = np.clip(n - before, 0, lengths)
    return starts, eff


def stationary_bootstrap_sums(
    columns: FloatArray,
    *,
    n_resamples: int,
    mean_block: float | None,
    seed: int,
) -> FloatArray:
    """Resample sums of each column of ``columns`` (shape ``(k, n)``) -> array ``(k, n_resamples)``.

    Same resampling distribution as ``stationary_bootstrap_indices`` (see module docstring).
    Lengths are capped at ``n`` before truncation, which does not change the truncated blocks.
    """
    cols = np.asarray(columns, dtype=np.float64)
    if cols.ndim != 2 or cols.shape[1] == 0:
        raise ValueError("columns must be a 2-D array (k, n) with n >= 1")
    if not np.all(np.isfinite(cols)):
        raise ValueError("columns must be finite")
    ncol, n = cols.shape
    nr = as_int(n_resamples, "n_resamples")
    if nr < 1:
        raise ValueError(f"n_resamples must be >= 1, got {nr}")
    mb = 1.0 if mean_block is None else _check_mean_block(mean_block, n)
    rng = np.random.Generator(np.random.PCG64(as_int(seed, "seed")))
    out = np.empty((ncol, nr), dtype=np.float64)
    done = 0
    if mb <= 1.0:
        rows = max(1, _CHUNK_ELEMENTS // n)
        while done < nr:
            r = min(rows, nr - done)
            idx = rng.integers(0, n, size=(r, n), dtype=np.int32)
            for c in range(ncol):
                out[c, done : done + r] = cols[c][idx].sum(axis=1)
            done += r
        return out
    prefix = np.zeros((ncol, 2 * n + 1), dtype=np.float64)
    prefix[:, 1:] = np.cumsum(np.concatenate([cols, cols], axis=1), axis=1)
    per_row = max(1, math.ceil(n / mb + 6.0 * math.sqrt(n / mb) + 8.0))
    rows = max(1, _CHUNK_ELEMENTS // per_row)
    while done < nr:
        r = min(rows, nr - done)
        starts, eff = draw_stationary_blocks(rng, r, n, mb)
        ends = starts + eff
        for c in range(ncol):
            out[c, done : done + r] = (prefix[c][ends] - prefix[c][starts]).sum(axis=1)
        done += r
    return out


def _flat_top(t: FloatArray) -> FloatArray:
    a = np.abs(t)
    out: FloatArray = np.where(a <= 0.5, 1.0, np.where(a <= 1.0, 2.0 * (1.0 - a), 0.0))
    return out


def optimal_block_length(values: Sequence[float] | FloatArray) -> float:
    """Politis-White (2004) / Patton-Politis-White (2009) stationary-bootstrap mean block length."""
    x = _as_series(values)
    n = int(x.size)
    if n < 4:
        return 1.0
    xc = x - x.mean()
    r0 = float(np.dot(xc, xc)) / n
    if r0 <= 0.0:
        return 1.0
    kn = max(5, math.ceil(math.sqrt(math.log10(n))))
    m_max = math.ceil(math.sqrt(n)) + kn
    max_lag = min(n - 1, m_max + kn)
    acov = np.array(
        [r0] + [float(np.dot(xc[: n - k], xc[k:])) / n for k in range(1, max_lag + 1)],
        dtype=np.float64,
    )
    rho = acov / r0
    threshold = 2.0 * math.sqrt(math.log10(n) / n)
    insignificant = np.abs(rho[1:]) < threshold  # lag k at index k-1
    m_hat = m_max
    for m in range(0, max_lag - kn + 1):
        if bool(np.all(insignificant[m : m + kn])):
            m_hat = m
            break
    big_m = min(2 * m_hat, m_max, max_lag)
    if big_m == 0:
        return 1.0
    lags = np.arange(-big_m, big_m + 1)
    lam = _flat_top(lags / big_m)
    r = acov[np.abs(lags)]
    g_big = float(np.sum(lam * np.abs(lags) * r))
    g0 = float(np.sum(lam * r))
    if g0 <= 0.0:
        return 1.0
    d_sb = 2.0 * g0 * g0
    b = (2.0 * g_big * g_big / d_sb) ** (1.0 / 3.0) * n ** (1.0 / 3.0)
    b_max = math.ceil(min(3.0 * math.sqrt(n), n / 3.0))
    return float(min(max(b, 1.0), b_max))
