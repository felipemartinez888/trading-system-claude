"""Pure windowed features (ADR-0006): ``f(bars[0..i]) -> value at i``, NaN before warm-up.

Every function takes a `BarArrays` (closed bars in time order) and returns a float64 array of the
same length whose entry ``i`` depends **only on bars 0..i**. ``warmup`` is the number of bars a
value needs: entries ``i < warmup - 1`` are NaN. Windowed statistics are computed row by row over
``sliding_window_view``, so a value is bitwise identical whether it is computed over the full
history or over a trailing window that contains its lookback (window parity is exact). The only
recursive feature, `ema`, converges: its declared warm-up makes the influence of the seed smaller
than ``EMA_TOLERANCE`` (relative), and values before that warm-up are NaN in both evaluations.

Definitions (n, m: window lengths in bars):

- ``true_range[i] = max(h_i, c_{i-1}) - min(l_i, c_{i-1})``; warm-up 2.
- ``atr(n)[i]`` = simple mean of the last n true ranges; warm-up n + 1.
- ``atr_percentile_rank(n, m)[i]`` = share of the last m ATR values that are <= ATR_i, in (0, 1];
  warm-up n + m.
- ``range_high(n)[i]`` / ``range_low(n)[i]`` = max high / min low of bars i-n+1..i; warm-up n.
  (Breakout rules compare against index i-1, the range *before* the decision bar.)
- ``ema(span)`` with ``alpha = 2 / (span + 1)``, seeded with the first close of the series given;
  warm-up ``ema_warmup(span)``.
- ``volume_zscore(n)[i] = (v_i - mean) / std`` over the last n volumes (population std; NaN when
  the std is 0); warm-up n.
- ``realized_vol(n)[i]`` = sample std (ddof 1) of the last n log close-to-close returns, per bar
  (not annualized); warm-up n + 1.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from numpy.typing import NDArray

from tsclaude.contracts.market import Bar

FEATURES_VERSION = "features-v1"
EMA_TOLERANCE = 1e-12

type FloatArray = NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class BarArrays:
    open_time_ms: NDArray[np.int64]
    close_time_ms: NDArray[np.int64]
    open: FloatArray
    high: FloatArray
    low: FloatArray
    close: FloatArray
    volume: FloatArray

    @classmethod
    def from_bars(cls, bars: Sequence[Bar]) -> BarArrays:
        return cls(
            np.array([b.open_time_ms for b in bars], dtype=np.int64),
            np.array([b.close_time_ms for b in bars], dtype=np.int64),
            np.array([b.open for b in bars], dtype=np.float64),
            np.array([b.high for b in bars], dtype=np.float64),
            np.array([b.low for b in bars], dtype=np.float64),
            np.array([b.close for b in bars], dtype=np.float64),
            np.array([b.volume for b in bars], dtype=np.float64),
        )

    def __len__(self) -> int:
        return int(self.close.shape[0])

    def tail(self, length: int) -> BarArrays:
        """The last `length` bars (a trailing window)."""
        s = slice(max(0, len(self) - length), None)
        return BarArrays(
            self.open_time_ms[s],
            self.close_time_ms[s],
            self.open[s],
            self.high[s],
            self.low[s],
            self.close[s],
            self.volume[s],
        )

    def head(self, length: int) -> BarArrays:
        """The first `length` bars (history up to a decision bar)."""
        s = slice(0, length)
        return BarArrays(
            self.open_time_ms[s],
            self.close_time_ms[s],
            self.open[s],
            self.high[s],
            self.low[s],
            self.close[s],
            self.volume[s],
        )


@dataclass(frozen=True, slots=True)
class Feature:
    """A named, versioned feature with its declared warm-up (bars needed for a value)."""

    name: str
    warmup: int
    fn: Callable[[BarArrays], FloatArray]
    version: str = FEATURES_VERSION

    def __call__(self, bars: BarArrays) -> FloatArray:
        return self.fn(bars)

    def at_last(self, window: BarArrays) -> float:
        """Value at the last bar of a trailing window (NaN if the window is too short)."""
        if len(window) < self.warmup:
            return math.nan
        return float(self.fn(window)[-1])


def _check_n(n: int, name: str = "n") -> None:
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise ValueError(f"{name} must be a positive int, got {n!r}")


def _nan(length: int) -> FloatArray:
    return np.full(length, np.nan, dtype=np.float64)


def _rolling(x: FloatArray, n: int, stat: Callable[[FloatArray], FloatArray]) -> FloatArray:
    """``out[i] = stat(x[i-n+1..i])`` row by row; NaN for i < n - 1 (and where x is NaN)."""
    out = _nan(x.shape[0])
    if x.shape[0] >= n:
        out[n - 1 :] = stat(sliding_window_view(x, n))
    return out


def true_range(b: BarArrays) -> FloatArray:
    out = _nan(len(b))
    if len(b) >= 2:
        prev = b.close[:-1]
        out[1:] = np.maximum(b.high[1:], prev) - np.minimum(b.low[1:], prev)
    return out


def atr(b: BarArrays, n: int) -> FloatArray:
    _check_n(n)
    tr = true_range(b)
    return _rolling(tr, n, lambda w: w.mean(axis=1))


def atr_percentile_rank(b: BarArrays, n: int, m: int) -> FloatArray:
    _check_n(n)
    _check_n(m, "m")
    a = atr(b, n)

    def rank(w: FloatArray) -> FloatArray:
        last = w[:, -1:]
        counts: FloatArray = (w <= last).sum(axis=1).astype(np.float64)
        return counts / float(w.shape[1])

    out = _rolling(a, m, rank)
    out[np.isnan(a)] = np.nan
    # windows that still contain NaN ATRs are not valid yet
    if len(b) >= 1:
        out[: n + m - 1] = np.nan
    return out


def range_high(b: BarArrays, n: int) -> FloatArray:
    _check_n(n)
    return _rolling(b.high, n, lambda w: w.max(axis=1))


def range_low(b: BarArrays, n: int) -> FloatArray:
    _check_n(n)
    return _rolling(b.low, n, lambda w: w.min(axis=1))


def ema_warmup(span: int, tolerance: float = EMA_TOLERANCE) -> int:
    """Bars after which the seed's weight ``(1 - alpha)^k`` is below `tolerance`, plus `span`."""
    _check_n(span, "span")
    alpha = 2.0 / (span + 1.0)
    if alpha >= 1.0:
        return 1
    return span + math.ceil(math.log(tolerance) / math.log(1.0 - alpha))


