# Trading System Claude

Local-first cryptocurrency market-intelligence, quantitative research, backtesting and
paper-trading platform for Binance Spot and USDⓈ-M Futures public market data —
designed, implemented and reviewed exclusively with Claude and Claude-based agents.

> **Status (2026-11-10): MVP FUNCTIONAL — VALIDATION INCOMPLETE.** The paper-only engineering
> MVP runs locally: Binance public data ingestion, the Parquet research lake, the backtest runner
> with certified reports, the paper runtime with deterministic replay, and the dashboard.
> Evidence and what is still open: [`docs/MVP_VALIDATION_REPORT.md`](docs/MVP_VALIDATION_REPORT.md);
> limitations: [`docs/MVP_LIMITATIONS.md`](docs/MVP_LIMITATIONS.md); how to run it:
> [`docs/MVP_USER_GUIDE.md`](docs/MVP_USER_GUIDE.md).
>
> Keep these apart: M2 is accepted with a limited scope (D-22); M3 has technical evidence but no
> formal owner acceptance; **no strategy has demonstrated an edge** (the only implemented strategy
> is a reference baseline, and its one real-data backtest made 0 trades); quantitative research
> (M4) has its tooling in progress and no real research run; there is **no live trading**, and none is planned before a separate
> owner decision. Milestone status: [`docs/ROADMAP.md`](docs/ROADMAP.md).

## Why this project exists

It is one arm of an experiment comparing:

1. a trading platform built entirely through Claude (this repository), and
2. a separately developed platform built through a multi-model, independently audited process.

The comparison covers both **signal performance** (prospective, cost-aware, statistically
honest) and **development effectiveness** (time, cost, defects, rework). See
[`docs/EVALUATION_PROTOCOL.md`](docs/EVALUATION_PROTOCOL.md).

## Hard boundaries

- Paper trading only. No live order execution, no exchange API keys, no real money.
- No Discord / Telegram / cloud hosting / domain in the MVP.
- No code, models, parameters, results or data imported from the original `trading-system` repository.
- The quantitative engine must work with **zero** Claude API calls; Claude agents are an optional,
  measured advisory layer that can only veto or downgrade, never bypass risk controls.
- A historical backtest is never reported as live validation.

## Documents

| Document | Purpose |
|---|---|
| [docs/PLAN_OVERVIEW.md](docs/PLAN_OVERVIEW.md) | One-page summary of the whole plan — start here |
| [docs/PRODUCT_REQUIREMENTS.md](docs/PRODUCT_REQUIREMENTS.md) | Requirements analysis with traceable IDs |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System architecture, options considered, stack, repo layout |
| [docs/DATA_ARCHITECTURE.md](docs/DATA_ARCHITECTURE.md) | Binance ingestion, storage, provenance, gaps, recovery |
| [docs/SIMULATION.md](docs/SIMULATION.md) | Backtest + paper engine ("one simulator, two clocks"), risk engine |
| [docs/SIGNALS_AND_AGENTS.md](docs/SIGNALS_AND_AGENTS.md) | Signal lifecycle and Claude signal-evaluation agents |
| [docs/RESEARCH_PROTOCOL.md](docs/RESEARCH_PROTOCOL.md) | Pre-registration, walk-forward, sealed holdout, statistics |
| [docs/DASHBOARD.md](docs/DASHBOARD.md) | Dashboard information architecture |
| [docs/DEV_ORCHESTRATION.md](docs/DEV_ORCHESTRATION.md) | Claude-only multi-agent development workflow |
| [docs/EVALUATION_PROTOCOL.md](docs/EVALUATION_PROTOCOL.md) | Draft A/B comparison protocol and export format |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Milestones, gates, MVP acceptance criteria, live status |
| [docs/RISKS_AND_COSTS.md](docs/RISKS_AND_COSTS.md) | Technical/scientific risks, infrastructure vs AI costs |
| [docs/DECISIONS_PENDING.md](docs/DECISIONS_PENDING.md) | Decisions that need the owner's input |
| [docs/INDEPENDENCE_LOG.md](docs/INDEPENDENCE_LOG.md) | Record of every consultation of original-project material |
| [docs/adr/](docs/adr/) | Architecture Decision Records |

## Not financial advice

This is a research instrument. Its outputs are hypotheses under test, not recommendations.
