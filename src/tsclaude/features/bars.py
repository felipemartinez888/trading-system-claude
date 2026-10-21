"""Derived strategy bars (5m / 15m / 1h) from canonical closed 1m bars, UTC-aligned (D-04).

A derived bar covers ``[k * I, (k + 1) * I)`` in UTC epoch time. It is emitted **only** when all
``I / 1m`` minutes of its bucket are present and the bucket's last minute has closed, i.e. at the
1m close ``(k + 1) * I - 1``. A bucket with a missing minute is never emitted (fail closed: no
interpolation, no partial bars); it is counted in ``BarAggregator.incomplete``, and so is every
bucket that received no minute at all between two observed buckets. Consumers treat an increase
of ``incomplete`` as a **break** in the strategy-bar history (no feature window spans it).

OHLCV: open of the first minute, max high, min low, close of the last minute, sums of volumes and
trade counts. ``source = DERIVED``; ``ingested_at_ms`` is the latest ingestion time of the parts,
or the bucket close + 1 ms, whichever is later (a derived bar cannot be known before its close).

The streaming `BarAggregator` is the single implementation; `derive_bars` is a batch wrapper, so
backtests and live use identical aggregation.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from tsclaude.contracts.market import Bar, BarSource, Interval
from tsclaude.contracts.timeutil import MS_PER_MINUTE

STRATEGY_INTERVALS = (Interval.M5, Interval.M15, Interval.H1)

#: Kept as an alias so callers can name what they handle; derived bars are contract `Bar`s.
type DerivedBar = Bar


class BarAggregator:
    """Streaming 1m -> interval aggregation for one (market, symbol)."""

    def __init__(self, interval: Interval) -> None:
        if interval is Interval.M1 or interval.ms % MS_PER_MINUTE != 0:
            raise ValueError(f"cannot derive {interval} bars from 1m bars")
        self.interval = interval
        self.minutes = interval.ms // MS_PER_MINUTE
        self._bucket: int | None = None
        self._parts: list[Bar] = []
        self._last_open: int | None = None
        self._last_bucket: int | None = None  # bucket of the previous 1m bar
        self.incomplete = 0  # buckets dropped: a minute missing, or the whole bucket empty

    def push(self, bar: Bar) -> Bar | None:
        """Add the next closed 1m bar; return the derived bar if this minute completes it."""
        if bar.interval is not Interval.M1:
            raise ValueError("BarAggregator consumes 1m bars only")
        if self._last_open is not None and bar.open_time_ms <= self._last_open:
            raise ValueError("1m bars must arrive in strictly increasing open time")
        first = self._parts[0] if self._parts else None
        if first is not None and (bar.market, bar.symbol) != (first.market, first.symbol):
            raise ValueError("one aggregator per (market, symbol)")
        self._last_open = bar.open_time_ms
        step = self.interval.ms
        bucket = bar.open_time_ms - bar.open_time_ms % step
        if self._last_bucket is not None and bucket - self._last_bucket > step:
            # buckets in between received no minute at all: they are incomplete too
            self.incomplete += (bucket - self._last_bucket) // step - 1
        self._last_bucket = bucket
        if bucket != self._bucket:
            if self._parts:
                self.incomplete += 1  # previous bucket never completed
            self._bucket = bucket
            self._parts = []
        self._parts.append(bar)
        if bar.close_time_ms != bucket + step - 1:
            return None
        parts, self._parts, self._bucket = self._parts, [], None
        if len(parts) != self.minutes:
            self.incomplete += 1
            return None
        return _combine(parts, self.interval, bucket)


def _combine(parts: Sequence[Bar], interval: Interval, bucket: int) -> Bar:
    first, last = parts[0], parts[-1]
    close_ms = bucket + interval.ms - 1
    return Bar(
        market=first.market,
        symbol=first.symbol,
        interval=interval,
        open_time_ms=bucket,
        close_time_ms=close_ms,
        open=first.open,
        high=max(p.high for p in parts),
        low=min(p.low for p in parts),
        close=last.close,
        volume=sum(p.volume for p in parts),
        quote_volume=sum(p.quote_volume for p in parts),
        trades=sum(p.trades for p in parts),
        taker_buy_volume=sum(p.taker_buy_volume for p in parts),
        taker_buy_quote_volume=sum(p.taker_buy_quote_volume for p in parts),
        source=BarSource.DERIVED,
        ingested_at_ms=max(close_ms + 1, max(p.ingested_at_ms for p in parts)),
    )


def derive_bars(bars_1m: Iterable[Bar], interval: Interval) -> list[Bar]:
    """Batch aggregation of one sorted 1m series (complete buckets only)."""
    agg = BarAggregator(interval)
    out: list[Bar] = []
    for b in bars_1m:
        derived = agg.push(b)
        if derived is not None:
            out.append(derived)
    return out
