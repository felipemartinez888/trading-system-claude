# Plan Overview — Trading System Claude

**Status:** proposal awaiting owner approval · **Date:** 2026-10-19 · Nothing is implemented yet.

## The idea in five sentences
A single local Python process ingests Binance Spot and USDⓈ-M public data into canonical 1-minute
bars with explicit provenance, detects gaps and staleness, and runs **pre-registered** strategies that
emit *Early Watch* and *Confirmed Entry* signals. One event-driven simulator executes those signals in
backtests (historical clock) and in paper trading (live clock), so research and live behaviour cannot
silently diverge. A deterministic, fail-closed risk engine has the final word; optional Claude agents
(Analyst + Skeptic) can only approve, downgrade or veto, and their value is measured by running a
`baseline` and an `agent_gated` paper book side by side. Everything that happens is an append-only,
hash-chained event, which makes "recorded before the outcome" provable for the A/B comparison. A
React dashboard served from localhost shows every number with a provenance badge — live, recovered,
backtest, simulated, demo, stale — and never invents values.

## Architecture at a glance
See [ARCHITECTURE](ARCHITECTURE.md) (diagram, options, stack, layout) and ADRs 0002–0011.

| Layer | Choice |
|---|---|
| Runtime | One asyncio process + worker processes for backtests; Start/Stop monitoring in-app |
| Data | Binance archives (bulk) → REST (backfill/recovery) → WebSockets (live); 1m canonical bars |
| Storage | SQLite event log + projections; Parquet lake + DuckDB for analytics |
| Research | Pre-registration, anchored walk-forward, code-enforced sealed holdout, bootstrap CIs, deflated Sharpe, random-entry controls, evidence grades A–D/X |
| Simulation | Next-bar execution, trade-through limits, gap-through stops, SL-first on ambiguity, Decimal ledger, fees/slippage/funding |
| Risk | Freshness, clock skew, spread, cost-adjusted R:R, per-trade risk, exposure, daily loss, drawdown kill switch — fail-closed |
| AI | Analyst + Skeptic (Claude), deterministic synthesis, masked evidence packets, budget cap, works with AI off |
| UI | FastAPI + React/TS; Live · Paper · Research · Performance · System workspaces |

## Development strategy
Claude-only, role-based subagents with module ownership (Data, Quant, Simulation & Risk,
Platform & UI, AI Agents) plus fresh-context **Reviewer** and adversarial **Causality Auditor**;
merges only on CI evidence; every task logged in dev metrics. See [DEV_ORCHESTRATION](DEV_ORCHESTRATION.md).

## Milestones
P Planning ✅ → **M0** Foundations → **M1** Live tape (first usable app: real data, health, demo
signals) → **M2** Historical data + backtest core → **M3** Paper trading + risk → **M4** Research
protocol + first strategies → **M5** Claude signal agents → **M6** Analytics + evaluation export →
**M7** Forward trial. MVP = M0–M5 + export. Details, gates and the 16 acceptance criteria:
[ROADMAP](ROADMAP.md).

## What could make this fail
No net edge exists (likely, and reportable); backtest/live divergence; LLM training-data
contamination of historical agent tests; Binance API churn or regional limits; sparse observation
coverage on a personal PC. See [RISKS_AND_COSTS](RISKS_AND_COSTS.md).

## Costs
Infrastructure $0 recurring. Optional runtime agents ≈ $0.03 per reviewed signal on Sonnet 5.5
(≈ $20/month at 20 reviews/day), capped at $1/day by default; requires an Anthropic API key billed
separately.

## Needs your input
D-01 network access for live tests · D-02 futures paper positions · D-03 runtime API + budget ·
D-04 universe/timeframes · D-05 data splits · D-06 A/B coordination · D-07 push approval ·
D-08 commit author. Recommendations in [DECISIONS_PENDING](DECISIONS_PENDING.md).
