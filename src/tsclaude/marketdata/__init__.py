"""Market data: Binance public REST/WebSocket ingestion, bar building, backfill (DATA_ARCHITECTURE).

Pure-logic modules (`types`, `models`, `binance.parse`, `binance.limits`, `bars`, `convert`,
`backfill`, `config`) import no network library. `binance.rest` needs httpx and `binance.ws`
imports websockets lazily, only when no connector is injected.
"""
