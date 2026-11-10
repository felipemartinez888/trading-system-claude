"""`h1_compression_breakout` (DRAFT): causality, warm-up, window parity, diagnose <=> on_bar,
parameter validation, geometry, and the draft registry (no run can build it).

Synthetic bars only (seeded random walks); nothing here is market data or an H1 result. No test
runs a backtest of the strategy. Lake-based tests (feasibility count, CLI ``--draft``, gap reset
through `StrategySource`) are in ``tests/unit/backtest/test_h1_draft_feasibility.py`` because they
use the backtest fixtures (`bt_helpers`)."""

from __future__ import annotations

import dataclasses
import math
import random
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tsclaude.contracts.market import Bar, BarSource, Interval, Market
from tsclaude.contracts.signals import Side, SignalStatus
from tsclaude.contracts.timeutil import MS_PER_HOUR
from tsclaude.features.core import BarArrays
from tsclaude.research import live_compat as lc
from tsclaude.risk.config import load_risk_config
from tsclaude.sim.costs import CostModel
from tsclaude.strategies import (
    DRAFT_REGISTRY,
    REGISTRY,
    H1CompressionBreakout,
    H1CompressionBreakoutParams,
    PlanningCosts,
    build_draft_strategy,
    build_strategy,
)
from tsclaude.strategies.h1_compression_breakout import (
    DIAGNOSE_STAGES,
    closed_hour_closes,
    trend_1h,
    trend_warmup,
)

REPO = Path(__file__).resolve().parents[3]
T0 = 1_709_251_200_000  # 2024-03-01T00:00:00Z (hour aligned)
M15 = Interval.M15
COSTS = PlanningCosts.from_cost_model(CostModel(), Market.SPOT, "BTCUSDT")
FAST = H1CompressionBreakoutParams(
    atr_n=5, rank_m=10, rank_max=0.6, range_n=6, vol_n=6, vol_z_min=0.0, trend_n=4,
    max_cost_r=0.5, time_exit_bars=8,
)  # fmt: skip
FIELDS = [f.name for f in dataclasses.fields(BarArrays)]


def _bars(
    n: int,
    seed: int,
    *,
    interval: Interval = M15,
    start: int = T0,
    flat_from: int | None = None,
) -> list[Bar]:
    """Seeded random walk with alternating calm / volatile regimes (synthetic)."""
    rng = random.Random(seed)
    out, p = [], 100.0
    step = interval.ms
    for i in range(n):
        regime = (i // 24) % 2
        sigma = 0.002 if regime == 0 else 0.01
        o = p
        c = o * math.exp(rng.gauss(0.002 if regime else -0.0005, sigma))
        h = max(o, c) * (1 + abs(rng.gauss(0, sigma / 2)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, sigma / 2)))
        v = rng.uniform(1, 3) * (4 if regime else 1)
        if flat_from is not None and i >= flat_from:
            o = h = lo = c = p
            v = 1.0
        p = c
        t = start + i * step
        out.append(
            Bar(
                market=Market.SPOT, symbol="BTCUSDT", interval=interval, open_time_ms=t,
                close_time_ms=t + step - 1, open=o, high=h, low=lo, close=c, volume=v,
                quote_volume=v * c, trades=1, taker_buy_volume=v / 2,
                taker_buy_quote_volume=v * c / 2, source=BarSource.DERIVED,
                ingested_at_ms=t + step,
            )
        )  # fmt: skip
    return out


def _slice(arr: BarArrays, lo: int, hi: int) -> BarArrays:
    return BarArrays(*(np.array(getattr(arr, f)[lo:hi]) for f in FIELDS))


def _windows(strategy: H1CompressionBreakout, bars: Sequence[Bar]) -> list[BarArrays]:
    """The trailing window a backtest gives the strategy at every bar (as `_History.window`)."""
    arr = BarArrays.from_bars(bars)
    w = strategy.window_bars
    return [_slice(arr, max(0, end - w), end) for end in range(1, len(bars) + 1)]


