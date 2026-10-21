"""Binance public REST client (spot and USD-M) on `httpx.AsyncClient` with an injectable transport.

Paper-only: only the unsigned market-data paths in `_ALLOWED_PATHS` can be requested; anything
else raises before a request is built (the CI guard additionally greps for private endpoints).

Rate limits:
- request *weights* per endpoint are Binance's documented costs (`_weight_*`); the *limits* are
  read from `exchangeInfo.rateLimits` (`load_exchange_info` seeds the limiter) and the used weight
  is corrected from `X-MBX-USED-WEIGHT-*` headers after every response;
- until exchangeInfo has been read, a conservative request-rate cap applies
  (`rest.unseeded_requests_per_s`);
- HTTP 429 -> wait `Retry-After` (seconds) and retry, up to `rest.max_retries`, then raise
  `RestRateLimited`;
- HTTP 418 -> stop all REST for this API family and raise `RestBanned` (also for every later call
  until `Retry-After` has elapsed, or until restart if none was given).

Spot falls back to `data-api.binance.vision` on connection errors and 5xx responses (never on
429/418: those are IP-level states).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

import httpx

from tsclaude.contracts.market import Market
from tsclaude.contracts.timeutil import MS_PER_MINUTE, WallClock
from tsclaude.marketdata.binance import parse
from tsclaude.marketdata.binance.limits import (
    RestBannedState,
    Sleep,
    SlidingWindowLimiter,
    WeightLimiter,
)
from tsclaude.marketdata.config import RestConfig
from tsclaude.marketdata.types import (
    ExchangeInfo,
    FundingRate,
    PremiumIndex,
    PriceKline,
    RestKline,
)

log = logging.getLogger(__name__)

KLINES_MAX_LIMIT = 1000  # documented maximum for spot and USD-M klines
FUNDING_MAX_LIMIT = 1000

_ALLOWED_PATHS: dict[Market, frozenset[str]] = {
    Market.SPOT: frozenset({"/api/v3/time", "/api/v3/exchangeInfo", "/api/v3/klines"}),
    Market.USDM: frozenset(
        {
            "/fapi/v1/time",
            "/fapi/v1/exchangeInfo",
            "/fapi/v1/klines",
            "/fapi/v1/premiumIndex",
            "/fapi/v1/fundingRate",
            "/fapi/v1/markPriceKlines",
        }
    ),
}


class RestError(RuntimeError):
    """Any REST failure that the caller must treat as 'data not obtained'."""

    def __init__(self, message: str, *, status: int | None = None, path: str | None = None):
        super().__init__(message)
        self.status = status
        self.path = path


class RestBanned(RestError):
    """HTTP 418: the IP is banned. REST is stopped; this is a health alarm, not a retry case."""

    def __init__(self, message: str, *, until_ms: int | None, path: str | None = None) -> None:
        super().__init__(message, status=418, path=path)
        self.until_ms = until_ms


class RestRateLimited(RestError):
    """HTTP 429 persisted after the configured retries."""


class _HasOpenTime(Protocol):
    @property
    def open_time_ms(self) -> int: ...


def _kline_weight_usdm(limit: int) -> int:
    # USD-M klines / markPriceKlines: weight by `limit` (documented table).
    if limit < 100:
        return 1
    if limit < 500:
        return 2
    if limit <= 1000:
        return 5
    return 10


def _retry_after_ms(headers: Mapping[str, str]) -> int | None:
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0, int(float(raw) * 1000))
    except ValueError:
        return None


class BinanceRest:
    """REST client for one API family (spot or USD-M); each has its own IP weight budget."""

    def __init__(
        self,
        market: Market,
        base_urls: tuple[str, ...],
        clock: WallClock,
        config: RestConfig | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if not base_urls:
            raise ValueError("at least one base URL is required")
        self.market = market
        self._bases = base_urls
        self._base_index = 0
        self._clock = clock
        self._config = config or RestConfig()
        self._sleep = sleep
        self.weights = WeightLimiter(clock, sleep, self._config.weight_budget_fraction)
        self._unseeded = SlidingWindowLimiter(
            self._config.unseeded_requests_per_s, 1_000, clock, sleep
        )
        self._funding = SlidingWindowLimiter(
            self._config.funding_rate_requests_per_5min, 5 * MS_PER_MINUTE, clock, sleep
        )
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=self._config.timeout_s,
            headers={"Accept": "application/json", "User-Agent": "tsclaude/0 (paper-only)"},
        )
        self.exchange_info: ExchangeInfo | None = None
        self.requests = 0
        self.fallbacks = 0
        self.rate_limited = 0

    @property
    def _prefix(self) -> str:
        return "/api/v3" if self.market is Market.SPOT else "/fapi/v1"

    @property
    def base_url(self) -> str:
        return self._bases[self._base_index]

    @property
    def banned(self) -> bool:
        return self.weights.banned

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> BinanceRest:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ core request
    async def _get(self, path: str, params: Mapping[str, Any], weight: int) -> Any:
        if path not in _ALLOWED_PATHS[self.market]:
            raise RestError(f"path {path!r} is not an allowed public endpoint", path=path)
        attempts_429 = 0
        attempts_net = 0
        while True:
            try:
                self.weights.check_banned()
            except RestBannedState as exc:
                raise RestBanned(str(exc), until_ms=exc.until_ms, path=path) from None
            if not self.weights.seeded:
                await self._unseeded.acquire()
            try:
                await self.weights.acquire(weight)
            except RestBannedState as exc:
                raise RestBanned(str(exc), until_ms=exc.until_ms, path=path) from None
            url = f"{self.base_url}{path}"
            self.requests += 1
            try:
                resp = await self._client.get(url, params=dict(params))
            except httpx.TransportError as exc:
                attempts_net += 1
                if self._next_base():
                    log.warning(
                        "REST transport error, falling back",
                        extra={"url": url, "error": repr(exc), "next": self.base_url},
                    )
                    continue
                if attempts_net > self._config.max_retries:
                    raise RestError(f"transport error: {exc!r}", path=path) from exc
                await self._sleep(min(2.0**attempts_net, 10.0))
                continue
            self.weights.observe_headers(resp.headers)
            status = resp.status_code
            if status == 418:
                retry = _retry_after_ms(resp.headers)
                self.weights.ban(retry)
                until = None if retry is None else self._clock.now_ms() + retry
                log.error(
                    "REST banned (HTTP 418); REST stopped",
                    extra={"path": path, "retry_after_ms": retry},
                )
                raise RestBanned("HTTP 418: IP banned; REST stopped", until_ms=until, path=path)
            if status == 429:
                self.rate_limited += 1
                attempts_429 += 1
                retry = _retry_after_ms(resp.headers)
                wait = retry if retry is not None else self.weights.next_window_ms()
                self.weights.block_for(wait)
                log.warning(
                    "REST rate limited (HTTP 429)",
                    extra={"path": path, "retry_after_ms": wait, "attempt": attempts_429},
                )
                if attempts_429 > self._config.max_retries:
                    raise RestRateLimited("HTTP 429 persisted", status=429, path=path)
                continue
            if status >= 500:
                attempts_net += 1
                if self._next_base():
                    log.warning(
                        "REST server error, falling back",
                        extra={"url": url, "status": status, "next": self.base_url},
                    )
                    continue
                if attempts_net > self._config.max_retries:
                    raise RestError(f"HTTP {status}", status=status, path=path)
                await self._sleep(min(2.0**attempts_net, 10.0))
                continue
            if status != 200:
                raise RestError(f"HTTP {status}: {resp.text[:200]}", status=status, path=path)
            try:
                return resp.json()
            except ValueError as exc:
                raise RestError("response is not JSON", status=status, path=path) from exc

    def _next_base(self) -> bool:
        if len(self._bases) == 1:
            return False
        self._base_index = (self._base_index + 1) % len(self._bases)
        self.fallbacks += 1
        return self._base_index != 0

    # ------------------------------------------------------------------ endpoints
    async def server_time(self) -> int:
        payload = await self._get(f"{self._prefix}/time", {}, 1)
        return self._parse(parse.parse_server_time, payload)

    async def load_exchange_info(self, symbols: tuple[str, ...] | None = None) -> ExchangeInfo:
        """Fetch exchangeInfo and seed the weight limiter from its `rateLimits`."""
        return (await self.load_exchange_info_raw(symbols))[1]

    async def load_exchange_info_raw(
        self, symbols: tuple[str, ...] | None = None
    ) -> tuple[Any, ExchangeInfo]:
        """Like `load_exchange_info`, also returning the raw JSON payload (kept as evidence)."""
        params: dict[str, Any] = {}
        if self.market is Market.SPOT:
            weight = 20
            if symbols:
                params["symbols"] = "[" + ",".join(f'"{s}"' for s in symbols) + "]"
        else:
            weight = 1
        payload = await self._get(f"{self._prefix}/exchangeInfo", params, weight)
        info: ExchangeInfo = self._parse(
            lambda p: parse.parse_exchange_info(self.market, p), payload
        )
        self.weights.seed(info.rate_limits)
        self.exchange_info = info
        return payload, info

    def _kline_weight(self, limit: int) -> int:
        return 2 if self.market is Market.SPOT else _kline_weight_usdm(limit)

    async def klines(
        self, symbol: str, start_ms: int, end_ms: int, interval: str = "1m"
    ) -> list[RestKline]:
        """All klines with start_ms <= open_time < end_ms, paginated (<= 1000 per call)."""
        path = f"{self._prefix}/klines"
        return await self._paginate(
            path, symbol, start_ms, end_ms, interval, parse.parse_rest_klines
        )

    async def mark_price_klines(
        self, symbol: str, start_ms: int, end_ms: int, interval: str = "1m"
    ) -> list[PriceKline]:
        if self.market is not Market.USDM:
            raise RestError("markPriceKlines exists only on USD-M")
        return await self._paginate(
            "/fapi/v1/markPriceKlines", symbol, start_ms, end_ms, interval, parse.parse_price_klines
        )

    async def _paginate[K: _HasOpenTime](
        self,
        path: str,
        symbol: str,
        start_ms: int,
        end_ms: int,
        interval: str,
        parser: Callable[[Any], Sequence[K]],
    ) -> list[K]:
        out: list[K] = []
        cursor = start_ms
        while cursor < end_ms:
            if interval == "1m":
                remaining = (end_ms - cursor + MS_PER_MINUTE - 1) // MS_PER_MINUTE
                limit = max(1, min(KLINES_MAX_LIMIT, remaining))
            else:
                limit = KLINES_MAX_LIMIT
            params = {
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms - 1,  # Binance treats endTime as inclusive
                "limit": limit,
            }
            payload = await self._get(path, params, self._kline_weight(limit))
            page: Sequence[K] = self._parse(parser, payload)
            rows = [r for r in page if start_ms <= r.open_time_ms < end_ms]
            if not rows:
                break
            out.extend(r for r in rows if r.open_time_ms >= cursor)
            # 1m: the next possible open time; other intervals: just past the last row.
            nxt = rows[-1].open_time_ms + (MS_PER_MINUTE if interval == "1m" else 1)
            if nxt <= cursor:
                break  # defensive: no progress
            cursor = nxt
            if len(page) < limit:
                break
        return out

    async def premium_index(self, symbol: str) -> PremiumIndex:
        if self.market is not Market.USDM:
            raise RestError("premiumIndex exists only on USD-M")
        payload = await self._get("/fapi/v1/premiumIndex", {"symbol": symbol}, 1)
        return self._parse(parse.parse_premium_index, payload)

    async def funding_rates(
        self,
        symbol: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = FUNDING_MAX_LIMIT,
    ) -> list[FundingRate]:
        """Funding rate history. Shares a separate documented 500 / 5 min / IP budget."""
        if self.market is not Market.USDM:
            raise RestError("fundingRate exists only on USD-M")
        params: dict[str, Any] = {"symbol": symbol, "limit": min(limit, FUNDING_MAX_LIMIT)}
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms
        await self._funding.acquire()
        payload = await self._get("/fapi/v1/fundingRate", params, 1)
        return self._parse(parse.parse_funding_rates, payload)

    @staticmethod
    def _parse[T](fn: Callable[[Any], T], payload: Any) -> T:
        try:
            return fn(payload)
        except parse.MalformedMessage as exc:
            raise RestError(f"malformed response: {exc.reason}") from exc
