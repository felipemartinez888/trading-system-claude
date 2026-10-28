# Devlog — 2026-10-28 — MVP-4: first usable local dashboard (Paper + Backtests)

- **Branch:** `feat/mvp-4-dashboard`, cut from `3f03b46` (M3-C).
- **Approvals:** M4-A (brief), M4-B (implementation) and M4-C (local commit, after the owner's Windows smoke
  test). B-1 to B-3 were approved to plan and implemented because each was needed for a required page item.
  Merge, push and M4-D are **not** approved.
- **Status:** MVP readiness only. No M1–M4 acceptance is claimed. Paper results and backtests are not evidence
  of strategy performance.
- **Hardware:** focused tests only; results are provisional (WHEA under investigation).
- **Separate track:** the 2026-10-27 M2 data findings are in `2026-10-27-m2-handover-reconciliation.md`.
  Nothing in that track (the halt policy, AVAX/HBAR source conventions, zero-volume policy, dataset acceptance)
  was changed here.

## What was built
**Paper page** (`/paper/portfolio`, renamed "Paper"; polls every 2 s):
- **Runtime header:**
  - state chip and reason (failure states in red);
  - session, next recorded session, opened at, last processed bar, live bar since start;
  - engine state and fault ("not available" when there is no engine);
  - dropped feed updates, audit-log status and warnings, downtime backfill;
  - awaiting-missing minutes, and the previous run.
- **Always-visible MVP note**, and the backend `limitations` list (it includes that restart equivalence is not
  guaranteed).
- **Books** (baseline, shadow, benchmark, each with its role):
  - equity, cash, unrealized PnL, open positions, closed trades, max drawdown (% of peak equity);
  - halted and ledger-reconciled flags.
- **Positions:** quantity per instrument. Per-position entry and unrealized PnL are stated as not exposed.
- **Rejections:** fill-time counts by reason (book summary) and request-level rejections (B-2).
- **Closed trades (B-1):** flags for gap fill, ambiguous bar, spans a data gap, and exit resolved from backfill.
- **Fills.**
- **Reference strategy:**
  - the REFERENCE / NOT VALIDATED banner and badge;
  - CONFIRMED intent count, and WATCH proposals as a count only ("counted, never traded");
  - per-instrument warm-up progress with history restarts, the time of the last one, its phase and reason (B-3);
  - the CONFIRMED-only signal table with its own SIMULATED/STALE/UNAVAILABLE badge.
- **Operator action:** only "Record new paper session". It is token-protected, needs a note, shows backend
  refusals verbatim, and reports `new_session_id` and whether anything was created. There is no reset, recovery,
  kill-switch or order control.
- **Loading and failure states:**
  - Every section shows its own provenance badge.
  - Empty-state messages appear only for loaded data; loading and errors are shown as such.
  - Reasons always come from the backend, and absent values read "not available".

**Backtests page** (`/research/backtests`):
- **Empty state:** says runs are created by `tsclaude backtest run`, each counted as a statistical trial, never
  from the page. No run exists locally today.
- **Lists:** research runs and reference runs are listed separately.
- **Detail, reproducibility:**
  - strategy, version and params hash, symbols, period, segment, datasets, cost model, funding;
  - code version, runner, simulator and features versions, config hash, seed and its source, hypothesis and
    preregistration ids, manifest hash;
  - n_trials, recomputed, metrics verified.
- **Detail, results (as recorded):** trades, win rate and CI, expectancy (R and %), profit factor, net PnL, fees,
  slippage, cost share, max drawdown, exit reasons, flagged shares, p*, margin LCB, WATCH/CONFIRMED counts, missing
  minutes, grade reasons, and the raw cost stress, random control and integrity data.
- **Integrity and recomputed grades:**
  - A failed metrics hash shows a red warning, and the grade becomes UNVERIFIED.
  - When the grade was recomputed, the reasons are labelled as recorded at run time.
- **Nothing is computed in the browser** beyond formatting.

## Validation display (existing contracts only)
`ValidationBadge` applies, in order:
1. A REFERENCE label or the `bracket_reference` strategy gives **REFERENCE / NOT VALIDATED**, whatever the grade.
2. Backtest metrics with `metrics_verified ≠ true` give **UNVERIFIED (recorded grade …)**. This is an integrity
   rule, not a grading rule.
3. Otherwise the grade is shown with its RESEARCH_PROTOCOL §6 words: A VALIDATED (credible), B RESEARCH /
   CANDIDATE (promising), C INSUFFICIENT, D REJECTED, X INVALID. **Only A is called validated.**

## Proposal: grade "X" on live reference signals (decision for the owner; nothing changed)
- **The conflict:**
  - `contracts/signals.py:126` requires `evidence_grade: str` from A/B/C/D/X. A missing grade is impossible
    without a contract change (Lead-only).
  - MVP-3 emits "X" for reference signals, and §6 defines X as an integrity failure.
- **Recommended, contract-safe option:**
  - **Keep "X".** It is the most conservative value, and §7 rejects it automatically for live promotion
    (grade ≥ C is required).
  - **Keep the label-first rule:** reference records always carry `market_conditions.label = REFERENCE` and
    `validation = NOT_VALIDATED`.
  - Add a one-sentence clarification to RESEARCH_PROTOCOL §6: on a REFERENCE-labelled record, X means "no graded
    evidence; not usable as evidence". This is a docs change and needs approval.
  - The UI already applies the rule and never shows such records as "INVALID".
- **Alternative for M4, with an ADR:** a contract amendment, for example an explicit `validation_status` field, or
  allowing no grade for ungraded strategies. Not proposed for MVP-4.

