# A/B Evaluation Protocol — DRAFT v0

Status: **draft**. Must be agreed for both systems and frozen (hashed and committed) **before** the
comparison window starts. After freeze, changes require a new version and restart the window.

## 1. What is compared

1. **Trading systems** — prospective signal and paper-trading performance.
2. **Development processes** — Claude-only vs multi-model audited engineering.

This is a process comparison, not a perfectly controlled experiment: the second project benefits from
requirements learned in the first, and both teams' operators know market history. These caveats are
part of the final report.

## 2. Common ground (to be agreed jointly)

| Item | Proposal |
|---|---|
| Instruments | Intersection of both systems' eligible universes under one shared rule (DATA_ARCHITECTURE §6) |
| Markets | Spot; USDⓈ-M perps if both support them |
| Window | ≥ 30 calendar days prospective, UTC; extended until each system has ≥ 100 closed trades or a pre-set maximum |
| Capital | 10,000 USDT notional per system per book |
| Costs | Same fee schedule and slippage model parameters, documented in the protocol |
| Risk budget | Same per-trade risk, max exposure, daily loss and drawdown limits |
| Eligibility | Only signals created during each system's observed sessions; evaluation over the **intersection** of both systems' observation coverage, with each system's full results also reported |
| Isolation | No access to the other system's signals, code or data during the window |

## 3. Pre-outcome commitment

- Signals are written to the hash-chained event log at creation.
- Daily (and on shutdown), the system exports the day's events and records the chain head hash in a
  dated commit (`evidence/chain/YYYY-MM-DD.txt`). Pushing those commits daily gives an independent,
  timestamped record that predictions preceded outcomes. Optional: an OpenTimestamps proof of the head
  hash for a third-party timestamp at no cost.

## 4. Export format

JSON Lines + JSON Schema (`src/tsclaude/contracts/export/*.schema.json`), one file per record type:

- `signals.jsonl` — all fields of the signal record (SIGNALS_AND_AGENTS §2), `run_mode=LIVE` only.
- `transitions.jsonl` — every lifecycle event with timestamps.
- `fills.jsonl`, `positions.jsonl` — per book.
- `coverage.jsonl` — observed sessions and data gaps.
- `manifest.json` — system version, commit, protocol hash, chain head hash, export time.

The format is system-neutral so the other system can produce the same files, and a shared scorer can
evaluate both.

## 5. Metrics

Per system and per book, with block-bootstrap CIs: net return, max drawdown, win rate (with n),
profit factor, expectancy (R and %), Sharpe/Sortino (stated annualization), signal frequency,
abstention rate, signal latency (bar close → persisted), risk-limit violations (should be 0),
by-regime performance, exposure-adjusted return (per unit risk, per capital-day used).

Two views:
- **Matched:** signals on the same symbol/side within a tolerance window — paired comparison.
- **Full universe:** each system's complete behaviour, including how often it chooses not to trade.

Benchmarks: buy-and-hold of the common universe, and each system's own deterministic baseline.

## 6. Development effectiveness

Equivalent-scope features only (scope differences are listed explicitly): calendar time, human
hours, AI/subscription/API cost, tokens when available, number of tasks/PRs, defects by severity
(found in review, in CI, escaped), regressions, incidents, time to repair, test coverage of critical
modules, operational complexity (steps to install/start/recover).

## 7. Prohibited

Changing success criteria after the window starts; excluding periods post hoc; re-running the window
and reporting the better one; tuning either system using the other's signals.
