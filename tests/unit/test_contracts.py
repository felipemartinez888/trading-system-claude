from __future__ import annotations

import pytest
from pydantic import ValidationError

from tsclaude.contracts.ids import new_ulid, ulid_timestamp_ms
from tsclaude.contracts.market import Bar, BarSource, Interval, Market
from tsclaude.contracts.provenance import Provenance, Sourced
from tsclaude.contracts.signals import (
    Condition,
    EntryRule,
    EntryRuleKind,
    RunMode,
    Side,
    SignalRecord,
    SignalStatus,
    can_transition,
)
from tsclaude.contracts.timeutil import ManualClock, from_iso, to_iso

T0 = 1_735_689_600_000  # 2025-01-01T00:00:00Z


# ---------------------------------------------------------------- time
def test_iso_roundtrip() -> None:
    assert to_iso(T0) == "2025-01-01T00:00:00.000Z"
    assert from_iso("2025-01-01T00:00:00.000Z") == T0
    assert from_iso(to_iso(T0 + 1234)) == T0 + 1234


@pytest.mark.parametrize("bad", ["2025-01-01T00:00:00", "2025-01-01T00:00:00+02:00"])
def test_iso_rejects_naive_or_non_utc(bad: str) -> None:
    with pytest.raises(ValueError, match=r"timezone|UTC"):
        from_iso(bad)


def test_manual_clock_never_goes_back() -> None:
    clock = ManualClock(10)
    clock.advance(5)
    assert clock.now_ms() == 15
    with pytest.raises(ValueError, match="backwards"):
        clock.set(1)


# ---------------------------------------------------------------- ids
def test_ulid_encodes_time_and_sorts() -> None:
    a = new_ulid(T0, randomness=0)
    b = new_ulid(T0 + 1, randomness=0)
    assert len(a) == 26
    assert ulid_timestamp_ms(a) == T0
    assert a < b


# ---------------------------------------------------------------- provenance
def test_sourced_requires_reason_when_unavailable() -> None:
    with pytest.raises(ValidationError):
        Sourced[float](value=None, provenance=Provenance.UNAVAILABLE)
    ok = Sourced[float].unavailable("stream not started")
    assert ok.value is None


def test_sourced_rejects_value_without_provenance_meaning() -> None:
    with pytest.raises(ValidationError):
        Sourced[float](value=None, provenance=Provenance.LIVE)
    with pytest.raises(ValidationError):
        Sourced[float](value=1.0, provenance=Provenance.STALE)  # STALE needs as_of_ms
    assert Sourced[float](value=1.0, provenance=Provenance.STALE, as_of_ms=T0).value == 1.0


# ---------------------------------------------------------------- bars
def _bar(**overrides: object) -> Bar:
    base: dict[str, object] = dict(
        market=Market.SPOT,
        symbol="BTCUSDT",
        interval=Interval.M1,
        open_time_ms=T0,
        close_time_ms=T0 + 59_999,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=10.0,
        quote_volume=1000.0,
        trades=42,
        taker_buy_volume=4.0,
        taker_buy_quote_volume=400.0,
        source=BarSource.WS_LIVE,
        ingested_at_ms=T0 + 60_100,
    )
    base.update(overrides)
    return Bar.model_validate(base)


def test_valid_bar() -> None:
    assert _bar().close == 100.5


@pytest.mark.parametrize(
    "overrides",
    [
        {"open_time_ms": T0 + 1, "close_time_ms": T0 + 60_000},  # misaligned
        {"close_time_ms": T0 + 60_000},  # wrong close convention
        {"high": 100.0},  # high below close
        {"low": 100.6},  # low above open
        {"open": float("nan")},
        {"taker_buy_volume": 11.0},
        {"volume": -1.0},
    ],
)
def test_invalid_bars_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _bar(**overrides)


def test_interval_ms() -> None:
    assert Interval.M15.ms == 900_000
    assert Interval.H4.ms == 14_400_000


# ---------------------------------------------------------------- signals
def _signal(**overrides: object) -> SignalRecord:
    base: dict[str, object] = dict(
        signal_id=new_ulid(T0, randomness=1),
        run_mode=RunMode.LIVE,
        market=Market.USDM,
        symbol="ETHUSDT",
        timeframe=Interval.M15,
        strategy_id="h1_compression_breakout",
        strategy_version="1.0.0",
        params_hash="abc",
        prereg_id="H1-v1",
        evidence_grade="C",
        side=Side.LONG,
        stage=SignalStatus.CONFIRMED,
        decision_time_ms=T0 + 899_999,
        created_at_ms=T0 + 900_120,
        snapshot_id="snap-1",
        entry_conditions=(
            Condition(
                name="atr_pct_rank", operator="<", threshold=0.2, observed=0.1, satisfied=True
            ),
        ),
        entry_rule=EntryRule(kind=EntryRuleKind.MARKET_NEXT_OPEN, valid_until_ms=T0 + 1_800_000),
        ref_price=100.0,
        stop_price=98.0,
        stop_basis="1.5 ATR below range low",
        take_profit_price=102.0,
        exit_policy="bracket 1R/1R, time exit 16 bars",
        rr_gross=1.0,
        rr_net=0.9,
        est_cost_bps=12.0,
        break_even_win_rate=0.53,
        rationale="range break after compression with volume z=2.4",
        market_conditions={"regime": "range", "spread_bps": 1.2},
    )
    base.update(overrides)
    return SignalRecord.model_validate(base)


def test_valid_signal() -> None:
    assert _signal().stage is SignalStatus.CONFIRMED


@pytest.mark.parametrize(
    "overrides",
    [
        {"stop_price": 101.0},  # long stop above price
        {"take_profit_price": 99.0},  # long TP below price
        {"created_at_ms": T0},  # persisted before decision time
        {"stage": SignalStatus.OPEN},  # must be created as WATCH/CONFIRMED
        {"break_even_win_rate": 1.2},
    ],
)
def test_invalid_signals_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _signal(**overrides)


def test_short_signal_geometry() -> None:
    sig = _signal(side=Side.SHORT, stop_price=102.0, take_profit_price=98.0)
    assert sig.side is Side.SHORT


def test_entry_rule_price_rules() -> None:
    with pytest.raises(ValidationError):
        EntryRule(kind=EntryRuleKind.LIMIT, valid_until_ms=T0)
    with pytest.raises(ValidationError):
        EntryRule(kind=EntryRuleKind.MARKET_NEXT_OPEN, price=1.0, valid_until_ms=T0)


def test_lifecycle_transitions() -> None:
    assert can_transition(SignalStatus.WATCH, SignalStatus.CONFIRMED)
    assert can_transition(SignalStatus.CONFIRMED, SignalStatus.VETOED)
    assert not can_transition(SignalStatus.CLOSED, SignalStatus.OPEN)
    assert not can_transition(
        SignalStatus.WATCH, SignalStatus.OPEN
    )  # no entry without confirmation
