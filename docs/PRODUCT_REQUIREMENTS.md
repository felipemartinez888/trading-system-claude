# Product Requirements Analysis

**Sources:** owner's *Master Prompt — Trading System Claude* (2026-10-19, authoritative) and
*Blueprint V1.1 Local-first* (2026-10-19, used as functional requirements only — see
[INDEPENDENCE_LOG](INDEPENDENCE_LOG.md)). Where they conflict, the Master Prompt wins.

Each requirement has an ID used by the roadmap, tests and acceptance criteria.
"MVP" = required for the first acceptable local demonstration (see [ROADMAP](ROADMAP.md#mvp-acceptance-criteria)).

## 1. Source conflicts resolved

| Conflict | Resolution |
|---|---|
| Blueprint §17 prompt and §18 checklist still require Discord; Blueprint §19 and Master Prompt §2 exclude it. | **Discord excluded from MVP.** Notifications are in-app only (REQ-UI-05). |
| Blueprint suggests a 15-page dashboard; Master Prompt lists 10 sections. | Master Prompt sections are the requirement; IA is our own ([DASHBOARD](DASHBOARD.md)). |
| Blueprint suggests an architecture and agent roster. | Treated as non-binding; our design is independent and justified in ADRs. |
| "Connect to Spot and Futures" vs. regional/term restrictions on futures. | Futures **public market data** is in scope; futures *paper* positions are a pending owner decision (D-02). |

## 2. Functional requirements

### Market data (DATA)
| ID | Requirement | MVP |
|---|---|---|
| REQ-DATA-01 | Ingest Binance Spot public market data (REST historical + WebSocket live). | ✅ |
| REQ-DATA-02 | Ingest Binance USDⓈ-M Futures public data incl. klines, mark price and funding. | ✅ |
| REQ-DATA-03 | Multiple symbols and configurable timeframes; intraday focus, scalping/swing evaluable. | ✅ |
| REQ-DATA-04 | Distinguish historical, live, closed, incomplete, recovered, missing and stale data in storage and UI. | ✅ |
| REQ-DATA-05 | Reconnection, rate-limit handling, duplicate prevention, timestamp normalization (UTC), gap detection. | ✅ |
| REQ-DATA-06 | Never silently treat stale data as current; staleness blocks new entries. | ✅ |
| REQ-DATA-07 | Persist live observations for later replay and forward evaluation. | ✅ |
| REQ-DATA-08 | Universe selected by pre-declared liquidity/history rules, never by test results. | ✅ |

### Signals (SIG)
| ID | Requirement | MVP |
|---|---|---|
| REQ-SIG-01 | Deterministic, LLM-free signal engine. | ✅ |
| REQ-SIG-02 | Two stages: Early Watch (not an entry recommendation) and Confirmed Entry (passed risk/validation). | ✅ |
| REQ-SIG-03 | Each signal carries the 16 fields of Master Prompt §6 (ID … final simulated outcome). | ✅ |
| REQ-SIG-04 | Immutable record created before the outcome is known; lifecycle via append-only transitions. | ✅ |
| REQ-SIG-05 | No signal merely because price moves; strategies must be pre-registered hypotheses. | ✅ |

### AI agents for signal evaluation (AGT)
| ID | Requirement | MVP |
|---|---|---|
| REQ-AGT-01 | Multi-agent Claude evaluation covering context/regime, setup validity, volatility/liquidity, risk, adversarial review, synthesis. | ✅ (with API key) |
| REQ-AGT-02 | Agents receive only validated data; cannot invent prices/indicators; outputs schema-validated. | ✅ |
| REQ-AGT-03 | Configurable mode: off / confirmed-only / research. App fully functional with mode off or API unavailable. | ✅ |
| REQ-AGT-04 | Visible provenance: deterministic-only, reviewed, rejected/downgraded by agents. Original signal preserved. | ✅ |
| REQ-AGT-05 | No agent may bypass hard risk controls. | ✅ |
| REQ-AGT-06 | Cost and latency per evaluation recorded; daily budget enforced. | ✅ |
| REQ-AGT-07 | Incremental value of agents measured against deterministic baseline (ablation). | Forward trial |

### Backtesting (BT)
| ID | Requirement | MVP |
|---|---|---|
| REQ-BT-01 | Reproducible simulation (dataset hash, strategy version, config, code commit, seed). | ✅ |
| REQ-BT-02 | Fees, slippage, position sizing, SL/TP, capital/equity accounting, drawdown, trade-level report. | ✅ |
| REQ-BT-03 | No look-ahead; decisions only on data available at decision time; next-bar execution. | ✅ |
| REQ-BT-04 | Explicit, conservative handling of ambiguous intrabar SL/TP and gaps. | ✅ |
| REQ-BT-05 | Walk-forward, out-of-sample, sealed holdout, sensitivity analysis, strategy comparison. | ✅ (≥1 strategy) |
| REQ-BT-06 | Report uncertainty, sample sizes, limitations, insufficient evidence. | ✅ |
| REQ-BT-07 | Funding for perpetual positions. | If D-02 = yes |

### Paper trading (PT)
| ID | Requirement | MVP |
|---|---|---|
| REQ-PT-01 | Simulated positions on live observations with fees/slippage. | ✅ |
| REQ-PT-02 | Realized/unrealized P&L, equity, drawdown; auditable event history. | ✅ |
| REQ-PT-03 | State survives restart; stale/disconnected data handled safely. | ✅ |
| REQ-PT-04 | Strictly separate from real execution; no credentials. | ✅ |

### Risk (RISK)
| ID | Requirement | MVP |
|---|---|---|
| REQ-RISK-01 | Hard limits: per-trade risk, exposure, concurrency, daily loss, drawdown kill switch, data freshness, spread, cost-adjusted R:R. | ✅ |
| REQ-RISK-02 | Fail-closed: any error in risk evaluation blocks the entry. | ✅ |

### Dashboard (UI)
| ID | Requirement | MVP |
|---|---|---|
| REQ-UI-01 | Market Overview, Scanner, Signals, Agent Analysis, Backtesting, Strategy Research, Paper Trading, Performance Analytics, Data & System Health, Settings. | ✅ |
| REQ-UI-02 | Every value labeled real/live, replay, backtest, simulated, estimated, demo, unavailable or stale. | ✅ |
| REQ-UI-03 | Useful empty states; no fabricated metrics. Demo data only when clearly labeled. | ✅ |
| REQ-UI-04 | Start/Stop monitoring controls showing the real backend state. | ✅ |
| REQ-UI-05 | In-app notifications (inbox, badge, optional local sound) for signals and health events. | ✅ |

### Runtime (RT)
| ID | Requirement | MVP |
|---|---|---|
| REQ-RT-01 | Runs locally on Windows; simple start/stop. | ✅ |
| REQ-RT-02 | Monitoring stops when the app stops; restart recovers state and identifies unobserved periods. | ✅ |
| REQ-RT-03 | Never claim a live signal for a period the app was not running. | ✅ |
| REQ-RT-04 | No paid infrastructure required. | ✅ |

### Evaluation (EVAL)
| ID | Requirement | MVP |
|---|---|---|
| REQ-EVAL-01 | Exportable, timestamped signal/trade/evaluation records, committed before outcomes. | ✅ (export) |
| REQ-EVAL-02 | Metrics: net return, max DD, win rate, PF, expectancy, risk-adjusted returns, frequency, latency, risk violations, by-regime, uncertainty. | ✅ |
| REQ-EVAL-03 | Development metrics: elapsed time, model usage, costs, defects, manual interventions. | ✅ (from day 0) |
| REQ-EVAL-04 | No access to the other system's predictions during evaluation. | Trial |

## 3. Non-functional requirements
| ID | Requirement |
|---|---|
| NFR-01 | Modular architecture with documented interfaces (OpenAPI + JSON Schema). |
| NFR-02 | Automated tests incl. deterministic fixtures and realistic integration tests; CI on Windows + Linux. |
| NFR-03 | Type checking (mypy strict on core packages; TypeScript strict on frontend). |
| NFR-04 | Structured JSON logging with session and correlation IDs. |
| NFR-05 | Secrets only in local `.env` (git-ignored); none required for MVP except optional Anthropic API key. |
| NFR-06 | Reproducible environment (`uv.lock`, `pnpm-lock.yaml`). |
| NFR-07 | Data provenance: content hashes for datasets, run manifests, hash-chained event log. |
| NFR-08 | Resource budget: a typical laptop (8 GB RAM, ~20 GB free disk) must run the live app. |
| NFR-09 | Feature is "done" only with linked verification evidence; acceptance criteria never weakened silently. |

## 4. Explicit non-goals (MVP)
Discord, Telegram, cloud hosting, domains, live order execution, autonomous real-money trading,
mandatory paid services, monitoring while the app is stopped, order-book microstructure strategies
(no historical depth data is planned — see [RISKS_AND_COSTS](RISKS_AND_COSTS.md)).
