"""`bracket_reference`: a simple compression-breakout bracket used to exercise the pipeline.

**Not claimed profitable.** It exists so the runner, simulator, certification and report can be
tested end to end with a documented, deterministic rule set (loosely H1 of RESEARCH_PROTOCOL §8).

Rules on each closed strategy bar ``i`` (long only, spot or USD-M):

- *Compression* at ``i-1``: ``atr_rank(atr_n, rank_m)[i-1] <= rank_max``.
- *Trend filter*: ``close_i > ema(ema_span)_i``.
- **WATCH** when compressed, trend filter holds and ``close_i`` is within ``watch_atr x ATR_i``
  below the prior range high ``range_high(range_n)[i-1]`` (not above it).
- **CONFIRMED** when compressed, trend filter holds, ``close_i > range_high(range_n)[i-1]`` and
  ``volume_z(vol_n)_i >= vol_z_min``.
- Geometry: market entry at the next 1m open (valid one strategy bar), stop
  ``close_i - stop_atr x ATR_i``, take-profit ``close_i + tp_r x (close_i - stop)``; optional time
  exit after ``time_exit_bars`` strategy bars (None = pure bracket).
- Cost filter: the planned all-taker round-trip cost in R must be ``<= max_cost_r``; otherwise
  nothing is confirmed. Therefore, for the pure bracket, ``p* = (1 + max_cost_r) / (1 + tp_r)`` is
  an upper bound of every trade's design break-even ``(1 + c)/(1 + b)`` and is used as the
  design p* (ADR-0012), with ``stop_loss_r = 1 + max_cost_r``. Caveat: stop and take-profit are
  placed from the **decision-bar close**, not from the fill; the entry fills at the next 1m open
  plus slippage, so the realized take-profit distance ``b`` (in R at the fill) and cost ``c``
  differ from the planned ones and the design p* is not a strict bound on realized trades.
  Certification always also uses the realized joint margin and takes the minimum, which covers
  this difference.

Realized volatility is reported as a market condition only; it does not gate.

`diagnose` reports the CONFIRMED rule stage by stage (`DIAGNOSE_STAGES`) for P&L-free
feasibility counts; it and `on_bar` share `_signal` / `_geometry`, so they cannot drift.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from tsclaude.contracts.market import Interval
from tsclaude.contracts.signals import EntryRuleKind, Side, SignalStatus
from tsclaude.features.core import (
    BarArrays,
    atr_percentile_rank,
    ema,
    ema_warmup,
    range_high,
    realized_vol,
    volume_zscore,
)
from tsclaude.features.core import atr as atr_fn
from tsclaude.strategies.base import PlanningCosts, Proposal, Strategy


@dataclass(frozen=True, slots=True)
class BracketReferenceParams:
    interval: str = "15m"
    atr_n: int = 14
    rank_m: int = 96
    rank_max: float = 0.35
    range_n: int = 20
    vol_n: int = 20
    vol_z_min: float = 1.0
    ema_span: int = 50
    rv_n: int = 20
    stop_atr: float = 1.5
    tp_r: float = 1.0
    max_cost_r: float = 0.2
    watch_atr: float = 0.5
    time_exit_bars: int | None = None

    def __post_init__(self) -> None:
        Interval(self.interval)
        if not 0.0 < self.rank_max <= 1.0:
            raise ValueError("rank_max must be in (0, 1]")
        if self.stop_atr <= 0 or self.tp_r <= 0 or self.max_cost_r <= 0:
            raise ValueError("stop_atr, tp_r and max_cost_r must be positive")
        if self.time_exit_bars is not None and self.time_exit_bars < 1:
            raise ValueError("time_exit_bars must be >= 1")


#: `BracketReference.diagnose` stages, in the order the LONG CONFIRMED rule applies them.
DIAGNOSE_STAGES: tuple[str, ...] = (
    "warmup_ready",  # the window holds `warmup_bars` strategy bars
    "features_defined",  # every gating feature is defined (volume z may be undefined)
    "compressed",  # atr_rank(atr_n, rank_m)[i-1] <= rank_max
    "trend",  # close_i > ema(ema_span)_i
    "breakout",  # close_i > range_high(range_n)[i-1]
    "volume_ok",  # volume_z(vol_n)_i defined and >= vol_z_min
    "stop_valid",  # stop = close_i - stop_atr x ATR_i is finite and > 0, ATR_i > 0
    "cost_ok",  # planned round-trip cost in R <= max_cost_r
)


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


class BracketReference(Strategy[BracketReferenceParams]):
    strategy_id = "bracket_reference"
    version = "1.0.0"

    def __init__(self, params: BracketReferenceParams | None = None) -> None:
        super().__init__(params or BracketReferenceParams())

    @property
    def interval(self) -> Interval:
        return Interval(self.params.interval)

    @property
    def warmup_bars(self) -> int:
        p = self.params
        return max(
            p.atr_n + p.rank_m + 1,  # rank at i-1
            p.range_n + 1,  # range high at i-1
            ema_warmup(p.ema_span),
            p.vol_n,
            p.rv_n + 1,
        )

    def _features(self, w: BarArrays) -> dict[str, float]:
        p = self.params
        rank = atr_percentile_rank(w, p.atr_n, p.rank_m)
        hi = range_high(w, p.range_n)
        return {
            "close": float(w.close[-1]),
            "atr": float(atr_fn(w, p.atr_n)[-1]),
            "atr_rank_prev": float(rank[-2]),
            "range_high_prev": float(hi[-2]),
            "ema": float(ema(w, p.ema_span)[-1]),
            "volume_z": float(volume_zscore(w, p.vol_n)[-1]),
            "realized_vol": float(realized_vol(w, p.rv_n)[-1]),
        }

    def _geometry(self, close: float, atr_value: float, costs: PlanningCosts) -> _Geometry | None:
        """Stop and planned cost in R from the decision-bar close; None if the stop is invalid."""
        stop = close - self.params.stop_atr * atr_value
        if not math.isfinite(stop) or stop <= 0 or atr_value <= 0:
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
        stop, cost_r = geo.stop, geo.cost_r
        if stage is SignalStatus.CONFIRMED and not self._cost_ok(cost_r):
            return None
        return Proposal(
            stage=stage,
            side=Side.LONG,
            entry_kind=EntryRuleKind.MARKET_NEXT_OPEN,
            ref_price=close,
            stop_price=stop,
            take_profit_price=close + p.tp_r * (close - stop),
            valid_bars=1,
            time_exit_bars=p.time_exit_bars,
            exit_policy=(
                f"bracket SL {p.stop_atr} ATR / TP {p.tp_r} R"
                + ("" if p.time_exit_bars is None else f" / time exit {p.time_exit_bars} bars")
            ),
            planned_cost_r=cost_r,
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
            trend=f["close"] > f["ema"],
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
            "trend": sig.trend,
            "breakout": sig.breakout,
            "volume_ok": sig.volume_ok,
        }
        if sig.confirmed_conditions:
            prop = self._bracket(f["close"], f["atr"], costs, SignalStatus.CONFIRMED)
            if prop is not None:
                return (_with(prop, conditions),)
            return ()
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
        out["trend"] = sig.trend
        out["breakout"] = sig.breakout
        out["volume_ok"] = sig.volume_ok
        geo = self._geometry(sig.features["close"], sig.features["atr"], costs)
        out["stop_valid"] = geo is not None
        out["cost_ok"] = geo is not None and self._cost_ok(geo.cost_r)
        return out

    def exit_geometry(self, window: BarArrays, side: Side, costs: PlanningCosts) -> Proposal | None:
        if side is not Side.LONG or len(window) < self.warmup_bars:
            return None
        a = float(atr_fn(window, self.params.atr_n)[-1])
        if math.isnan(a):
            return None
        return self._bracket(float(window.close[-1]), a, costs, SignalStatus.CONFIRMED)

    def design_p_star(self, costs: PlanningCosts) -> float | None:
        p = self.params
        if p.time_exit_bars is not None:
            return None  # time exits make payoffs variable: realized p*
        return (1.0 + p.max_cost_r) / (1.0 + p.tp_r)

    def stop_loss_r(self, costs: PlanningCosts) -> float | None:
        return 1.0 + self.params.max_cost_r

    def bracket_geometry(self, costs: PlanningCosts) -> tuple[float, float | None]:
        """(take-profit in stop distances, smallest stop distance as a fraction of price that
        the cost filter lets through): what `research.live_compat` checks against live rules."""
        return self.params.tp_r, costs.round_trip_frac / self.params.max_cost_r


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