## Backend additions (additive, read-only; no sim/, risk/, contracts/, research/, marketdata/ or store/ change)
- **B-1, closed trades:** `paper_runtime._publish` adds `trades`, the last 200 per book (`TradeRecord.to_payload`,
  exact Decimal strings), sorted by exit.
- **B-2, request-level rejections:** `_publish` adds `request_rejections`, the last 200 `sim_request_rejected`
  events.
  - `sim.events` is copied and scanned once per publish, together with fills.
- **New endpoint:** `GET /api/paper/activity?limit≤200` serves B-1 and B-2 with the same SIMULATED/STALE/UNAVAILABLE
  rules as books and fills.
- **B-3, per-instrument history breaks:** `ReferenceLiveStrategy.warmup_progress()` adds `history_breaks`,
  `last_break_detected_at_ms`, `last_break_phase` (warm-up/live) and `last_break_reason`.
  - Breaks are detected by comparing `StrategySource.breaks` before and after each bar; the count is per
    instrument because the aggregation is.
  - The result is not journaled and does not affect decisions.

## Files
- **Backend:**
  - `src/tsclaude/runtime/paper_runtime.py`
  - `src/tsclaude/api/paper.py`
  - `src/tsclaude/strategies/live_adapter.py`
- **Backend tests:**
  - `tests/integration/test_mvp4_paper_api.py` (new)
  - `tests/unit/strategies/test_live_adapter.py` (one test added)
- **Frontend:**
  - `api/types.ts`, `api/client.ts` (POST with a JSON body), `api/hooks.ts`
  - `components/ValidationBadge.tsx` (new) and its test
  - `pages/PaperPage.tsx` and `pages/BacktestsPage.tsx` (new), with tests
  - `navigation.ts`, `App.tsx`, `format.ts` (display helpers), `styles.css`
- **Docs:**
  - this devlog
  - `2026-10-27-m2-handover-reconciliation.md` (the separate M2 track, preserved from the scratchpad)
  - the `metrics.csv` row
- **Not touched:** data, evidence, holdout, dependencies, lockfiles, CI. `docs/ROADMAP.md` is still stale; it is
  not edited without approval.

## Tests (provisional; focused)
- **Backend:**
  - MVP-4 API, adapter, strategy, MVP-3, MVP-2.1, MVP-2, paper API, API core and server lifecycle: **109 passed**;
  - `ruff check --no-cache` clean; `ruff format --check` 207 files formatted; `mypy` (scratch cache) shows no
    issues in 114 files.
- **Frontend** (`node_modules/.bin`; pnpm is not on PATH here):
  - `tsc --noEmit` (strict) clean;
  - `vitest run`: **97 passed** in 10 files (all frontend tests, including 18 new page and badge tests);
  - `vite build` succeeded into a scratch folder, leaving the served `web/static` untouched. The bundle is
    501 kB, just over Vite's 500 kB warning threshold; not blocking.
- **Coverage of the new tests:**
  - disabled, failure and previous-run states;
  - STALE labels with the backend reason;
  - trades, both rejection kinds, warm-up and its break reason;
  - REFERENCE never shown as INVALID; WATCH never in the signal table;
  - only the new-session control; token gating; refusal and success messages;
  - no invented empty states on load errors; the MVP note shown when status is unavailable;
  - the backtests empty state, the reference/research split, the §6 grade words, an unverified grade A, real
    manifest keys, and unavailable metrics.

## Reviews
- **Code review: changes required, 3 blockers, all fixed:**
  - B1, the wrong manifest keys (`tool` instead of `code`) and missing reproducibility fields;
  - B2, unverified metrics could show VALIDATED;
  - B3, invented empty states while loading or on error.

  Non-blocking items fixed: the publish pass, the backfill flag, the recompute label, the SYSTEM badge, nullable
  request ids and keys, the truncation note, the success message, drawdown units, the helper location, and
  `aria-pressed`.
- **Provenance and safety audit: FAIL on one item, fixed.**
  - The finding: grade C was shown as "CANDIDATE"; it now uses the §6 word "INSUFFICIENT".
  - **Passed:** no order, reset or recovery path; no WATCH promotion; label-first reference display; backend
    labels kept; B-1 to B-3 read-only with no decision effect; limitations visible.
  - **Also corrected:** an overstated line in the M2 devlog (the 2023-03-24 hole is exactly 81 minutes in all
    five symbols; XRP's and SOL's extra minutes are separate gaps).
- **Re-check of the fixes by the code reviewer: approve.** B1–B3 are fixed and tested; no blocking item remains.
  Left as acceptable: after a failed refetch, the error shows beside the last-known values and their earlier
  provenance badge.

## Remaining risks and limitations
- **Windows check covered the default state only:** the owner's smoke test passed with paper disabled; RUNNING
  paper has not been checked in the real app.
- **No backtest run exists**, so the Backtests detail is tested only with fixtures.
- **Paper limitations are unchanged:** restart equivalence is not guaranteed, the reference warm-up resets on
  unobserved minutes, and the real 9-day first start is unmeasured.
- **Lists are capped:** the activity and fills lists hold the last 100–200 entries, and the page says so.
- **Grade X semantics** for reference signals await the owner decision above.
- **Double-clicking** "Record new paper session" can send two requests with different ids before the button
  disables. The backend refuses or records safely and never overwrites.

## Owner check (NORMAL PowerShell, no administrator)
**Result (owner, 2026-10-28): PASS.**
- **Paper:** the page exists and shows the disabled state as described.
- **Backtests:** the page exists and shows its empty state.
- No issues were observed.

Procedure:
Build the dashboard, start the app with `paper.yaml` unchanged (paper disabled), then open Paper and Backtests.
Expected:
- Paper shows DISABLED with its reason;
- Backtests shows the empty state.

Optionally, enable paper in a local copy of the config during a monitoring session and confirm the RUNNING,
STALE and SIMULATED labels.
