# MVP limitations (stated, not hidden)

## Quantitative
- **NO VALIDATED STRATEGY.**
  - The only strategy is `bracket_reference`, a pipeline REFERENCE: not claimed profitable, never research evidence (D-14).
  - `GET /api/research/status` and the Backtests page say so, derived from evidence.
- **No preregistration exists, no hypothesis (H1–H4) is implemented, no real-data walk-forward has run.**
  - The walk-forward driver exists and is tested on synthetic data only.
  - Real data must be run on the owner PC (the lake lives there).
  - The expected outcome of the first real runs is `NO_EDGE_DEMONSTRATED`, and that is a valid result.
- **The walk-forward tool is not safe on real data yet** ([MVP_CLOSURE_HANDOFF](MVP_CLOSURE_HANDOFF.md) §4): it uses runs graded X, and its default start (2022-01-01) leaves no warm-up, which makes every training run grade X. Fix both before any real-data walk-forward.
- **Backtests apply no risk gate** (`allow_all`), while the live baseline book requires net reward/risk ≥ 1.5. Research can validate trades that live would reject (handoff §6, I-1).
- **Trial accounting is conservative.** The registry counts every (configuration, window) evaluated, so walk-forward certifications use a larger trial count than the grid size.
- **Data scope** follows the M2 limited acceptance (D-22):
  - Spot 1m for 5 symbols, 2022–2025;
  - USD-M history and funding not imported;
  - Top-10 coverage incomplete;
  - B01–B12 deferred (D-15).
- **Costs** are the configured VIP0 model with fixed slippage; spreads before live capture are modeled, not observed.
- **Trial registry run ids** do not match run directories (pre-existing, `research/runner.py:903` vs `:1020`). Counting is unaffected; the fix is in MVP_VALIDATION_REPORT §9.
- **The reference strategy traded 0 times** in real-data BTC 2024-Q1 with default parameters. It is a pipeline reference, not a research subject.

## Paper runtime and recovery
- **Restart equivalence is not guaranteed** (P-1; seeds 13073 and 465923499). An entry can fill in a minute that a downtime overlaps.
- **An unobserved live minute restarts the reference warm-up** (about 7.8 days of history, reloaded from the store).
- **NB-1 residual clock risk** is accepted for the supervised paper-only MVP (D-21). It has no fixed error bound.
- **Clock sensitivity.**
  - A PC clock more than 1 s off Binance blocks all observation (fail closed).
  - The owner PC was 6.9 s off (2026-11-06) and 3.4 s off (2026-11-09) while Windows showed "synced".
  - Sync before each session (MVP_USER_GUIDE §2).
- **The strategy books have never been exercised in a real session.**
  - Three real sessions traded the benchmark book only: 2026-11-06 1 h, 2026-11-09 25 min, and the 2026-11-09 recovery run.
  - The reference strategy was warmed up and evaluating, but produced no signal. Its conditions (compression + breakout + volume) are rare by design.
  - The deterministic Target A tests cover signal → risk → fills.

## Platform instability seen during validation (owner PC, 2026-11-09)
- **Three events in about 10 hours:**
  - a PC restart during a paper session (13:07);
  - a native access violation (`0xC0000005`) in 1 of 2 recovery attempts on identical input, not reproduced on retry with faulthandler;
  - a `TypeError` that correct execution cannot raise, on a hot path that otherwise succeeded thousands of times.
- **Reading:** consistent with the open hardware (WHEA) investigation, not proven.
- **The software held up:** it failed closed, kept the hash chain intact, recorded clean stops where it could, and recovered (MVP_VALIDATION_REPORT §7).
- **Consequence:** long unattended runs on this machine are not reliable yet. Keep sessions supervised.
- **Status detail:** `RUNNING` is published from `_open()` until the first poll, even when that poll then turns `AWAITING_BACKFILL`. Nothing is processed in that window.

## Platform and environment
- **Owner PC hardware (WHEA):** open, out of scope here; see the platform-instability section above.
- **CI flakes:** the paper-runtime timing flakes are root-caused and fixed in tests (validation report §6).
  - One timing test still asserts real-time gaps (`test_retries_back_off_and_are_capped`, ≤ 0.31 s).
  - Loosening it is a Lead decision, so it can still fail on a stalled runner.
- **No local Python test run of fastapi/pyarrow/hypothesis tests in the agent container:** package installs are blocked there, so CI is the authoritative run.
- **Not formally accepted:** the M1 2-hour gate and M0 acceptance.
- **Not implemented** (outside the engineering MVP, Target C):
  - M5 agents;
  - M6 export;
  - Discord;
  - analytics pages beyond the six shown.

## Not adopted
- **Graphify: REJECT for the MVP**, on the owner's pilot data. About 2.1× more context tokens than `grep`, with equal or lower recall ([MVP_MASTER_PLAN](MVP_MASTER_PLAN.md) §7).
- **Rust:** deferred; no measured bottleneck.
