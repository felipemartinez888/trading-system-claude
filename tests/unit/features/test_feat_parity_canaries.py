"""Window parity (ADR-0006) and look-ahead canaries for every feature, plus derived bars."""

from __future__ import annotations

import math
import random

import numpy as np
import pytest

from tsclaude.contracts.market import Bar, BarSource, Interval, Market
from tsclaude.contracts.timeutil import MS_PER_MINUTE
from tsclaude.features import (
    BarAggregator,
    BarArrays,
    Feature,
    atr_feature,
    atr_rank_feature,
    derive_bars,
    ema_feature,
    range_high_feature,
    range_low_feature,
    realized_vol_feature,
    volume_z_feature,
)

T0 = 1_640_995_200_000  # 2022-01-01T00:00:00Z


def _bars(n: int, seed: int = 7, interval: Interval = Interval.M15) -> list[Bar]:
    rng = random.Random(seed)
    out = []
    price = 100.0
    for i in range(n):
        o = price
        c = max(1.0, o * math.exp(rng.gauss(0.0, 0.01)))
        h = max(o, c) * (1 + abs(rng.gauss(0.0, 0.003)))
        lo = min(o, c) * (1 - abs(rng.gauss(0.0, 0.003)))
        price = c
        t = T0 + i * interval.ms
        v = rng.uniform(1.0, 10.0)
        out.append(
            Bar(
                market=Market.SPOT,
                symbol="BTCUSDT",
                interval=interval,
                open_time_ms=t,
                close_time_ms=t + interval.ms - 1,
                open=o,
                high=h,
                low=lo,
                close=c,
                volume=v,
                quote_volume=v * c,
                trades=1,
                taker_buy_volume=v / 2,
                taker_buy_quote_volume=v * c / 2,
                source=BarSource.DERIVED,
                ingested_at_ms=t + interval.ms,
            )
        )
    return out


FEATURES: list[Feature] = [
    atr_feature(14),
    atr_rank_feature(14, 50),
    range_high_feature(20),
    range_low_feature(20),
    ema_feature(20),
    volume_z_feature(20),
    realized_vol_feature(20),
]


@pytest.mark.parametrize("feature", FEATURES, ids=lambda f: f.name)
def test_window_parity_full_history_equals_trailing_window(feature: Feature) -> None:
    arrays = BarArrays.from_bars(_bars(feature.warmup + 300))
    full = feature(arrays)
    window = feature.warmup + 5  # live recomputation: warm-up + margin
    checked = 0
    for i in range(feature.warmup - 1, len(arrays)):
        trailing = arrays.head(i + 1).tail(window)
        live = feature.at_last(trailing)
        if len(trailing) < feature.warmup:
            assert math.isnan(live)
            continue
        assert not math.isnan(full[i]), (feature.name, i)
        assert live == pytest.approx(float(full[i]), rel=1e-9, abs=1e-12), (feature.name, i)
        checked += 1
    assert checked >= 300
    # declared warm-up is exact: before it, values are NaN in both evaluations
    assert np.all(np.isnan(full[: feature.warmup - 1]))


@pytest.mark.parametrize("feature", FEATURES, ids=lambda f: f.name)
def test_lookahead_canary_mutating_future_never_changes_past(feature: Feature) -> None:
    bars = _bars(feature.warmup + 120)
    base = feature(BarArrays.from_bars(bars))
    cut = feature.warmup + 60
    rng = random.Random(99)
    mutated = list(bars[:cut])
    for b in bars[cut:]:  # wildly different future: x10 / x0.1 prices, huge volumes
        f = rng.choice([10.0, 0.1])
        mutated.append(
            b.model_copy(
                update={
                    "open": b.open * f,
                    "high": b.high * f * 1.5,
                    "low": b.low * f * 0.5,
                    "close": b.close * f,
                    "volume": b.volume * 1000,
                }
            )
        )
    after = feature(BarArrays.from_bars(mutated))
    np.testing.assert_array_equal(base[:cut], after[:cut])
    assert not np.array_equal(base[cut:], after[cut:], equal_nan=True)