def _mutate(arr: BarArrays, idx: Sequence[int], seed: int) -> BarArrays:
    """A copy with the OHLCV of bars `idx` replaced (open times unchanged)."""
    rng = np.random.default_rng(seed)
    cols = {f: np.array(getattr(arr, f)) for f in FIELDS}
    for i in idx:
        c = float(cols["close"][i]) * float(rng.uniform(0.7, 1.4))
        o = float(cols["open"][i]) * float(rng.uniform(0.7, 1.4))
        cols["open"][i], cols["close"][i] = o, c
        cols["high"][i] = max(o, c) * 1.05
        cols["low"][i] = min(o, c) * 0.95
        cols["volume"][i] = float(rng.uniform(0.1, 50.0))
    return BarArrays(*(cols[f] for f in FIELDS))


# ---------------------------------------------------------------- parameters and identity
@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"interval": "5m"}, "interval must be one of"),
        ({"interval": "4h"}, "interval must be one of"),
        ({"time_exit_bars": None}, "time_exit_bars"),
        ({"time_exit_bars": 0}, "time_exit_bars"),
        ({"time_exit_bars": True}, "time_exit_bars"),
        ({"time_exit_bars": 8.0}, "time_exit_bars"),
        ({"trend_n": 0}, "trend_n"),
        ({"trend_n": 1}, "trend_n"),
        ({"vol_n": 1}, "vol_n"),
        ({"rank_max": 0.0}, "rank_max"),
        ({"rank_max": 1.5}, "rank_max"),
        ({"stop_atr": 0.0}, "stop_atr"),
        ({"tp_r": -1.0}, "tp_r"),
        ({"max_cost_r": 0}, "max_cost_r"),
        ({"watch_atr": -0.1}, "watch_atr"),
        ({"vol_z_min": math.nan}, "vol_z_min"),
    ],
)
def test_parameter_validation(over: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        H1CompressionBreakoutParams(**over)


def test_unknown_parameters_are_refused_by_the_draft_factory() -> None:
    with pytest.raises(ValueError, match="unknown h1_compression_breakout params"):
        build_draft_strategy("h1_compression_breakout", {"ema_span_1h": 50})


def test_the_runnable_registry_refuses_the_draft_and_the_draft_registry_builds_it() -> None:
    assert "h1_compression_breakout" not in REGISTRY
    assert "h1_compression_breakout" in DRAFT_REGISTRY
    with pytest.raises(ValueError, match="unknown strategy"):
        build_strategy("h1_compression_breakout")
    with pytest.raises(ValueError, match="unknown draft strategy"):
        build_draft_strategy("bracket_reference")  # drafts only
    s = build_draft_strategy("h1_compression_breakout", {"tp_r": 1.5})
    assert isinstance(s, H1CompressionBreakout)
    assert s.version == "0.1.0-draft"
    assert s.params.tp_r == 1.5
    assert s.describe()["interval"] == "15m"


def test_identity_is_deterministic_and_parameter_sensitive() -> None:
    a = H1CompressionBreakout(FAST)
    b = build_draft_strategy("h1_compression_breakout", dataclasses.asdict(FAST))
    assert a.params_hash == b.params_hash == a.strategy_hash
    assert a.params_hash != H1CompressionBreakout(dataclasses.replace(FAST, tp_r=1.5)).params_hash
    windows = _windows(a, _bars(300, 3))
    assert [a.on_bar(w, COSTS) for w in windows] == [b.on_bar(w, COSTS) for w in windows]
    assert [a.diagnose(w, COSTS) for w in windows] == [b.diagnose(w, COSTS) for w in windows]


# ---------------------------------------------------------------- warm-up
def test_declared_warmup() -> None:
    default = H1CompressionBreakout()
    assert default.warmup_bars == 50 * 4 + 2 * 3 == 206  # trend term dominates on 15m
    assert default.window_bars == 211
    hourly = H1CompressionBreakout(H1CompressionBreakoutParams(interval="1h"))
    assert hourly.warmup_bars == 14 + 96 + 1  # on 1h the ATR rank term dominates
    assert trend_warmup(Interval.H1, 50) == 50
    # far below the live 30-day backfill (H1-6): about 2.1 days on 15m, 4.6 days on 1h
    assert default.warmup_bars * M15.ms < 3 * 86_400_000
    assert hourly.warmup_bars * Interval.H1.ms < 5 * 86_400_000
    assert H1CompressionBreakout(FAST).warmup_bars == 4 * 4 + 6 == 22


@pytest.mark.parametrize("offset", [0, 1, 2, 3])
def test_no_proposal_before_warmup_and_the_trend_is_defined_at_warmup(offset: int) -> None:
    """Whatever the hour alignment of the first bar, nothing is proposed or diagnosed as ready
    before `warmup_bars` bars, and with exactly `warmup_bars` bars the trend is always defined."""
    strat = H1CompressionBreakout(FAST)
    bars = _bars(200, 11, start=T0 + offset * M15.ms)
    windows = _windows(strat, bars)
    for w in windows[: strat.warmup_bars - 1]:
        assert strat.on_bar(w, COSTS) == ()
        flags = strat.diagnose(w, COSTS)
        assert flags is not None
        assert not any(flags.values())
        assert strat.exit_geometry(w, Side.LONG, COSTS) is None
    arr = BarArrays.from_bars(bars)
    for end in range(strat.warmup_bars, len(bars) + 1):
        w = _slice(arr, end - strat.warmup_bars, end)  # exactly the warm-up, any alignment
        close, mean = trend_1h(w, M15, FAST.trend_n)
        assert math.isfinite(close), end
        assert math.isfinite(mean), end


# ---------------------------------------------------------------- the 1h trend (H1-6)
def _reference_trend(bars: Sequence[Bar], decision: int, n: int) -> tuple[float, float]:
    """Independent reference: hourly closes are the closes of 15m bars ending an hour, at or
    before the decision bar's close; the last `n` of them."""
    t = bars[decision].close_time_ms
    closes = [b.close for b in bars if (b.close_time_ms + 1) % MS_PER_HOUR == 0 and
              b.close_time_ms <= t]  # fmt: skip
    if len(closes) < n:
        return math.nan, math.nan
    tail = closes[-n:]
    return tail[-1], float(np.mean(np.array(tail)))


@pytest.mark.parametrize("offset", [0, 1, 2, 3])
def test_trend_matches_an_independent_hourly_reference(offset: int) -> None:
    strat = H1CompressionBreakout(FAST)
    bars = _bars(240, 21 + offset, start=T0 + offset * M15.ms)
    for i, w in enumerate(_windows(strat, bars)):
        if len(w) < strat.warmup_bars:
            continue
        got = trend_1h(w, M15, FAST.trend_n)
        want = _reference_trend(bars, i, FAST.trend_n)
        assert got[0] == want[0], i
        assert got[1] == pytest.approx(want[1], rel=1e-15, abs=0.0), i


def test_the_forming_hour_is_never_used() -> None:
    """Mutating every bar of the hour that has not closed at the decision bar (including the
    decision bar itself) never changes the trend; when the decision bar closes its hour, that
    hour is used (the canary is live)."""
    strat = H1CompressionBreakout(FAST)
    arr = BarArrays.from_bars(_bars(160, 5))
    used = 0
    for end in range(strat.window_bars, len(arr) + 1):
        w = _slice(arr, end - strat.window_bars, end)
        k = int(w.open_time_ms[-1] % MS_PER_HOUR // M15.ms)  # position of the decision bar
        base = trend_1h(w, M15, FAST.trend_n)
        if k < 3:  # hour still forming: its k+1 bars are in the window
            forming = list(range(len(w) - k - 1, len(w)))
            assert trend_1h(_mutate(w, forming, end), M15, FAST.trend_n) == base
            closes, _ = closed_hour_closes(w, M15)
            assert closes[-1] != w.close[-1]  # the decision bar's close is not an hour close
        else:  # the decision bar closes its hour at T: it is the last closed hour
            changed = trend_1h(_mutate(w, [len(w) - 1], end), M15, FAST.trend_n)
            assert changed[0] != base[0]
            used += 1
    assert used > 0


def test_trend_fails_closed_on_non_contiguous_hours() -> None:
    bars = _bars(80, 7)
    arr = BarArrays.from_bars(bars)
    w = _slice(arr, 0, 80)
    assert math.isfinite(trend_1h(w, M15, 4)[0])
    # drop one bar in the middle of an hour: that hour is not complete, the hours are no longer
    # consecutive and the trend is undefined
    keep = [i for i in range(80) if i != 70]
    gap = BarArrays(*(np.array(getattr(w, f)[keep]) for f in FIELDS))
    assert all(math.isnan(x) for x in trend_1h(gap, M15, 4))
    # an empty or too short window
    assert all(math.isnan(x) for x in trend_1h(_slice(arr, 0, 0), M15, 4))
    assert all(math.isnan(x) for x in trend_1h(_slice(arr, 0, 15), M15, 4))


def test_on_1h_every_bar_is_a_closed_hour() -> None:
    params = dataclasses.replace(FAST, interval="1h")
    strat = H1CompressionBreakout(params)
    bars = _bars(120, 9, interval=Interval.H1)
    for w in _windows(strat, bars)[strat.warmup_bars :]:
        close, mean = trend_1h(w, Interval.H1, params.trend_n)
        assert close == w.close[-1]
        assert mean == float(np.mean(w.close[-params.trend_n :]))


# ---------------------------------------------------------------- causality and parity
def test_causality_canary_future_bars_never_change_a_decision() -> None:
    """Decisions are computed from the history up to the decision bar; mutating every bar after
    it (prices and volumes) never changes the proposals or stage flags at that bar."""
    strat = H1CompressionBreakout(FAST)
    bars = _bars(400, 13)
    arr = BarArrays.from_bars(bars)
    rng = random.Random(0)
    for i in rng.sample(range(strat.warmup_bars, len(arr) - 1), 60):
        mutated = _mutate(arr, list(range(i + 1, len(arr))), i)
        for full in (arr, mutated):
            assert np.array_equal(full.close[: i + 1], arr.close[: i + 1])
        lo = max(0, i + 1 - strat.window_bars)
        a = _slice(arr, lo, i + 1)
        b = _slice(mutated, lo, i + 1)
        assert strat.on_bar(a, COSTS) == strat.on_bar(b, COSTS)
        assert strat.diagnose(a, COSTS) == strat.diagnose(b, COSTS)


def test_window_parity_trailing_window_equals_full_history() -> None:
    """Every feature is a finite window: the trailing window of `window_bars` bars and the full
    history up to the decision bar give identical proposals and stage flags."""
    strat = H1CompressionBreakout(FAST)
    for seed in (1, 2):
        bars = _bars(500, seed, start=T0 + seed * M15.ms)
        arr = BarArrays.from_bars(bars)
        for end in range(strat.warmup_bars, len(arr) + 1, 3):
            full = _slice(arr, 0, end)
            tail = _slice(arr, max(0, end - strat.window_bars), end)
            assert strat.on_bar(full, COSTS) == strat.on_bar(tail, COSTS), end
            assert strat.diagnose(full, COSTS) == strat.diagnose(tail, COSTS), end


# ---------------------------------------------------------------- diagnose <=> on_bar
@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"max_cost_r": 0.25},
        {"max_cost_r": 0.15, "stop_atr": 1.0},
        {"vol_z_min": 1.0, "rank_max": 0.35},
        {"rank_max": 1.0, "vol_z_min": -5.0, "trend_n": 2},
        {"interval": "1h"},
    ],
)
def test_all_stages_pass_iff_on_bar_confirms(overrides: dict[str, Any]) -> None:
    strat = H1CompressionBreakout(dataclasses.replace(FAST, **overrides))
    interval = strat.interval
    seen = {"confirmed": 0, "watch": 0, "cost_fail": 0, "not_ready": 0, "trend_fail": 0}
    for seed in range(1, 11):
        bars = _bars(400, seed, interval=interval, flat_from=330 if seed % 4 == 0 else None)
        for window in _windows(strat, bars):
            flags = strat.diagnose(window, COSTS)
            assert flags is not None
            assert tuple(flags) == DIAGNOSE_STAGES
            props = strat.on_bar(window, COSTS)
            confirmed = [p for p in props if p.stage is SignalStatus.CONFIRMED]
            assert all(flags.values()) == bool(confirmed)
            for p in props:
                assert p.side is Side.LONG
                assert p.time_exit_bars == strat.params.time_exit_bars
            if confirmed:
                seen["confirmed"] += 1
                assert confirmed[0].planned_cost_r is not None
                assert confirmed[0].planned_cost_r <= strat.params.max_cost_r
            if any(p.stage is SignalStatus.WATCH for p in props):
                seen["watch"] += 1
                assert not flags["breakout"]
                assert flags["compressed"]
                assert flags["trend_1h"]
            if not flags["warmup_ready"]:
                seen["not_ready"] += 1
                assert not any(flags.values())
            if flags["compressed"] and not flags["trend_1h"]:
                seen["trend_fail"] += 1
                assert props == ()
            stages = list(flags.values())
            if all(stages[:-1]) and not stages[-1]:
                seen["cost_fail"] += 1
                assert props == ()
    assert seen["confirmed"] > 0
    assert seen["not_ready"] > 0
    assert seen["trend_fail"] > 0  # the trend stage is exercised, not vacuous
    if overrides.get("max_cost_r", 0.5) < 0.3:
        assert seen["cost_fail"] > 0


