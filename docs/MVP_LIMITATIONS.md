# MVP limitations (stated, not hidden)

## Quantitative
- **NO VALIDATED STRATEGY.**
  - The only strategy is `bracket_reference`, a pipeline REFERENCE: not claimed profitable, never research evidence (D-14).
  - `GET /api/research/status` and the Backtests page say so, derived from evidence.
- **No preregistration exists, no hypothesis (H1–H4) is implemented, no real-data walk-forward has run.**
  - The walk-forward driver exists and is tested on synthetic data only.
  - Real data must be run on the owner PC (the lake lives there).
  - The expected outcome of the first real runs is `NO_EDGE_DEMONSTRATED`, and that is a valid result.
- **Trial accounting is conservative.** The registry counts every (configuration, window) evaluated, so walk-forward certifications use a larger trial count than the grid size.
- **Data scope** follows the M2 limited acceptance (D-22):
  - Spot 1m for 5 symbols, 2022–2025;
  - USD-M history and funding not imported;
  - Top-10 coverage incomplete;
  - B01–B12 deferred (D-15).
- **Costs** are the configured VIP0 model with fixed slippage; spreads before live capture are modeled, not observed.

## Paper runtime and recovery
- **Restart equivalence is not guaranteed** (P-1; seeds 13073 and 465923499). An entry can fill in a minute that a downtime overlaps.
- **An unobserved live minute restarts the reference warm-up** (about 7.8 days of history, reloaded from the store).
- **NB-1 residual clock risk** is accepted for the supervised paper-only MVP (D-21). It has no fixed error bound.
- **Clock sensitivity.**
  - A PC clock more than 1 s off Binance blocks all observation (fail closed).
  - The owner PC was 6.9 s off (2026-11-06) and 3.4 s off (2026-11-09) while Windows showed "synced".
  - Sync before each session (MVP_USER_GUIDE §2).
- **The strategy books have never been exercised in a real session.** The real 1 h replay (2026-11-06) traded the benchmark book only.

## Open defect under investigation
- **Native crash while recovering a session the PC restart ended** (2026-11-09, owner evidence run).
  - Exit code `0xC0000005` about 4 s after start, at `supervisor start: creating service`, while the live-bar store opened the copied database.
  - The same copy opens fine on Linux, and SQLite's integrity check passes.
  - Next: one retry with `PYTHONFAULTHANDLER=1` (MVP_VALIDATION_REPORT §7). Recovery after an unclean end stays unproven on Windows until then.
- **Status detail:** `RUNNING` is published from `_open()` until the first poll, even when that poll then turns `AWAITING_BACKFILL`. Nothing is processed in that window.

## Platform and environment
- **Owner PC restarts:** hardware (WHEA), open, out of scope here; one occurred during a paper session on 2026-11-09. Sessions survive by design, but such runs are interrupted.
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