def ema(b: BarArrays, span: int) -> FloatArray:
    _check_n(span, "span")
    alpha = 2.0 / (span + 1.0)
    x = b.close
    out = _nan(x.shape[0])
    if x.shape[0] == 0:
        return out
    raw = np.empty_like(x)
    acc = float(x[0])
    for i in range(x.shape[0]):  # recursive by definition; seeded with the first close
        acc = acc + alpha * (float(x[i]) - acc)
        raw[i] = acc
    w = ema_warmup(span)
    out[w - 1 :] = raw[w - 1 :]
    return out


def volume_zscore(b: BarArrays, n: int) -> FloatArray:
    _check_n(n)
    if n < 2:
        raise ValueError("volume_zscore needs n >= 2")

    def z(w: FloatArray) -> FloatArray:
        mu = w.mean(axis=1)
        sd = w.std(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            res: FloatArray = np.where(sd > 0.0, (w[:, -1] - mu) / sd, np.nan)
        return res

    return _rolling(b.volume, n, z)


def realized_vol(b: BarArrays, n: int) -> FloatArray:
    _check_n(n)
    if n < 2:
        raise ValueError("realized_vol needs n >= 2")
    r = _nan(len(b))
    if len(b) >= 2:
        r[1:] = np.log(b.close[1:] / b.close[:-1])
    return _rolling(r, n, lambda w: w.std(axis=1, ddof=1))


# ---------------------------------------------------------------- named feature constructors
def atr_feature(n: int) -> Feature:
    return Feature(f"atr_{n}", n + 1, lambda b: atr(b, n))


def atr_rank_feature(n: int, m: int) -> Feature:
    return Feature(f"atr_rank_{n}_{m}", n + m, lambda b: atr_percentile_rank(b, n, m))


def range_high_feature(n: int) -> Feature:
    return Feature(f"range_high_{n}", n, lambda b: range_high(b, n))


def range_low_feature(n: int) -> Feature:
    return Feature(f"range_low_{n}", n, lambda b: range_low(b, n))


def ema_feature(span: int) -> Feature:
    return Feature(f"ema_{span}", ema_warmup(span), lambda b: ema(b, span))


def volume_z_feature(n: int) -> Feature:
    return Feature(f"volume_z_{n}", n, lambda b: volume_zscore(b, n))


def realized_vol_feature(n: int) -> Feature:
    return Feature(f"realized_vol_{n}", n + 1, lambda b: realized_vol(b, n))