def test_probe_on_an_empty_window_declares_the_stages() -> None:
    flags = H1CompressionBreakout().diagnose(_slice(BarArrays.from_bars([]), 0, 0), COSTS)
    assert flags is not None
    assert tuple(flags) == DIAGNOSE_STAGES
    assert not any(flags.values())


# ---------------------------------------------------------------- geometry
def test_geometry_exit_geometry_and_break_even_convention() -> None:
    strat = H1CompressionBreakout(dataclasses.replace(FAST, tp_r=1.5, max_cost_r=0.25))
    assert strat.bracket_geometry(COSTS) == (1.5, COSTS.round_trip_frac / 0.25)
    assert strat.design_p_star(COSTS) is None  # time exit required -> realized p*
    assert strat.stop_loss_r(COSTS) == 1.25
    assert strat.max_holding_ms() == (8 + 1) * M15.ms
    windows = _windows(strat, _bars(300, 4))
    assert all(strat.exit_geometry(w, Side.SHORT, COSTS) is None for w in windows)  # long only
    accepted = [g for g in (strat.exit_geometry(w, Side.LONG, COSTS) for w in windows) if g]
    assert accepted  # the cost filter lets some bars through on this data
    found = accepted[0]
    assert found.stage is SignalStatus.CONFIRMED
    risk = found.ref_price - found.stop_price
    assert risk > 0
    assert found.take_profit_price == pytest.approx(found.ref_price + 1.5 * risk)
    assert found.time_exit_bars == 8
    assert found.valid_bars == 1
    assert "time exit 8 bars" in found.exit_policy


