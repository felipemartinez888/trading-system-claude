"""Market data contracts: markets, intervals, bar provenance and the canonical bar record."""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tsclaude.contracts.timeutil import MS_PER_HOUR, MS_PER_MINUTE


class Market(StrEnum):
    SPOT = "spot"
    USDM = "usdm"  # Binance USDⓈ-M perpetual futures


class Interval(StrEnum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"

    @property
    def ms(self) -> int:
        return _INTERVAL_MS[self]


_INTERVAL_MS: dict[Interval, int] = {
    Interval.M1: MS_PER_MINUTE,
    Interval.M5: 5 * MS_PER_MINUTE,
    Interval.M15: 15 * MS_PER_MINUTE,
    Interval.H1: MS_PER_HOUR,
    Interval.H4: 4 * MS_PER_HOUR,
}


class BarSource(StrEnum):
    ARCHIVE = "archive"  # data.binance.vision bulk files
    REST_BACKFILL = "rest_backfill"  # REST history for periods never observed live
    WS_LIVE = "ws_live"  # closed kline received on a WebSocket during a session
    REST_RECOVERED = "rest_recovered"  # REST fetch for a bar we should have seen live (late/missed)
    DERIVED = "derived"  # aggregated from canonical 1m bars


class Bar(BaseModel):
    """A *closed* OHLCV bar. Partial bars never use this type.

    `close_time_ms` follows Binance's convention: open_time + interval - 1 ms.
    """

    model_config = ConfigDict(frozen=True)

    market: Market
    symbol: str = Field(min_length=1)
    interval: Interval
    open_time_ms: int = Field(ge=0)
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float = Field(ge=0)
    quote_volume: float = Field(ge=0)
    trades: int = Field(ge=0)
    taker_buy_volume: float = Field(ge=0)
    taker_buy_quote_volume: float = Field(ge=0)
    source: BarSource
    ingested_at_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _check(self) -> Self:
        step = self.interval.ms
        if self.open_time_ms % step != 0:
            raise ValueError("open_time_ms is not aligned to the interval (UTC)")
        if self.close_time_ms != self.open_time_ms + step - 1:
            raise ValueError("close_time_ms must equal open_time_ms + interval - 1")
        prices = (self.open, self.high, self.low, self.close)
        if any(not math.isfinite(p) or p <= 0 for p in prices):
            raise ValueError("prices must be finite and positive")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high below open/close/low")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low above open/close/high")
        if self.taker_buy_volume > self.volume * (1 + 1e-9):
            raise ValueError("taker buy volume exceeds volume")
        return self
