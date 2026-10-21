"""Strategy interface and `bracket_reference`: identity, window parity, look-ahead canary, rules."""

from __future__ import annotations

import dataclasses
import math
import random

import pytest

from tsclaude.contracts.market import Bar, BarSource, Interval, Market
from tsclaude.contracts.signals import EntryRuleKind, Side, SignalStatus
from tsclaude.features.core import BarArrays
from tsclaude.sim.costs import CostModel
from tsclaude.strategies import (
    BracketReference,
    BracketReferenceParams,
    PlanningCosts,
    Proposal,
    build_strategy,
)

T0 = 1_709_251_200_000  # 2024-03-01T00:00:00Z
PARAMS = BracketReferenceParams(
    atr_n=5, rank_m=10, rank_max=0.6, range_n=6, vol_n=6, vol_z_min=0.0, ema_span=5, rv_n=5,
    max_cost_r=0.5,
)  # fmt: skip
COSTS = PlanningCosts.from_cost_model(CostModel(), Market.SPOT, "BTCUSDT")


def _bars(n: int, seed: int = 3) -> list[Bar]:
    rng = random.Random(seed)
    out, p = [], 100.0
    for i in range(n):
        regime = (i // 24) % 2
        sigma = 0.002 if regime == 0 else 0.01
        o = p
        c = o * math.exp(rng.gauss(0.002 if regime else 0.0, sigma))
        h = max(o, c) * (1 + abs(rng.gauss(0, sigma / 2)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, sigma / 2)))
        p = c
        t = T0 + i * Interval.M15.ms
        v = rng.uniform(1, 3) * (4 if regime else 1)
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


def _key(p: Proposal) -> tuple[object, ...]:
    return (p.stage, p.side, p.entry_kind, p.valid_bars, p.time_exit_bars)


def test_identity_params_hash_and_frozen() -> None:
    a, b = BracketReference(PARAMS), BracketReference(PARAMS)
    assert a.params_hash == b.params_hash
    assert len(a.params_hash) == 64
    assert a.strategy_hash == a.params_hash
    assert BracketReference(dataclasses.replace(PARAMS, tp_r=0.8)).params_hash != a.params_hash
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.params.tp_r = 2.0  # type: ignore[misc]
    d = a.describe()
    assert d["strategy_id"] == "bracket_reference"
    assert d["version"] == BracketReference.version
    assert d["warmup_bars"] == a.warmup_bars


def test_registry_builds_and_fails_closed() -> None:
    s = build_strategy("bracket_reference", {"tp_r": 0.8})
    assert s.params.tp_r == 0.8  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="unknown strategy"):
        build_strategy("nope")
    with pytest.raises(ValueError, match="unknown bracket_reference params"):
        build_strategy("bracket_reference", {"tp": 1})


def test_trailing_window_equals_full_history_decisions() -> None:
    s = BracketReference(PARAMS)
    a = BarArrays.from_bars(_bars(600))
    seen = 0
    for i in range(s.warmup_bars - 1, len(a)):
        hist = a.head(i + 1)
        full = s.on_bar(hist, COSTS)
        live = s.on_bar(hist.tail(s.window_bars), COSTS)
        assert [_key(p) for p in full] == [_key(p) for p in live], i
        for f, w in zip(full, live, strict=True):
            assert w.stop_price == pytest.approx(f.stop_price, rel=1e-9)
            assert (w.take_profit_price or 0) == pytest.approx(f.take_profit_price or 0, rel=1e-9)
        seen += len(full)
    assert seen > 0


def test_lookahead_canary_future_bars_never_change_past_proposals() -> None:
    s = BracketReference(PARAMS)
    bars = _bars(400)
    cut = 250
    mutated = bars[:cut] + [
        b.model_copy(update={"high": b.high * 3, "close": b.close * 2, "open": b.open * 2,
                             "volume": b.volume * 100})
        for b in bars[cut:]
    ]  # fmt: skip
    a, m = BarArrays.from_bars(bars), BarArrays.from_bars(mutated)
    for i in range(s.warmup_bars - 1, cut):
        assert s.on_bar(a.head(i + 1).tail(s.window_bars), COSTS) == s.on_bar(
            m.head(i + 1).tail(s.window_bars), COSTS
        )


def test_confirmed_geometry_and_cost_filter() -> None:
    s = BracketReference(PARAMS)
    a = BarArrays.from_bars(_bars(600))
    confirmed: list[Proposal] = []
    for i in range(s.warmup_bars - 1, len(a)):
        confirmed += [
            p for p in s.on_bar(a.head(i + 1).tail(s.window_bars), COSTS)
            if p.stage is SignalStatus.CONFIRMED
        ]  # fmt: skip
    assert confirmed
    for p in confirmed:
        assert p.side is Side.LONG
        assert p.entry_kind is EntryRuleKind.MARKET_NEXT_OPEN
        risk = p.ref_price - p.stop_price
        assert risk > 0
        assert p.take_profit_price == pytest.approx(p.ref_price + PARAMS.tp_r * risk)
        assert p.planned_cost_r is not None
        assert p.planned_cost_r <= PARAMS.max_cost_r
        assert p.conditions["breakout"] is True
    # prohibitive costs: nothing is ever confirmed
    dear = PlanningCosts(0.5, "test")
    for i in range(s.warmup_bars - 1, len(a)):
        assert all(
            p.stage is not SignalStatus.CONFIRMED
            for p in s.on_bar(a.head(i + 1).tail(s.window_bars), dear)
        )


def test_short_window_gives_nothing() -> None:
    s = BracketReference(PARAMS)
    a = BarArrays.from_bars(_bars(s.warmup_bars - 1))
    assert s.on_bar(a, COSTS) == ()
    assert s.exit_geometry(a, Side.LONG, COSTS) is None


def test_design_break_even_and_stop_loss() -> None:
    s = BracketReference(PARAMS)
    assert s.design_p_star(COSTS) == pytest.approx((1 + 0.5) / (1 + 1.0))
    assert s.stop_loss_r(COSTS) == pytest.approx(1.5)
    timed = BracketReference(dataclasses.replace(PARAMS, time_exit_bars=8))
    assert timed.design_p_star(COSTS) is None  # time exits: realized p*


def test_planning_costs_are_all_taker_round_trip() -> None:
    c = PlanningCosts.from_cost_model(CostModel(), Market.SPOT, "BTCUSDT")
    # VIP-0 spot taker 0.1 % and 3 bps default slippage on both legs
    assert c.round_trip_frac == pytest.approx(2 * (0.001 + 0.0003))
    assert c.cost_r(100.0, 99.0) == pytest.approx(c.round_trip_frac * 100.0)
    assert c.cost_r(100.0, 100.0) == math.inf
