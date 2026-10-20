"""Derive higher-timeframe bars from canonical 1m bars (ADR-0004).

A derived bar exists only if **every** 1m bar of its UTC-aligned bucket is present; incomplete
buckets are skipped, never filled. open = first open, close = last close, high/low = extremes,
volumes and trade counts are sums. `source = derived`.
"""

from __future__ import annotations

from collections.abc import Iterable

from tsclaude.contracts.market import Bar, BarSource, Interval
from tsclaude.contracts.timeutil import MS_PER_MINUTE


def aggregate(bars: Iterable[Bar], interval: Interval) -> tuple[list[Bar], int]:
    """1m bars (one market/symbol, sorted, unique) -> (complete derived bars, skipped buckets)."""
    step = interval.ms
    need = step // MS_PER_MINUTE
    buckets: dict[int, list[Bar]] = {}
    for b in bars:
        if b.interval is not Interval.M1:
            raise ValueError("aggregate() takes 1m bars")
        buckets.setdefault(b.open_time_ms - b.open_time_ms % step, []).append(b)
    out: list[Bar] = []
    skipped = 0
    for start in sorted(buckets):
        group = buckets[start]
        if len(group) != need:
            skipped += 1
            continue
        first, last = group[0], group[-1]
        out.append(
            Bar(
                market=first.market,
                symbol=first.symbol,
                interval=interval,
                open_time_ms=start,
                close_time_ms=start + step - 1,
                open=first.open,
                high=max(b.high for b in group),
                low=min(b.low for b in group),
                close=last.close,
                volume=sum(b.volume for b in group),
                quote_volume=sum(b.quote_volume for b in group),
                trades=sum(b.trades for b in group),
                taker_buy_volume=sum(b.taker_buy_volume for b in group),
                taker_buy_quote_volume=sum(b.taker_buy_quote_volume for b in group),
                source=BarSource.DERIVED,
                ingested_at_ms=max(b.ingested_at_ms for b in group),
            )
        )
    return out, skipped