def test_feature_values_hand_checked() -> None:
    bars = _bars(30)
    a = BarArrays.from_bars(bars)
    tr = [max(a.high[i], a.close[i - 1]) - min(a.low[i], a.close[i - 1]) for i in range(1, 30)]
    atr3 = atr_feature(3)(a)
    assert atr3[3] == pytest.approx(sum(tr[0:3]) / 3)
    assert math.isnan(atr3[2])
    rh = range_high_feature(5)(a)
    assert rh[10] == max(a.high[6:11])
    vz = volume_z_feature(5)(a)
    w = a.volume[6:11]
    assert vz[10] == pytest.approx((w[-1] - w.mean()) / w.std())
    rv = realized_vol_feature(4)(a)
    lr = np.log(a.close[7:11] / a.close[6:10])
    assert rv[10] == pytest.approx(float(np.std(lr, ddof=1)))


def _minutes(n: int, start: int, skip: set[int] = frozenset()) -> list[Bar]:  # type: ignore[assignment]
    out = []
    for i in range(n):
        if i in skip:
            continue
        t = start + i * MS_PER_MINUTE
        out.append(
            Bar(
                market=Market.SPOT,
                symbol="ETHUSDT",
                interval=Interval.M1,
                open_time_ms=t,
                close_time_ms=t + MS_PER_MINUTE - 1,
                open=100 + i,
                high=101 + i,
                low=99 + i,
                close=100.5 + i,
                volume=1.0,
                quote_volume=100.0,
                trades=2,
                taker_buy_volume=0.5,
                taker_buy_quote_volume=50.0,
                source=BarSource.ARCHIVE,
                ingested_at_ms=t + MS_PER_MINUTE,
            )
        )
    return out


def test_derived_bars_utc_aligned_complete_buckets_only() -> None:
    start = T0 + 3 * MS_PER_MINUTE  # starts mid-bucket: first 15m bucket is incomplete
    m1 = _minutes(60, start, skip={20})
    agg = BarAggregator(Interval.M15)
    out = [d for b in m1 if (d := agg.push(b)) is not None]
    assert out == derive_bars(m1, Interval.M15)
    opens = [b.open_time_ms for b in out]
    # buckets at T0+0 (starts at minute 3: incomplete), T0+15 (minute 20 missing: dropped),
    # T0+30 and T0+45 complete
    assert opens == [T0 + 30 * MS_PER_MINUTE, T0 + 45 * MS_PER_MINUTE]
    assert agg.incomplete == 2
    b = out[0]
    first = next(x for x in m1 if x.open_time_ms == b.open_time_ms)
    assert b.open == first.open
    assert b.close_time_ms == b.open_time_ms + 15 * MS_PER_MINUTE - 1
    assert b.volume == pytest.approx(15.0)
    assert b.source is BarSource.DERIVED
    assert b.ingested_at_ms > b.close_time_ms


@pytest.mark.parametrize("interval", [Interval.M5, Interval.M15, Interval.H1])
def test_derived_bar_emitted_exactly_at_its_close(interval: Interval) -> None:
    m1 = _minutes(180, T0)
    agg = BarAggregator(interval)
    for b in m1:
        d = agg.push(b)
        if d is not None:
            assert d.close_time_ms == b.close_time_ms  # never before its last minute closed
            assert d.open_time_ms % interval.ms == 0


def test_aggregator_rejects_unsorted_input() -> None:
    m1 = _minutes(3, T0)
    agg = BarAggregator(Interval.M5)
    agg.push(m1[1])
    with pytest.raises(ValueError, match="increasing"):
        agg.push(m1[0])


def test_empty_buckets_count_as_incomplete() -> None:
    m1 = _minutes(90, T0, skip=set(range(20, 50)))  # minutes 20..49 missing: bucket 30 empty
    agg = BarAggregator(Interval.M15)
    out = [d for b in m1 if (d := agg.push(b)) is not None]
    assert [b.open_time_ms for b in out] == [T0, T0 + 60 * MS_PER_MINUTE, T0 + 75 * MS_PER_MINUTE]
    assert agg.incomplete == 3  # bucket 15 (partial), 30 (empty), 45 (partial)
