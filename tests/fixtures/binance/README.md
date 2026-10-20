# Binance payload fixtures

Structures follow Binance's public documentation (field names, types and nesting) for:
Spot WebSocket Streams (kline, bookTicker, combined-stream envelope, subscribe response),
USD-M Futures Websocket Market Streams (kline, bookTicker, markPriceUpdate), and REST market data
(`/api/v3/time`, `/api/v3/exchangeInfo`, `/api/v3/klines`, `/fapi/v1/exchangeInfo`,
`/fapi/v1/klines`, `/fapi/v1/premiumIndex`, `/fapi/v1/fundingRate`, `/fapi/v1/markPriceKlines`).

**The numeric values are synthetic test values, not recorded market data.** Timestamps are around
2026-10-19 12:00 UTC (1792411200000 ms). Exchange-info payloads are trimmed to the fields and filters
this code reads plus a few neighbours, in the documented shape.
