# Backtesting, Paper Trading and Risk

## 1. One simulator, two clocks

A single event-driven simulator (`tsclaude.sim`) executes orders against bars. It is driven by a
`Clock` interface:

- `HistoricalClock` — iterates closed bars from the Parquet lake (backtests, walk-forward, replays).
- `LiveClock` — receives closed bars from the bar builder in real time (paper trading).

Strategies, features, the risk engine and the simulator cannot tell the difference except through
`clock.mode`, which only affects labeling. **Parity test:** a recorded live session replayed through
`HistoricalClock` must reproduce the paper ledger byte-for-byte (same events, same hashes).

## 2. Decision and execution timing

- Strategies evaluate at bar close `T` using bars with `close_time ≤ T` only.
- The earliest execution is the **next bar open** after `T + decision_latency` (default: next 1m bar
  open). A signal can never fill at the close it was computed from.
- Live latency (bar close → signal persisted) is measured and reported; backtests assume a
  configurable latency at least as large as the observed live p95.

## 3. Orders and fill model

| Order | Fill rule (conservative) |
|---|---|
| Market entry/exit | Next 1m open ± slippage; taker fee. |
| Limit entry | Fills only if a later 1m bar trades **through** the limit by ≥ 1 tick (touching is not filling); maker fee; unfilled orders expire per the signal's validity window. |
| Stop-loss | Triggered when the 1m path reaches the stop; fills at the stop **or the bar open if it gapped beyond it**, plus slippage; taker fee. |
| Take-profit | Limit semantics (must trade through), maker fee; option to model as taker for robustness runs. |
| Time exit / invalidation | Market at next open. |

**Intrabar ambiguity.** Strategy bars (5m/15m/1h) are resolved on the 1m path. If SL and TP are both
reachable inside the same 1m bar, the outcome is **SL first** and the trade is flagged `ambiguous_bar`;
the report shows the count and the P&L sensitivity if those trades were resolved TP-first. If the
ambiguous share exceeds a threshold (default 5 % of trades) the run's evidence grade is capped.

**Costs.** Fees from `configs/costs.yaml` (VIP-0 defaults, to be verified against Binance's current
fee schedule when the evaluation protocol is frozen). Slippage = `half_spread + impact`, where:
- live/paper: half-spread from the latest `book_top` sample, impact = k · σ_1m · √(order_notional / bar_quote_volume);
- backtest: spread from a model calibrated on captured live `book_top` per symbol and volatility bucket
  (before enough live data exists, a deliberately pessimistic default is used and the run is labeled so).
Cost stress tests (×1.5, ×2) are mandatory in validation reports.

**Perpetual futures (if D-02 approves futures paper positions).** Funding applied at actual funding
timestamps using historical/live funding rates; P&L marked on mark price; leverage capped at 1×
notional/equity in MVP, so liquidation is out of reach but margin checks still run.

## 4. Sizing and ledger

- Fixed-fractional risk: position size = `risk_per_trade × equity / |entry − stop|`, then clipped by
  max notional per position, lot size, min notional and available cash/margin. Rounding always toward
  smaller size.
- Ledger amounts use `Decimal` quantized to 1e-8 in the quote asset; research vectors use float64.
  Every fill produces balanced ledger entries (cash, position, fee, funding), so equity is
  reconstructible from entries alone, and a reconciliation test checks it.
- Mark-to-market at every 1m close for equity curves and drawdown.

## 5. Books

The paper engine runs several books in parallel on the same signals so comparisons are prospective
and apples-to-apples:

| Book | Takes |
|---|---|
| `baseline` | every deterministic `CONFIRMED` signal that passes risk |
| `agent_gated` | the subset not vetoed by Claude agents (exists only when agents are enabled) |
| `benchmark` | buy-and-hold of the universe, equal weight (spot) |

This makes the agents' incremental value (REQ-AGT-07) a direct, forward-measured difference.

## 6. Risk engine (deterministic, fail-closed)

Evaluated before an entry is accepted and again after agent review. Any exception → `RISK_BLOCKED`.

| Rule | Default (configurable, versioned) |
|---|---|
| Data freshness | all streams for the symbol HEALTHY; no unresolved gap in the strategy lookback |
| Clock skew | |local − Binance server| ≤ 1 s |
| Spread gate | live spread ≤ max(2 × median spread, cap) |
| Cost-adjusted reward/risk | ≥ 1.5 after fees + slippage |
| Risk per trade | 0.5 % of book equity |
| Max concurrent positions | 5 per book; 1 per symbol |
| Correlated exposure | ≤ 3 positions in the same correlation cluster (e.g. BTC-beta) |
| Gross exposure | ≤ 100 % of equity (no leverage in MVP) |
| Daily loss limit | −2 % of start-of-day equity → no new entries until next UTC day |
| Drawdown kill switch | −10 % from peak → book halted until manual reset (logged) |
| Signal validity | entry must happen within the signal's validity window |

## 7. Restart behaviour

On restart, open paper positions are **resolved over the backfilled 1m path** for the downtime (as if
resting SL/TP orders had been on the exchange), tagged `resolved_from_backfill`. No new entries are
created for the downtime. Signals still in `WATCH`/`CONFIRMED` whose validity window passed during
downtime become `EXPIRED (downtime)`.

## 8. Reports and run manifest

Each run writes `runs/<run_id>/` with `manifest.json` (dataset IDs, strategy ID + version + params hash,
config hashes, code commit, seed, timezone=UTC, cost model version), `trades.parquet`,
`equity.parquet`, `metrics.json`. Metrics: number of trades, net expectancy (R and %), profit factor,
win rate with exact Clopper–Pearson CI, break-even win rate `p*` and certified margin (ADR-0012), payoff ratio, max drawdown and duration, Sharpe/Sortino (annualization
stated), turnover, exposure, MAE/MFE, cost share of gross P&L, by-symbol/hour/regime breakdowns,
stationary block-bootstrap CIs.

## 9. Verification plan (tests that gate M2/M3)

- **Look-ahead canaries:** synthetic series where any use of future data produces an impossible
  P&L; features/strategies must yield exactly the no-information result.
- **Golden trades:** hand-computed fixtures for each order type, gap-through stops, ambiguous bars,
  fees, funding, rounding.
- **Property tests (hypothesis):** ledger always balances; equity never depends on bar-iteration
  chunking; size never exceeds limits.
- **Window parity:** features computed on full history equal the trailing-window live computation for
  every bar after warm-up.
- **Replay parity:** recorded live session → identical paper ledger hash.
- **Reproducibility:** same manifest → identical `metrics.json` hash on Windows and Linux.
