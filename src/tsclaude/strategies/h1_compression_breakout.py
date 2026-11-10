"""`h1_compression_breakout` (DRAFT 0.1.0-draft): hypothesis H1 of RESEARCH_PROTOCOL §8.

**Draft, not runnable.** The strategy is registered only in `strategies.DRAFT_REGISTRY`, which
nothing that records a trial reads (`backtest run`, `walkforward`, the runner). The only consumer
is the P&L-free feasibility count (`tsclaude backtest feasibility --draft`) and preregistration
validation, which lists the draft status as an open item so `freeze` refuses. Promotion to
`REGISTRY` is an owner-approved change. No backtest of this strategy has been run and no observed
profit shaped these rules: they are the H1-v1 draft rules with the audit's fixes
(`docs/MVP_CLOSURE_HANDOFF.md` §5, H1-6 and H1-7) and an `interval` parameter for the H1-1
feasibility question.

**Long only, spot.** Shorts need USD-M history and funding, which are not imported (D-02, D-22);
the strategy proposes nothing but LONG entries. It can be evaluated on USD-M bars once they exist,
with the same long rules.

Rules at the close ``T`` of strategy bar ``i`` (``interval`` 15m by default, 1h allowed), using only
the trailing window that ends at bar ``i`` (nothing later exists in it):

- *Compression* at ``i-1``: ``atr_percentile_rank(atr_n, rank_m)[i-1] <= rank_max``.
- *1h trend* (H1-6: a finite window, no EMA): ``C_last > mean(C_1 .. C_trend_n)`` where ``C_k`` are
  the closes of the last ``trend_n`` **closed** 1h bars, ``C_last`` the most recent of them. They
  are built from the strategy bars in the window: an hour is a group of ``3600000 / interval_ms``
  contiguous strategy bars whose first bar opens on a UTC hour boundary, and it is used only if
  its last bar is in the window (so the hour closed at or before ``T``). The hour that contains
  bar ``i`` and has not closed yet (the *forming* hour) is never used; when bar ``i`` is the last
  bar of its hour, that hour closed at ``T`` and is the most recent closed hour. On ``interval=1h``
  each strategy bar is a closed hour. The last ``trend_n`` hours must be consecutive and the most
  recent must be the hour that closed last before or at ``T``; otherwise the trend is undefined and
  nothing is proposed (fail closed).
- *Breakout*: ``close_i > range_high(range_n)[i-1]``.
- *Participation*: ``volume_zscore(vol_n)_i >= vol_z_min`` (undefined z: not passed).
- **WATCH** when compressed, trend holds and
  ``0 <= range_high[i-1] - close_i <= watch_atr x ATR_i``.
- **CONFIRMED** when compressed, trend, breakout and participation hold and the cost filter passes.
- Geometry: market entry at the next 1m open (valid one strategy bar); stop
  ``close_i - stop_atr x ATR_i``; take-profit ``close_i + tp_r x (close_i - stop)``; a **required**
  time exit after ``time_exit_bars`` strategy bars (H1-7), so one maximum holding period is
  ``(time_exit_bars + 1) x interval`` from the decision (fill within one bar, then the time exit)
  and an embargo can be shown to cover it. SL first when both levels are inside one 1m bar
  (simulator rule).
- Cost filter: planned all-taker round-trip cost (`PlanningCosts`) in R ``<= max_cost_r``.

Break-even: the time exit makes payoffs variable, so ``design_p_star`` is None and certification
uses the realized p* (the convention of `bracket_reference` with a time exit); ``stop_loss_r`` is
``1 + max_cost_r``, the planned loss at the stop including costs.

Warm-up (`warmup_bars`) = ``max(atr_n + rank_m + 1, range_n + 1, vol_n, trend_n x g + 2 (g - 1))``
with ``g = 3600000 / interval_ms`` strategy bars per hour: the trend term covers ``trend_n`` full
hours plus at most ``g - 1`` bars of the forming hour and ``g - 1`` bars of a partial leading hour
at the start of the window. With the defaults on 15m this is 206 bars (51.5 hours), far below the
live ``max_backfill_days: 30`` (the EMA(50) of H1-v1 needed 741 hourly bars). Every feature is a
finite window, so values over the trailing window equal values over the full history exactly.

`diagnose` reports the CONFIRMED rule stage by stage (`DIAGNOSE_STAGES`); it and `on_bar` share
`_signal` / `_geometry`, so they cannot drift.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from tsclaude.contracts.market import Interval
from tsclaude.contracts.signals import EntryRuleKind, Side, SignalStatus
from tsclaude.contracts.timeutil import MS_PER_HOUR
from tsclaude.features.core import (
    BarArrays,
    FloatArray,
    atr_percentile_rank,
    range_high,
    volume_zscore,
)
from tsclaude.features.core import atr as atr_fn
from tsclaude.strategies.base import PlanningCosts, Proposal, Strategy

#: decision intervals the draft accepts (H1-1: 15m vs 1h with the same rules)
ALLOWED_INTERVALS: tuple[str, ...] = ("15m", "1h")


def _pos_int(name: str, value: object, minimum: int = 1) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}, got {value!r}")


def _real(name: str, value: object) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    return float(value)


@dataclass(frozen=True, slots=True)
class H1CompressionBreakoutParams:
    interval: str = "15m"
    atr_n: int = 14
    rank_m: int = 96
    rank_max: float = 0.35
    range_n: int = 20
    vol_n: int = 20
    vol_z_min: float = 1.0
    trend_n: int = 50  # closed 1h bars in the trend mean (H1-6)
    stop_atr: float = 1.5
    tp_r: float = 1.0
    max_cost_r: float = 0.2
    watch_atr: float = 0.5
    time_exit_bars: int = 48  # strategy bars; required (H1-7)

    def __post_init__(self) -> None:
        if self.interval not in ALLOWED_INTERVALS:
            raise ValueError(f"interval must be one of {list(ALLOWED_INTERVALS)}")
        for name in ("atr_n", "rank_m", "range_n", "time_exit_bars"):
            _pos_int(name, getattr(self, name))
        _pos_int("vol_n", self.vol_n, 2)
        _pos_int("trend_n", self.trend_n, 2)  # a close is never above a mean of itself alone
        rank_max = _real("rank_max", self.rank_max)
        if not 0.0 < rank_max <= 1.0:
            raise ValueError("rank_max must be in (0, 1]")
        _real("vol_z_min", self.vol_z_min)
        for name in ("stop_atr", "tp_r", "max_cost_r"):
            if _real(name, getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")
        if _real("watch_atr", self.watch_atr) < 0:
            raise ValueError("watch_atr must be >= 0")


#: `H1CompressionBreakout.diagnose` stages, in the order the LONG CONFIRMED rule applies them.
DIAGNOSE_STAGES: tuple[str, ...] = (
    "warmup_ready",  # the window holds `warmup_bars` strategy bars
    "features_defined",  # every gating feature is defined (volume z may be undefined)
    "compressed",  # atr_rank(atr_n, rank_m)[i-1] <= rank_max
    "trend_1h",  # last closed 1h close > mean of the last trend_n closed 1h closes
    "breakout",  # close_i > range_high(range_n)[i-1]
    "volume_ok",  # volume_z(vol_n)_i defined and >= vol_z_min
    "stop_valid",  # stop = close_i - stop_atr x ATR_i is finite and > 0, ATR_i > 0
    "cost_ok",  # planned round-trip cost in R <= max_cost_r
)


def bars_per_hour(interval: Interval) -> int:
    step = interval.ms
    if step > MS_PER_HOUR or MS_PER_HOUR % step:
        raise ValueError(f"interval {interval} does not divide one hour")
    return MS_PER_HOUR // step


def closed_hour_closes(w: BarArrays, interval: Interval) -> tuple[FloatArray, np.ndarray]:
    """Closes and open times of the complete, hour-aligned 1h groups whose last strategy bar is
    in `w` (so each hour closed at or before the last bar's close). The forming hour (its last bar
    not in `w`) and a partial leading hour are excluded. Pure function of `w`."""
    g = bars_per_hour(interval)
    step = interval.ms
    ot = w.open_time_ms
    n = len(w)
    if g == 1:
        return np.asarray(w.close, dtype=np.float64), np.asarray(ot, dtype=np.int64)
    starts = np.nonzero(ot % MS_PER_HOUR == 0)[0]
    starts = starts[starts + g - 1 < n]
    # bars are strictly increasing on the interval grid: equal span <=> g contiguous bars
    starts = starts[ot[starts + g - 1] - ot[starts] == (g - 1) * step]
    return np.asarray(w.close[starts + g - 1], dtype=np.float64), np.asarray(ot[starts])


def trend_1h(w: BarArrays, interval: Interval, trend_n: int) -> tuple[float, float]:
    """(close of the last closed 1h bar, simple mean of the last `trend_n` closed 1h closes), or
    NaNs when they are not defined: fewer than `trend_n` closed hours, hours not consecutive, or
    the most recent closed hour is not the one that closed last at or before the window's end."""
    nan = (math.nan, math.nan)
    if len(w) == 0:
        return nan
    closes, hour_open = closed_hour_closes(w, interval)
    if closes.shape[0] < trend_n:
        return nan
    last_close_ms = int(w.close_time_ms[-1])
    # the hour that closed last at or before T opens at floor((T + 1) / 1h) x 1h - 1h
    expected_last = ((last_close_ms + 1) // MS_PER_HOUR) * MS_PER_HOUR - MS_PER_HOUR
    tail_open = hour_open[-trend_n:]
    if int(tail_open[-1]) != expected_last:
        return nan
    if not bool(np.all(np.diff(tail_open) == MS_PER_HOUR)):
        return nan
    tail = closes[-trend_n:]
    return float(tail[-1]), float(np.mean(tail))


def trend_warmup(interval: Interval, trend_n: int) -> int:
    """Strategy bars that always contain `trend_n` closed hours, whatever the alignment."""
    g = bars_per_hour(interval)
    return trend_n * g + 2 * (g - 1)


@dataclass(frozen=True, slots=True)
class _Geometry:
    stop: float
    cost_r: float


@dataclass(frozen=True, slots=True)
class _Signal:
    features: dict[str, float]
    compressed: bool
    trend: bool
    breakout: bool
    volume_ok: bool

    @property
    def confirmed_conditions(self) -> bool:
        return self.compressed and self.trend and self.breakout and self.volume_ok


class H1CompressionBreakout(Strategy[H1CompressionBreakoutParams]):
    strategy_id = "h1_compression_breakout"
    version = "0.1.0-draft"

    def __init__(self, params: H1CompressionBreakoutParams | None = None) -> None:
        super().__init__(params or H1CompressionBreakoutParams())

    @property
    def interval(self) -> Interval:
        return Interval(self.params.interval)

    @property
    def warmup_bars(self) -> int:
        p = self.params
        return max(
            p.atr_n + p.rank_m + 1,  # rank at i-1
            p.range_n + 1,  # range high at i-1
            p.vol_n,
            trend_warmup(self.interval, p.trend_n),
        )

    def _features(self, w: BarArrays) -> dict[str, float]:
        p = self.params
        rank = atr_percentile_rank(w, p.atr_n, p.rank_m)
        hi = range_high(w, p.range_n)
        h_close, h_mean = trend_1h(w, self.interval, p.trend_n)
        return {
            "close": float(w.close[-1]),
            "atr": float(atr_fn(w, p.atr_n)[-1]),
            "atr_rank_prev": float(rank[-2]),
            "range_high_prev": float(hi[-2]),
            "trend_close_1h": h_close,
            "trend_mean_1h": h_mean,
            "volume_z": float(volume_zscore(w, p.vol_n)[-1]),
        }

    def _geometry(self, close: float, atr_value: float, costs: PlanningCosts) -> _Geometry | None:
        """Stop and planned cost in R from the decision-bar close; None if the stop is invalid."""
        stop = close - self.params.stop_atr * atr_value
        if not math.isfinite(stop) or stop <= 0 or not atr_value > 0:
            return None
        return _Geometry(stop, costs.cost_r(close, stop))

    def _cost_ok(self, cost_r: float) -> bool:
        return not cost_r > self.params.max_cost_r

    def _bracket(
        self, close: float, atr_value: float, costs: PlanningCosts, stage: SignalStatus
    ) -> Proposal | None:
        p = self.params
        geo = self._geometry(close, atr_value, costs)
        if geo is None:
            return None
        if stage is SignalStatus.CONFIRMED and not self._cost_ok(geo.cost_r):
            return None
        return Proposal(
            stage=stage,
            side=Side.LONG,
            entry_kind=EntryRuleKind.MARKET_NEXT_OPEN,
            ref_price=close,
            stop_price=geo.stop,
            take_profit_price=close + p.tp_r * (close - geo.stop),
            valid_bars=1,
            time_exit_bars=p.time_exit_bars,
            exit_policy=(
                f"bracket SL {p.stop_atr} ATR / TP {p.tp_r} R / time exit {p.time_exit_bars} bars"
            ),
            planned_cost_r=geo.cost_r,
        )

    def _signal(self, window: BarArrays) -> _Signal | None:
        """Features and entry conditions at the last bar; None before the warm-up or while a
        gating feature is undefined. Shared by `on_bar` and `diagnose` so they cannot drift."""
        p = self.params
        if len(window) < self.warmup_bars:
            return None
        f = self._features(window)
        if any(math.isnan(v) for k, v in f.items() if k != "volume_z"):
            return None
        return _Signal(
            features=f,
            compressed=f["atr_rank_prev"] <= p.rank_max,
            trend=f["trend_close_1h"] > f["trend_mean_1h"],
            breakout=f["close"] > f["range_high_prev"],
            volume_ok=not math.isnan(f["volume_z"]) and f["volume_z"] >= p.vol_z_min,
        )

    def on_bar(self, window: BarArrays, costs: PlanningCosts) -> Sequence[Proposal]:
        p = self.params
        sig = self._signal(window)
        if sig is None:
            return ()
        f = sig.features
        conditions: dict[str, float | bool] = {
            **{k: v for k, v in f.items() if not math.isnan(v)},
            "compressed": sig.compressed,
            "trend_1h": sig.trend,
            "breakout": sig.breakout,
            "volume_ok": sig.volume_ok,
        }
        if sig.confirmed_conditions:
            prop = self._bracket(f["close"], f["atr"], costs, SignalStatus.CONFIRMED)
            return () if prop is None else (_with(prop, conditions),)
        near = 0.0 <= f["range_high_prev"] - f["close"] <= p.watch_atr * f["atr"]
        if sig.compressed and sig.trend and near:
            prop = self._bracket(f["close"], f["atr"], costs, SignalStatus.WATCH)
            return () if prop is None else (_with(prop, conditions),)
        return ()

    def diagnose(self, window: BarArrays, costs: PlanningCosts) -> Mapping[str, bool] | None:
        """LONG entry stages in the order `on_bar` applies them (`DIAGNOSE_STAGES`).

        All stages pass  <=>  `on_bar` returns a CONFIRMED proposal. WATCH proposals are not an
        entry and are not counted. A stage that cannot be evaluated (warm-up not ready, an
        undefined feature, an invalid stop) and every stage after it is False."""
        out = dict.fromkeys(DIAGNOSE_STAGES, False)
        out["warmup_ready"] = len(window) >= self.warmup_bars
        sig = self._signal(window)
        if sig is None:
            return out
        out["features_defined"] = True
        out["compressed"] = sig.compressed
        out["trend_1h"] = sig.trend
        out["breakout"] = sig.breakout
        out["volume_ok"] = sig.volume_ok
        geo = self._geometry(sig.features["close"], sig.features["atr"], costs)
        out["stop_valid"] = geo is not None
        out["cost_ok"] = geo is not None and self._cost_ok(geo.cost_r)
        return out

    def exit_geometry(self, window: BarArrays, side: Side, costs: PlanningCosts) -> Proposal | None:
        """A CONFIRMED market entry at the last bar with this strategy's exits (random control):
        the same stop, take-profit, time exit and cost filter, without the entry conditions."""
        if side is not Side.LONG or len(window) < self.warmup_bars:
            return None
        a = float(atr_fn(window, self.params.atr_n)[-1])
        if math.isnan(a):
            return None
        return self._bracket(float(window.close[-1]), a, costs, SignalStatus.CONFIRMED)

    def design_p_star(self, costs: PlanningCosts) -> float | None:
        return None  # the required time exit makes payoffs variable: realized p*

    def stop_loss_r(self, costs: PlanningCosts) -> float | None:
        return 1.0 + self.params.max_cost_r

    def bracket_geometry(self, costs: PlanningCosts) -> tuple[float, float | None]:
        """(take-profit in stop distances, smallest stop distance as a fraction of price that
        the cost filter lets through): what `research.live_compat` checks against live rules."""
        return self.params.tp_r, costs.round_trip_frac / self.params.max_cost_r

    def max_holding_ms(self) -> int:
        """Longest scheduled span from the decision close to a time exit: one validity bar for
        the fill plus `time_exit_bars` bars (exits deferred by candles without trades or by data
        gaps can add to it; an embargo should keep a margin)."""
        return (self.params.time_exit_bars + 1) * self.interval.ms


def _with(prop: Proposal, conditions: dict[str, float | bool]) -> Proposal:
    return Proposal(
        stage=prop.stage,
        side=prop.side,
        entry_kind=prop.entry_kind,
        ref_price=prop.ref_price,
        stop_price=prop.stop_price,
        take_profit_price=prop.take_profit_price,
        valid_bars=prop.valid_bars,
        exit_policy=prop.exit_policy,
        entry_price=prop.entry_price,
        time_exit_bars=prop.time_exit_bars,
        planned_cost_r=prop.planned_cost_r,
        conditions=conditions,
    )
