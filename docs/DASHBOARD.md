# Dashboard Design

## 1. Information architecture

Five workspaces in a left rail, a persistent **status bar** on top. Every required section of the
Master Prompt (§10) maps to exactly one place.

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ ● MONITORING  Session 3h12m │ Spot ✓ Fut ✓ │ Clock +42ms │ AI: Sonnet $0.31/$1 │ 🔔 3 │ [Stop] │
├──────────┬───────────────────────────────────────────────────────────────────────┤
│ LIVE     │                                                                       │
│  Markets │                                                                       │
│  Scanner │                     workspace content                                 │
│  Signals │                                                                       │
│ PAPER    │                                                                       │
│ RESEARCH │                                                                       │
│ PERFORM. │                                                                       │
│ SYSTEM   │                                                                       │
└──────────┴───────────────────────────────────────────────────────────────────────┘
```

| Workspace | Pages | Master-prompt section(s) |
|---|---|---|
| **Live** | Markets · Scanner · Signals (inbox + detail drawer with Agent tab) | Market Overview, Opportunity Scanner, Signals, Agent Analysis |
| **Paper** | Portfolio · Positions & Orders · Ledger · Books compare (baseline vs agent-gated vs benchmark) | Paper Trading |
| **Research** | Strategies & pre-registrations · Backtests (list → run detail) · Validation (walk-forward, holdout, trials) | Backtesting, Strategy Research |
| **Performance** | Analytics (by strategy/asset/regime/hour) · Agent value · Evaluation export | Performance Analytics |
| **System** | Health (streams, freshness, gaps, coverage timeline) · Logs & audit · Costs · Settings | Data and System Health, Settings |

## 2. Provenance badges (REQ-UI-02)

Every number, chart series and table row carries one badge, driven by a `provenance` field in the
API response, never inferred by the frontend:

`SYSTEM` · `LIVE` · `PARTIAL` · `RECOVERED` · `HISTORICAL` · `REPLAY` · `BACKTEST` · `SIMULATED` · `ESTIMATED` ·
`DEMO` · `STALE` · `UNAVAILABLE`

`DEMO` data (used while the backend is being built) renders with a diagonal-hatch watermark and a
banner. Demo fixtures live in one module and cannot be enabled while monitoring is on.

## 3. Key screens

**Markets** — watchlist table (spot/perp price, 24h change, ATR percentile, regime, spread, funding,
freshness dot), candlestick chart with signals and gap shading, data-source legend.

**Scanner** — current Early Watch candidates ranked by strategy-defined score, with "why" (conditions
met / pending) and a time-to-expiry bar; filters by strategy, grade, timeframe, market.

**Signals** — inbox (new / watching / confirmed / open / closed / vetoed / expired), unread badges,
optional local sound. Detail drawer tabs: *Summary* (entry/SL/TP, R:R gross/net, grade),
*Evidence* (annotated chart, conditions, snapshot), *Agents* (analyst dimensions, skeptic objections,
synthesis rule applied, cost, latency, or `NOT_REQUESTED`/`AI_UNAVAILABLE`), *Timeline* (every event),
*Outcome* (per book, incl. shadow outcome for vetoed signals).

**Backtests** — run list with grade chips; run detail: equity vs benchmark, drawdown, trade list,
return distribution, MAE/MFE scatter, by-hour/weekday/regime heatmaps, cost breakdown, ambiguity
sensitivity, bootstrap CIs, manifest (hashes) with "reproduce" button.

**Validation** — walk-forward fold table and stitched OOS curve, parameter-neighbourhood heatmap,
trial counter and deflated Sharpe, holdout status (sealed / unlocked once, result).

**Paper** — equity per book, open positions with live unrealized P&L (marked `STALE` when data is),
orders/fills, ledger with reconciliation status, kill-switch control.

**Health** — stream table (state, last message, msg/s, reconnects, recycled connections), freshness
budgets, gap list with resolution, coverage timeline (observed vs market-data coverage), clock offset,
REST weight usage, event-chain integrity check, disk usage.

**Settings** — universe (read-only snapshot + rule), timeframes, risk limits (versioned; changes are
events), agent mode/model/budget, cost model, data retention, runtime (auto-start monitoring).

## 4. Empty and failure states

Each page defines: *nothing yet* (what will appear and what action produces it), *stopped*
(monitoring off — last-known values greyed and labeled), *stale* (badge + reason), *error*
(what failed, link to Health). No chart is ever drawn with invented points to "look full".

## 5. Technical notes

- Frontend talks to `/api/*` (REST read models) and `/ws/live` (push of bars, signal events, health,
  notifications). Types generated from OpenAPI.
- Charts: lightweight-charts for OHLC and markers; ECharts for analytics.
- Design system: dark-first, high-density tables, colour-blind-safe up/down palette; accessible
  contrast in light mode too.
- Playwright E2E tests run against a backend in `replay` mode with recorded fixtures, so UI tests are
  deterministic and need no network.
