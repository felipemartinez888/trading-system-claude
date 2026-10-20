# Risks and Costs

## 1. Major risks

| # | Risk | L | I | Mitigation |
|---|---|---|---|---|
| R1 | **No net edge exists** in the tested hypotheses after costs — the most likely outcome for simple intraday strategies on liquid crypto. | High | High | Treated as a valid result; grading, random-entry controls and honest reports instead of tuning until something "works". |
| R2 | Backtest/live divergence (features, fills, timing). | Med | High | One simulator two clocks, window-parity and replay-parity tests, measured live latency fed into backtests. |
| R3 | LLM agents "know" history from training data → inflated agent backtests. | High | Med | Masked packets; agent value credited only on post-cutoff forward data. |
| R4 | Binance API churn (e.g. 2026 futures WS split, endpoint retirements), outages, rate-limit bans. | High | Med | Limits read at runtime, header-driven rate limiting, weekly drift check, recorded-fixture tests, clear health alarms. |
| R5 | Regional or terms restrictions on Binance futures/spot for the owner's location or network. | Med | High | M0 reachability spike from the owner's machine; futures paper positions gated by decision D-02; spot-only fallback. |
| R6 | Laptop sleep/shutdown → sparse observation coverage → biased forward sample. | High | Med | Coverage tracked and reported; A/B on coverage intersection; Windows "prevent sleep while monitoring" option; mini-PC later. |
| R7 | No historical spread/depth data → slippage modeled, not observed. | Certain | Med | Pessimistic defaults, calibration from live capture, cost stress tests, explicit labels. |
| R8 | Survivorship bias in the universe. | Certain | Low–Med | Documented; point-in-time universe on backlog. |
| R9 | Mono-model review shares blind spots with the builder. | Med | Med | Fresh-context reviewers, adversarial causality audits, deterministic test gates that do not depend on any model's judgment. |
| R10 | Windows specifics: event loop, file locking (DuckDB/Parquet while open), long paths, AV scanning data folders, clock drift. | Med | Med | Windows CI from M0, server-time offset monitoring, single-writer storage design. |
| R11 | Scope creep (15+ pages, many strategies, many agents). | High | Med | Vertical slices, gates, one timeframe family optimized at a time. |
| R12 | Development workspace can't reach Binance → live behaviour verified only on the owner's machine. | Certain | Med | Recorded fixtures for CI; decision D-01 (allowlist domains and/or owner-run verification scripts). |
| R13 | Evidence tampering doubts in the A/B comparison. | Low | High | Hash-chained events, daily chain-head commits, optional OpenTimestamps. |

## 2. Costs

### Infrastructure (target: $0 recurring)

| Item | Cost | Notes |
|---|---|---|
| Binance public market data | $0 | Rate limits and availability apply. |
| Python, DuckDB, SQLite, Polars, FastAPI, React, charts | $0 | Open source (lightweight-charts requires attribution). |
| Storage | $0 marginal | ~2–5 GB for 1m bars of 10–20 symbols × 2 markets × 4 years, plus live captures (~tens of MB/day). |
| Hosting / domain / notifications | $0 | Not used in MVP. |
| Electricity, internet, the PC being on | not zero | Owner's existing resources; 24/7 operation is not promised. |
| GitHub | $0 | Private repo + Actions minutes within free allowance (Windows runners consume minutes at a higher multiplier — CI kept lean). |

### AI costs (reported separately)

| Item | Cost | Notes |
|---|---|---|
| Development with Claude (this work) | Owner's existing Claude plan | Usage limits of the plan apply; tracked in devlog where visible. |
| Runtime signal agents (optional) | ≈ $0.002 – $0.07 per reviewed signal depending on model; default cap $1/day | Requires an Anthropic API key, billed separately from the Claude subscription. See SIGNALS_AND_AGENTS §5. |
| Research-mode reviews (optional) | Batch API at 50 % of list price | Indicative only (training-data contamination). |
