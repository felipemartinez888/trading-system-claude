"""Live smoke tests against real Binance public endpoints. Skipped by default (`-m 'not live'`).

Run on the owner's machine:  uv run pytest -m live tests/integration/marketdata -q
"""

from __future__ import annotations

import asyncio

import pytest

from tsclaude.contracts.market import Market
from tsclaude.contracts.timeutil import MS_PER_MINUTE, SystemClock
from tsclaude.marketdata.config import EndpointsConfig, WsConfig

pytestmark = pytest.mark.live


def test_live_rest_exchange_info_time_and_klines() -> None:
    pytest.importorskip("httpx")
    from tsclaude.marketdata.binance.rest import BinanceRest

    async def main() -> None:
        ep = EndpointsConfig()
        clock = SystemClock()
        for market, bases in ((Market.SPOT, ep.spot_rest), (Market.USDM, ep.usdm_rest)):
            async with BinanceRest(market, bases, clock) as rest:
                info = await rest.load_exchange_info(("BTCUSDT",))
                assert info.symbols["BTCUSDT"].status
                assert rest.weights.seeded
                server = await rest.server_time()
                assert abs(server - clock.now_ms()) < 60_000
                end = server - server % MS_PER_MINUTE
                rows = await rest.klines("BTCUSDT", end - 5 * MS_PER_MINUTE, end)
                assert [r.open_time_ms for r in rows] == [
                    end - i * MS_PER_MINUTE for i in range(5, 0, -1)
                ]

    asyncio.run(main())


def test_live_ws_each_group_delivers() -> None:
    pytest.importorskip("websockets")
    from tsclaude.marketdata.binance.parse import parse_ws_frame
    from tsclaude.marketdata.binance.ws import default_connector

    async def main() -> None:
        ep = EndpointsConfig()
        connect = default_connector(WsConfig())
        urls = [
            (Market.SPOT, f"{ep.spot_ws[0]}/stream?streams=btcusdt@bookTicker"),
            (Market.USDM, f"{ep.usdm_ws_market[0]}/stream?streams=btcusdt@markPrice@1s"),
            (Market.USDM, f"{ep.usdm_ws_public[0]}/stream?streams=btcusdt@bookTicker"),
        ]
        for market, url in urls:
            conn = await connect(url)
            try:
                raw = await asyncio.wait_for(conn.recv(), 15)
                parse_ws_frame(raw, market)
            finally:
                await conn.close()

    asyncio.run(main())
