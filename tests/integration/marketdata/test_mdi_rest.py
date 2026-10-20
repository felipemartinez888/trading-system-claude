"""REST client against httpx.MockTransport: pagination, weight headers, 429, 418, fallback."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

httpx = pytest.importorskip("httpx")

from mdi_helpers import MIN, T0, FakeSleep, kline_row, load_fixture  # noqa: E402

from tsclaude.contracts.market import Market  # noqa: E402
from tsclaude.contracts.timeutil import ManualClock  # noqa: E402
from tsclaude.marketdata.binance.rest import (  # noqa: E402
    BinanceRest,
    RestBanned,
    RestError,
    RestRateLimited,
)
from tsclaude.marketdata.config import RestConfig  # noqa: E402

Handler = Callable[[Any], Any]


def _client(
    market: Market,
    handler: Handler,
    clock: ManualClock,
    bases: tuple[str, ...] | None = None,
    **cfg: Any,
) -> tuple[BinanceRest, FakeSleep]:
    sleep = FakeSleep(clock)
    if bases is None:
        bases = ("https://api.test",) if market is Market.SPOT else ("https://fapi.test",)
    rest = BinanceRest(
        market,
        bases,
        clock,
        RestConfig(**cfg),
        transport=httpx.MockTransport(handler),
        sleep=sleep,
    )
    return rest, sleep


def _klines_server(available: range, weight_header: str = "X-MBX-USED-WEIGHT-1M"):  # type: ignore[no-untyped-def]
    calls: list[dict[str, str]] = []
    used = {"w": 0}

    def handler(request: Any) -> Any:
        params = dict(request.url.params)
        calls.append(params)
        start, end, limit = int(params["startTime"]), int(params["endTime"]), int(params["limit"])
        rows = [kline_row(t) for t in available if start <= t <= end][:limit]
        used["w"] += 2
        return httpx.Response(200, json=rows, headers={weight_header: str(used["w"])})

    return handler, calls


def test_klines_paginate_at_1000_and_respect_bounds() -> None:
    clock = ManualClock(T0)
    available = range(T0, T0 + 2500 * MIN, MIN)
    handler, calls = _klines_server(available)
    rest, _ = _client(Market.SPOT, handler, clock)
    rows = asyncio.run(rest.klines("BTCUSDT", T0, T0 + 2500 * MIN))
    assert len(rows) == 2500
    assert [r.open_time_ms for r in rows] == list(available)
    assert [int(c["limit"]) for c in calls] == [1000, 1000, 500]
    assert calls[0]["startTime"] == str(T0)
    assert calls[1]["startTime"] == str(T0 + 1000 * MIN)
    assert calls[2]["startTime"] == str(T0 + 2000 * MIN)
    assert all(c["endTime"] == str(T0 + 2500 * MIN - 1) for c in calls)
    assert all(c["interval"] == "1m" for c in calls)
    assert rest.weights.windows["1M"][0] == 6  # from the header, not our own estimate


def test_klines_stops_when_exchange_returns_fewer() -> None:
    clock = ManualClock(T0)
    handler, calls = _klines_server(range(T0, T0 + 10 * MIN, MIN))
    rest, _ = _client(Market.USDM, handler, clock)
    rows = asyncio.run(rest.klines("BTCUSDT", T0, T0 + 100 * MIN))
    assert len(rows) == 10
    assert len(calls) == 1
    assert asyncio.run(rest.klines("BTCUSDT", T0 + 500 * MIN, T0 + 600 * MIN)) == []


def test_exchange_info_seeds_limiter_and_headers_throttle() -> None:
    clock = ManualClock(T0 + 1_000)
    seen: list[str] = []
    used = {"w": 1198}

    def handler(request: Any) -> Any:
        seen.append(request.url.path)
        if request.url.path == "/fapi/v1/exchangeInfo":
            return httpx.Response(
                200,
                json=load_fixture("rest_usdm_exchange_info.json"),
                headers={"X-MBX-USED-WEIGHT-1M": "1"},
            )
        # another client on the same IP pushed the minute's usage near our 50% budget (1200)
        used["w"] += 1
        return httpx.Response(
            200,
            json=load_fixture("rest_time.json"),
            headers={"X-MBX-USED-WEIGHT-1M": str(used["w"])},
        )

    rest, sleep = _client(Market.USDM, handler, clock)
    assert not rest.weights.seeded
    info = asyncio.run(rest.load_exchange_info(("BTCUSDT",)))
    assert rest.weights.seeded
    assert info.symbols["BTCUSDT"].status == "TRADING"
    assert rest.weights.windows["1M"] == (1, 2400)
    asyncio.run(rest.server_time())  # server reports 1199
    asyncio.run(rest.server_time())  # 1199 + 1 fits the 1200 budget; server reports 1200
    assert sleep.calls == []
    asyncio.run(rest.server_time())  # 1200 + 1 exceeds the budget -> waits for the window
    assert sleep.calls
    assert clock.now_ms() >= T0 + MIN  # waited for the next minute window
    assert seen.count("/fapi/v1/time") == 3


def test_spot_exchange_info_requests_symbols_param() -> None:
    clock = ManualClock(T0)
    params: list[dict[str, str]] = []

    def handler(request: Any) -> Any:
        params.append(dict(request.url.params))
        return httpx.Response(200, json=load_fixture("rest_spot_exchange_info.json"))

    rest, _ = _client(Market.SPOT, handler, clock)
    asyncio.run(rest.load_exchange_info(("BTCUSDT", "ETHUSDT")))
    assert params[0]["symbols"] == '["BTCUSDT","ETHUSDT"]'
    assert rest.weights.windows["1M"][1] == 6000


def test_429_honours_retry_after_then_succeeds() -> None:
    clock = ManualClock(T0)
    responses = [
        httpx.Response(429, headers={"Retry-After": "7"}, json={"code": -1003, "msg": "x"}),
        httpx.Response(200, json=load_fixture("rest_time.json")),
    ]

    def handler(request: Any) -> Any:
        return responses.pop(0)

    rest, sleep = _client(Market.SPOT, handler, clock)
    assert asyncio.run(rest.server_time()) == 1_792_411_265_000
    assert clock.now_ms() >= T0 + 7_000
    assert sum(sleep.calls) >= 7
    assert rest.rate_limited == 1


def test_429_persisting_raises_after_retries() -> None:
    clock = ManualClock(T0)

    def handler(request: Any) -> Any:
        return httpx.Response(429, headers={"Retry-After": "1"})

    rest, _ = _client(Market.SPOT, handler, clock, max_retries=2)
    with pytest.raises(RestRateLimited):
        asyncio.run(rest.server_time())
    assert rest.rate_limited == 3


def test_418_stops_rest_and_raises_typed_error() -> None:
    clock = ManualClock(T0)
    calls = {"n": 0}

    def handler(request: Any) -> Any:
        calls["n"] += 1
        return httpx.Response(418, headers={"Retry-After": "120"})

    rest, _ = _client(Market.USDM, handler, clock)
    with pytest.raises(RestBanned) as info:
        asyncio.run(rest.klines("BTCUSDT", T0, T0 + MIN))
    assert info.value.status == 418
    assert info.value.until_ms == T0 + 120_000
    assert rest.banned
    with pytest.raises(RestBanned):
        asyncio.run(rest.server_time())
    assert calls["n"] == 1  # nothing was sent while banned
    clock.advance(120_000)
    assert not rest.banned


def test_418_without_retry_after_stops_until_restart() -> None:
    clock = ManualClock(T0)

    def handler(request: Any) -> Any:
        return httpx.Response(418)

    rest, _ = _client(Market.SPOT, handler, clock)
    with pytest.raises(RestBanned):
        asyncio.run(rest.server_time())
    clock.advance(10 * 24 * 3_600_000)
    assert rest.banned


def test_spot_falls_back_to_data_api_on_5xx_and_transport_errors() -> None:
    clock = ManualClock(T0)
    hosts: list[str] = []

    def handler(request: Any) -> Any:
        hosts.append(request.url.host)
        if request.url.host == "api.binance.com":
            return httpx.Response(503)
        return httpx.Response(200, json=load_fixture("rest_time.json"))

    rest, _ = _client(
        Market.SPOT,
        handler,
        clock,
        bases=("https://api.binance.com", "https://data-api.binance.vision"),
    )
    assert asyncio.run(rest.server_time()) == 1_792_411_265_000
    assert hosts == ["api.binance.com", "data-api.binance.vision"]
    assert rest.base_url == "https://data-api.binance.vision"

    def broken(request: Any) -> Any:
        if request.url.host == "data-api.binance.vision":
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json=load_fixture("rest_time.json"))

    rest2, _ = _client(
        Market.SPOT,
        broken,
        clock,
        bases=("https://data-api.binance.vision", "https://api.binance.com"),
    )
    assert asyncio.run(rest2.server_time()) == 1_792_411_265_000


def test_errors_are_typed() -> None:
    clock = ManualClock(T0)

    def handler(request: Any) -> Any:
        if request.url.path.endswith("/klines"):
            return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})
        return httpx.Response(200, json={"unexpected": True})

    rest, _ = _client(Market.SPOT, handler, clock)
    with pytest.raises(RestError) as e400:
        asyncio.run(rest.klines("NOPE", T0, T0 + MIN))
    assert e400.value.status == 400
    with pytest.raises(RestError, match="malformed"):
        asyncio.run(rest.server_time())


def test_only_public_paths_and_futures_endpoints() -> None:
    clock = ManualClock(T0)
    paths: list[str] = []

    def handler(request: Any) -> Any:
        paths.append(request.url.path)
        p = request.url.path
        if p == "/fapi/v1/premiumIndex":
            return httpx.Response(200, json=load_fixture("rest_usdm_premium_index.json"))
        if p == "/fapi/v1/fundingRate":
            return httpx.Response(200, json=load_fixture("rest_usdm_funding_rate.json"))
        if p == "/fapi/v1/markPriceKlines":
            return httpx.Response(200, json=load_fixture("rest_usdm_mark_price_klines.json"))
        return httpx.Response(404)

    fut, _ = _client(Market.USDM, handler, clock)
    assert asyncio.run(fut.premium_index("BTCUSDT")).mark_price == pytest.approx(67234.12)
    assert len(asyncio.run(fut.funding_rates("BTCUSDT", T0 - 86_400_000, T0))) == 2
    assert len(asyncio.run(fut.mark_price_klines("BTCUSDT", T0, T0 + MIN))) == 1
    assert paths == ["/fapi/v1/premiumIndex", "/fapi/v1/fundingRate", "/fapi/v1/markPriceKlines"]

    spot, _ = _client(Market.SPOT, handler, clock)
    with pytest.raises(RestError):
        asyncio.run(spot.premium_index("BTCUSDT"))
    with pytest.raises(RestError, match="not an allowed"):
        asyncio.run(spot._get("/api/v3/account", {}, 1))
    assert len(paths) == 3  # nothing sent for refused calls
