"""Internal messages -> canonical `Bar` (closed only), and bar <-> plain dict for event payloads."""

from __future__ import annotations

from typing import Any

from tsclaude.contracts.market import Bar, BarSource, Interval, Market
from tsclaude.marketdata.types import KlineUpdate, RestKline


def bar_from_ws(k: KlineUpdate, ingested_at_ms: int) -> Bar:
    """A *closed* WS kline -> Bar(ws_live). Raises ValueError for forming klines or invalid data."""
    if not k.is_closed:
        raise ValueError("a forming kline is not a bar")
    return Bar(
        market=k.market,
        symbol=k.symbol,
        interval=k.interval,
        open_time_ms=k.open_time_ms,
        close_time_ms=k.close_time_ms,
        open=k.open,
        high=k.high,
        low=k.low,
        close=k.close,
        volume=k.volume,
        quote_volume=k.quote_volume,
        trades=k.trades,
        taker_buy_volume=k.taker_buy_volume,
        taker_buy_quote_volume=k.taker_buy_quote_volume,
        source=BarSource.WS_LIVE,
        ingested_at_ms=ingested_at_ms,
    )


def bar_from_rest(
    market: Market, symbol: str, k: RestKline, source: BarSource, ingested_at_ms: int
) -> Bar:
    if source not in (BarSource.REST_BACKFILL, BarSource.REST_RECOVERED):
        raise ValueError(f"REST bars are rest_backfill or rest_recovered, not {source}")
    return Bar(
        market=market,
        symbol=symbol,
        interval=Interval.M1,
        open_time_ms=k.open_time_ms,
        close_time_ms=k.close_time_ms,
        open=k.open,
        high=k.high,
        low=k.low,
        close=k.close,
        volume=k.volume,
        quote_volume=k.quote_volume,
        trades=k.trades,
        taker_buy_volume=k.taker_buy_volume,
        taker_buy_quote_volume=k.taker_buy_quote_volume,
        source=source,
        ingested_at_ms=ingested_at_ms,
    )


def bar_payload(bar: Bar) -> dict[str, Any]:
    """JSON-safe dict of a bar for event payloads (enums as strings)."""
    return bar.model_dump(mode="json")
