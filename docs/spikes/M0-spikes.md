# M0 Spikes — findings

## (a) Windows asyncio + websockets behaviour
- **Status:** pending owner run of `scripts/spikes/binance_check.py` on Windows (it records which
  event loop is in use and whether all four WebSocket endpoints deliver messages).
- The full reconnect/24h-recycle soak belongs to M1 (gate: 2-hour run with zero unexplained gaps).

## (b) Binance public archive coverage (`data.binance.vision`)
Source: the `binance/binance-public-data` repository README (fetched 2026-10-19).

| Finding | Design impact |
|---|---|
| Spot and USD-M futures archives provide `klines`, `aggTrades`, `trades`, in daily and monthly zips. New daily files appear the next day; monthly files on the first Monday of the month. | Importer uses monthly files for complete months and daily files for the current month; REST covers the remaining tail. |
| Each zip has a sibling `.CHECKSUM` (SHA-256). | Importer verifies every file before use; failures are hard errors (`dataset` not created). |
| Kline CSV columns: open time, open, high, low, close, volume, close time, quote volume, trades, taker buy base volume, taker buy quote volume, ignore. Same for spot and USD-M. | One parser for both markets. |
| **Spot archive timestamps are in microseconds from 2025-01-01 onwards** (milliseconds before). | Importer detects the unit per file (magnitude check + date) and normalizes to epoch ms; a unit test with both formats is mandatory in M2. Mixing units silently would corrupt every 2025+ bar. |
| Futures funding rate, mark/index price klines in the archive: **not documented in the README.** | The spike script lists the futures archive prefixes on the owner's machine to confirm; REST endpoints `/fapi/v1/fundingRate` and `/fapi/v1/markPriceKlines` are the fallback. |

## (c) Reachability from the owner's network
- The cloud development workspace reaches **no** Binance domain and, currently, **no** package
  registry (PyPI, npm) — egress proxy policy. Verification therefore runs in GitHub Actions (code) and
  on the owner's PC (live endpoints).
- **Status:** pending owner run of `scripts/spikes/binance_check.py`. Expected output: one line per
  check and a JSON report under `evidence/spikes/` to commit.
