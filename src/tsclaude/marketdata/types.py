"""Internal typed messages and identifiers shared by the market-data and quality packages.

These are *internal* to ingestion. Anything exposed by the API goes through `models.py` and is
wrapped in `contracts.provenance.Sourced`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from tsclaude.contracts.events import NewEvent
from tsclaude.contracts.market import Interval, Market

type EventSink = Callable[[NewEvent], object]
"""Where producers submit events (normally `EventStore.append`, wrapped by the service)."""


class StreamKind(StrEnum):
    KLINE_1M = "kline_1m"
    BOOK_TICKER = "book_ticker"
    MARK_PRICE = "mark_price"


@dataclass(frozen=True, slots=True)
class StreamKey:
    market: Market
    symbol: str
    kind: StreamKind

    @property
    def id(self) -> str:
        return f"{self.market}:{self.symbol}:{self.kind}"


def symbol_id(market: Market, symbol: str) -> str:
    """Aggregate id used for per-(market, symbol) market-data events."""
    return f"{market}:{symbol}"


@dataclass(frozen=True, slots=True)
class KlineUpdate:
    """One kline WebSocket event (forming or closed). Timestamps are epoch ms."""

    market: Market
    symbol: str
    interval: Interval
    event_time_ms: int
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    trades: int
    taker_buy_volume: float
    taker_buy_quote_volume: float
    is_closed: bool


@dataclass(frozen=True, slots=True)
class BookTickerUpdate:
    """Best bid/ask. Spot payloads carry no event time (`event_time_ms` is then None)."""

    market: Market
    symbol: str
    update_id: int
    bid_price: float
    bid_qty: float
    ask_price: float
    ask_qty: float
    event_time_ms: int | None
    transaction_time_ms: int | None


@dataclass(frozen=True, slots=True)
class MarkPriceUpdate:
    """USD-M `markPriceUpdate` event."""

    market: Market
    symbol: str
    event_time_ms: int
    mark_price: float
    index_price: float
    estimated_settle_price: float
    funding_rate: float | None  # Binance sends "" for non-perpetual contracts
    next_funding_time_ms: int | None  # 0 / absent for non-perpetual contracts


@dataclass(frozen=True, slots=True)
class ControlResponse:
    """Reply to a SUBSCRIBE/UNSUBSCRIBE/LIST_SUBSCRIPTIONS control message."""

    id: int | str | None
    result: object
    error: object | None


type ParsedMessage = KlineUpdate | BookTickerUpdate | MarkPriceUpdate | ControlResponse


@dataclass(frozen=True, slots=True)
class RestKline:
    """One row of a REST klines response (closed or still forming — the caller decides)."""

    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time_ms: int
    quote_volume: float
    trades: int
    taker_buy_volume: float
    taker_buy_quote_volume: float


@dataclass(frozen=True, slots=True)
class PriceKline:
    """One row of `markPriceKlines` (prices only; Binance marks the other columns 'ignore')."""

    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    close_time_ms: int


@dataclass(frozen=True, slots=True)
class PremiumIndex:
    symbol: str
    mark_price: float
    index_price: float
    estimated_settle_price: float
    last_funding_rate: float
    interest_rate: float
    next_funding_time_ms: int
    time_ms: int


@dataclass(frozen=True, slots=True)
class FundingRate:
    symbol: str
    funding_rate: float
    funding_time_ms: int
    mark_price: float | None


@dataclass(frozen=True, slots=True)
class RateLimit:
    """One `exchangeInfo.rateLimits` entry."""

    rate_limit_type: str  # REQUEST_WEIGHT, RAW_REQUESTS, ORDERS (ignored: paper only)
    interval: str  # SECOND, MINUTE, DAY
    interval_num: int
    limit: int

    @property
    def window_ms(self) -> int:
        unit = {"SECOND": 1_000, "MINUTE": 60_000, "HOUR": 3_600_000, "DAY": 86_400_000}
        return unit[self.interval] * self.interval_num

    @property
    def header_suffix(self) -> str:
        """Suffix of the matching `X-MBX-USED-WEIGHT-<n><unit>` header, e.g. `1M`."""
        return f"{self.interval_num}{self.interval[0]}"


@dataclass(frozen=True, slots=True)
class SymbolInfo:
    symbol: str
    status: str
    base_asset: str
    quote_asset: str
    tick_size: str | None
    step_size: str | None
    min_notional: str | None
    filters: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class ExchangeInfo:
    market: Market
    server_time_ms: int | None
    rate_limits: tuple[RateLimit, ...]
    symbols: dict[str, SymbolInfo]
