"""`Strategy.diagnose` and `BracketReference.diagnose`: stage flags mirror the CONFIRMED rule.

Mapping (documented in `BracketReference.diagnose`): every stage passes exactly when `on_bar`
returns a CONFIRMED proposal. WATCH proposals are not entries; on such bars at least one stage
fails (breakout, since WATCH requires the close at or below the prior range high)."""

from __future__ import annotations

import dataclasses
import math
import random
from collections.abc import Sequence

import numpy as np
import pytest

from tsclaude.contracts.market import Bar, BarSource, Interval, Market
from tsclaude.contracts.signals import SignalStatus
from tsclaude.features.core import BarArrays
from tsclaude.sim.costs import CostModel
from tsclaude.strategies import BracketReference, BracketReferenceParams, PlanningCosts, Strategy
from tsclaude.strategies.bracket_reference import DIAGNOSE_STAGES

T0 = 1_709_251_200_000  # 2024-03-01T00:00:00Z
BASE = BracketReferenceParams(
    atr_n=5, rank_m=10, rank_max=0.6, range_n=6, vol_n=6, vol_z_min=0.0, ema_span=5, rv_n=5,
    max_cost_r=0.5,
)  # fmt: skip
COSTS = PlanningCosts.from_cost_model(CostModel(), Market.SPOT, "BTCUSDT")


def _bars(n: int, seed: int, *, flat_from: int | None = None) -> list[Bar]:
    rng = random.Random(seed)
    out, p = [], 100.0
    for i in range(n):
        regime = (i // 24) % 2
        sigma = 0.002 if regime == 0 else 0.01
        o = p
        c = o * math.exp(rng.gauss(0.002 if regime else 0.0, sigma))
        h = max(o, c) * (1 + abs(rng.gauss(0, sigma / 2)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, sigma / 2)))
        v = rng.uniform(1, 3) * (4 if regime else 1)
        if flat_from is not None and i >= flat_from:  # degenerate: zero range, constant volume
            o = h = lo = c = p
            v = 1.0
        p = c
        t = T0 + i * Interval.M15.ms
        out.append(
            Bar(
                market=Market.SPOT, symbol="BTCUSDT", interval=Interval.M15, open_time_ms=t,
                close_time_ms=t + Interval.M15.ms - 1, open=o, high=h, low=lo, close=c, volume=v,
                quote_volume=v * c, trades=1, taker_buy_volume=v / 2,
                taker_buy_quote_volume=v * c / 2, source=BarSource.DERIVED,
                ingested_at_ms=t + Interval.M15.ms,
            )
        )  # fmt: skip
    return out


def _windows(strategy: Strategy[BracketReferenceParams], bars: Sequence[Bar]) -> list[BarArrays]:
    arr = BarArrays.from_bars(bars)
    w = strategy.window_bars
    out = []
    for end in range(1, len(bars) + 1):
        lo = max(0, end - w)
        out.append(
            BarArrays(*(getattr(arr, f.name)[lo:end] for f in dataclasses.fields(BarArrays)))
        )
    return out


def test_base_strategy_diagnose_defaults_to_none() -> None:
    """The base hook is optional: a strategy that does not override it declares no stages."""
    strat = BracketReference(BASE)
    assert Strategy.diagnose(strat, BarArrays.from_bars([]), COSTS) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"max_cost_r": 0.25},  # the cost filter bites on part of the bars
        {"max_cost_r": 0.15, "stop_atr": 1.0},
        {"vol_z_min": 1.0, "rank_max": 0.35},
        {"rank_max": 1.0, "vol_z_min": -5.0},
    ],
)
def test_all_stages_pass_iff_on_bar_confirms(overrides: dict[str, float]) -> None:
    strat = BracketReference(dataclasses.replace(BASE, **overrides))
    seen = {"confirmed": 0, "watch": 0, "cost_fail": 0, "not_ready": 0}
    for seed in range(1, 9):
        bars = _bars(400, seed, flat_from=330 if seed % 4 == 0 else None)
        for window in _windows(strat, bars):
            flags = strat.diagnose(window, COSTS)
            assert flags is not None
            assert tuple(flags) == DIAGNOSE_STAGES
            props = strat.on_bar(window, COSTS)
            confirmed = any(p.stage is SignalStatus.CONFIRMED for p in props)
            assert all(flags.values()) == confirmed
            if confirmed:
                seen["confirmed"] += 1
            if any(p.stage is SignalStatus.WATCH for p in props):
                seen["watch"] += 1
                assert not flags["breakout"]
            if not flags["warmup_ready"]:
                seen["not_ready"] += 1
                assert not any(flags.values())
            stages = list(flags.values())
            if all(stages[:-1]) and not stages[-1]:
                seen["cost_fail"] += 1
                assert props == ()  # a cost-filtered breakout is neither CONFIRMED nor WATCH
    assert seen["confirmed"] > 0
    assert seen["not_ready"] > 0
    if overrides.get("max_cost_r", 0.5) < 0.3:
        assert seen["cost_fail"] > 0  # the cost stage is exercised, not vacuous


def test_diagnose_uses_only_the_window_and_is_pure() -> None:
    strat = BracketReference(BASE)
    windows = _windows(strat, _bars(200, 5))
    for w in windows[-40:]:
        a = strat.diagnose(w, COSTS)
        copy = BarArrays(*(np.array(getattr(w, f.name)) for f in dataclasses.fields(BarArrays)))
        assert strat.diagnose(copy, COSTS) == a == strat.diagnose(w, COSTS)


def test_invalid_stop_fails_stop_valid_and_on_bar_confirms_nothing() -> None:
    """A stop at or below zero (stop_atr huge) fails `stop_valid`; `on_bar` confirms nothing."""
    strat = BracketReference(dataclasses.replace(BASE, stop_atr=500.0))
    reached = 0
    for window in _windows(strat, _bars(400, 2)):
        flags = strat.diagnose(window, COSTS)
        assert flags is not None
        stages = list(flags.values())
        if all(stages[:6]):
            reached += 1
            assert not flags["stop_valid"]
            assert not flags["cost_ok"]
            assert not any(p.stage is SignalStatus.CONFIRMED for p in strat.on_bar(window, COSTS))
    assert reached > 0


def test_undefined_feature_fails_features_defined() -> None:
    """A NaN price in the window makes a gating feature undefined: nothing passes after
    `warmup_ready`, and `on_bar` returns nothing."""
    strat = BracketReference(BASE)
    w = _windows(strat, _bars(200, 3))[-1]
    close = np.array(w.close)
    close[-1] = float("nan")
    bad = dataclasses.replace(w, close=close)
    flags = strat.diagnose(bad, COSTS)
    assert flags is not None
    assert flags["warmup_ready"]
    assert not any(list(flags.values())[1:])
    assert strat.on_bar(bad, COSTS) == ()
