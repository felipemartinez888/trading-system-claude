# Market Data Architecture

## 1. Binance facts this design depends on

Checked against Binance developer documentation on **2026-10-19**. Binance changes these often
(the USDⓈ-M WebSocket endpoint was split into routed paths in 2026), so the ingestion layer reads
limits from `exchangeInfo` at startup, honors response headers, and a weekly "API drift" check is
part of operations.

| Item | Spot | USDⓈ-M Futures |
|---|---|---|
| REST base | `api.binance.com` (and market-data-only `data-api.binance.vision`) | `fapi.binance.com` |
| WebSocket base | `wss://stream.binance.com:9443` or `:443`; market-data-only `wss://data-stream.binance.vision` | `wss://fstream.binance.com/public` (bookTicker, depth) and `/market` (klines, markPrice, aggTrade, forceOrder, tickers). Legacy unrouted URLs were decommissioned after 2026-04-23. |
| Streams per connection | 1024 | 1024 |
| Client → server message limit | 5 msg/s (pings, pongs and subscribe messages count) | 10 msg/s |
| Ping / pong | server ping every 20 s; pong within 1 min | server ping every 3 min; pong within 10 min |
| Connection lifetime | 24 h, then forced disconnect | 24 h, then forced disconnect |
| Connection attempts | 300 per 5 min per IP | (not documented on the connect page — treat as spot) |
| Retired endpoints to avoid | `/api/v1/*` market data (retired 2026-03-25); `!ticker@arr` (retired 2026-03-26) | — |
| Bulk history | `data.binance.vision` monthly/daily zip archives with `.CHECKSUM` files (klines; for futures also funding rate and mark/index price klines) — **exact file coverage to be verified in the M2 spike** | |

Consequences baked into the design:

- Connections are **planned to be recycled** before 24 h (overlapping reconnect: open new, subscribe,
  confirm first message, close old) so the forced disconnect never creates a gap.
- Futures uses **two connections per group** (`/public` and `/market`), as Binance recommends.
- A token-bucket limiter guards outgoing WS control messages (≤ 4/s spot, ≤ 8/s futures).
- REST requests go through a weight-aware limiter seeded from `exchangeInfo.rateLimits` and corrected
  from `X-MBX-USED-WEIGHT-*` headers; HTTP 429 → back off with `Retry-After`; HTTP 418 → stop REST and
  raise a health alarm.
- The development cloud workspace cannot reach Binance (egress allowlist); live integration tests run
  on the owner's Windows machine or after the domains are allowlisted (decision D-01).

## 2. Data products

| Product | Source | Purpose |
|---|---|---|
| `bars_1m` (canonical OHLCV + quote volume, trades, taker-buy volume) | archives → REST → WS | Everything: features, signals, simulation, intrabar resolution. |
| `bars_{5m,15m,1h,4h}` | derived from `bars_1m` | Strategy timeframes. Consistency-checked against native Binance klines on a sample. |
| `book_top` (best bid/ask, sizes) | WS bookTicker (live only), sampled at 1 s | Live spread/slippage estimates and the spread gate. **No historical equivalent is planned**; backtests use a calibrated spread model (see SIMULATION). |
| `mark_price_1m`, `funding` | archives/REST (futures) | Perp P&L and funding cash flows. |
| `exchange_filters` snapshots | REST `exchangeInfo` | Tick/lot size, min notional, status (`TRADING`, `CANCEL_ONLY`…); snapshot stored per session. |
| `server_clock` samples | REST `/time` | Clock-offset monitoring; latency metrics are corrected by it. |

## 3. Provenance and state model

Every bar row carries:

| Column | Values |
|---|---|
| `source` | `archive`, `rest_backfill`, `ws_live`, `rest_recovered` |
| `is_closed` | bars in storage are always closed; partial bars exist only in memory/UI as `PARTIAL` |
| `observed_session_id` | session in which the bar was *seen live* (null if only backfilled) |
| `ingested_at`, `event_time` | UTC epoch milliseconds (microsecond option not used for bars) |
| `dataset_id` | content-addressed ID of the archive/import batch it came from |

The UI maps this to user-facing states: **LIVE** (closed, ws_live, fresh), **PARTIAL** (forming candle),
**RECOVERED** (filled after the fact via REST), **HISTORICAL** (archive/backfill), **STALE** (freshness
budget exceeded), **MISSING** (known gap, unrecovered).

### Watermark rule for closing a bar
A 1m bar `[t, t+60s)` is accepted as closed when either:
1. the WS kline event for it arrives with the closed flag set → `ws_live`, or
2. `now_server > t + 60s + grace (default 5 s)` without (1) → fetch via REST; if returned → `rest_recovered`
   and a `bar_late` event; if not → `gap_detected`.

Duplicates (same market, symbol, open time) are idempotent upserts only when identical; a differing
duplicate raises `bar_conflict` (kept for audit, newest-by-rule wins, counted in health).

### Freshness budgets
Each stream has a budget (e.g. kline 1m: 75 s since last closed bar; bookTicker: 10 s; markPrice: 5 s).
Exceeding it flips the stream to **STALE**, emits an event, shows in the UI and — via the risk engine —
blocks new entries for affected symbols.

## 4. Gaps, coverage and downtime

Two kinds of coverage are tracked separately because they mean different things:

- **Market data coverage** — do we have the closed bars for this interval? (Repairable via REST/archives.)
- **Observation coverage** — was the app running and seeing this interval live? (Never repairable.)

Live signals can only exist inside observation coverage. The forward trial and A/B evaluation are
computed over the intersection of both systems' observation coverage (see EVALUATION_PROTOCOL).

`data_gaps` records `{market, symbol, interval, from, to, cause (downtime | disconnect | exchange | unknown),
detected_at, resolution (recovered | unrecoverable | pending), resolved_at}`.

## 5. Storage layout

```
data/
  lake/                                   # Parquet, immutable once written
    market=spot/kind=bars_1m/symbol=BTCUSDT/year=2025/month=03/part-<dataset_id>.parquet
    market=usdm/kind=funding/symbol=BTCUSDT/...
  catalog.duckdb                          # views over the lake + dataset manifests
  state.db                                # SQLite WAL: events + projections + live bars buffer
  imports/<dataset_id>/manifest.json      # source URLs, checksums verified, row counts, hash
```

- Live closed bars are written to `state.db` first (durable, transactional) and compacted to Parquet
  daily (and on clean shutdown); compaction is idempotent and verified by row counts and hashes.
- A `dataset_id` = SHA-256 over sorted file hashes; backtest manifests reference dataset IDs, so a run
  can be reproduced or shown to be irreproducible.

## 6. Universe (REQ-DATA-08)

Selected by a rule frozen in `configs/universe.yaml` with a snapshot date, e.g.:
listed on **both** spot and USDⓈ-M perps for ≥ 24 months before the snapshot; USDT-quoted; excluding
stablecoin bases, leveraged tokens and symbols not in `TRADING` status; ranked by 90-day median daily
quote volume; top N (default 10). The resulting list and the data used to compute it are stored.
Survivorship bias of a "today's survivors" universe over past years is a documented limitation;
point-in-time universes are a post-MVP improvement.

## 7. Timeframes

Canonical 1m. Strategy timeframes start with **5m, 15m and 1h** (intraday focus). Scalping views use
1m/5m for display and research only; swing (4h) is enabled after the intraday pipeline is validated.
Only one timeframe family is optimized at a time.
