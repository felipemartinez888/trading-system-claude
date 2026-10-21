"""Causal features as pure windowed functions (ADR-0006) and derived strategy bars.

`bars` aggregates canonical 1m bars into UTC-aligned 5m/15m/1h bars (complete buckets only);
`core` holds the feature functions, each with a declared warm-up; `Feature.at_last` evaluates a
feature over a trailing window (the live/backtest signal path) and must equal the full-history
value (window-parity tests).
"""

from tsclaude.features.bars import BarAggregator, DerivedBar, derive_bars
from tsclaude.features.core import (
    FEATURES_VERSION,
    BarArrays,
    Feature,
    atr,
    atr_feature,
    atr_percentile_rank,
    atr_rank_feature,
    ema,
    ema_feature,
    ema_warmup,
    range_high,
    range_high_feature,
    range_low,
    range_low_feature,
    realized_vol,
    realized_vol_feature,
    true_range,
    volume_z_feature,
    volume_zscore,
)

__all__ = [
    "FEATURES_VERSION",
    "BarAggregator",
    "BarArrays",
    "DerivedBar",
    "Feature",
    "atr",
    "atr_feature",
    "atr_percentile_rank",
    "atr_rank_feature",
    "derive_bars",
    "ema",
    "ema_feature",
    "ema_warmup",
    "range_high",
    "range_high_feature",
    "range_low",
    "range_low_feature",
    "realized_vol",
    "realized_vol_feature",
    "true_range",
    "volume_z_feature",
    "volume_zscore",
]