def test_huge_stop_fails_stop_valid_and_confirms_nothing() -> None:
    strat = H1CompressionBreakout(dataclasses.replace(FAST, stop_atr=500.0))
    reached = 0
    for window in _windows(strat, _bars(400, 2)):
        flags = strat.diagnose(window, COSTS)
        assert flags is not None
        if all(list(flags.values())[:6]):
            reached += 1
            assert not flags["stop_valid"]
            assert not flags["cost_ok"]
            assert not any(p.stage is SignalStatus.CONFIRMED for p in strat.on_bar(window, COSTS))
    assert reached > 0


@pytest.mark.parametrize("tp_r", [0.5, 1.0, 1.5])
def test_live_compatibility_of_the_draft_geometries(tp_r: float) -> None:
    """H1-5 / D-E as a regression: with the live floor 1.5, every tp_r <= 1.5 is INCOMPATIBLE."""
    risk = load_risk_config(REPO / "configs" / "risk.yaml")
    assert risk.min_rr_net == Decimal("1.5")
    got = lc.assess(
        H1CompressionBreakout(H1CompressionBreakoutParams(tp_r=tp_r)), risk=risk,
        cost_model=CostModel(), market=Market.SPOT, symbol="BTCUSDT",
    )  # fmt: skip
    assert got.status == lc.INCOMPATIBLE
    assert got.strategy_min_stop_fraction == pytest.approx(COSTS.round_trip_frac / 0.2)
