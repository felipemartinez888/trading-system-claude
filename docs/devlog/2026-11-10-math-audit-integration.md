# 2026-11-10: mathematical audit integration, P&L-free diagnostics, decision package

Lead session. Branch `task/mvp-integration`. Directive: "TSC — Consolidated MVP & mathematical research
integration". Nothing here freezes H1, records a trial, reads the holdout, changes a risk threshold or
merges a branch.

## What was done

| Commit | Change | Tests |
|---|---|---|
| `87ad417` | feasibility-v2: per-stage cumulative, marginal and leave-one-out counts (`stage_filter`); candidates and decision bars by UTC hour with the busiest-6-hour share (`by_utc_hour`); ablation-ladder counts declared per strategy (`ABLATION_LADDERS`). Still no outcome field: the no-outcome guard covers the new keys | `test_feasibility.py` (4 new) |
| `c750fe8` | `power_plan.py` and `tsclaude backtest power-plan`: planned minimum detectable win-rate margin per take-profit and n_eff ratio, from a feasibility count, under alpha / registry rows; walk-forward report gains `exit_mix` (time-exit share per phase and configuration) and `power_reading` (realized p*, n_eff, margin). Certification, selection and the verdict logic are unchanged | `test_power_plan.py` (8), rehearsal asserts the new keys |
| `9126512` | `docs/research/MATHEMATICAL_STRATEGY_FOUNDATIONS.md` (the audit, verbatim, with a reconciliation part), `docs/research/H1_DECISION_PACKAGE.md`, `tests/unit/research/test_math_foundations_numbers.py` | 8 number-pinning tests |
| this commit | Review fixes (below); readiness assessment and owner NORMAL PowerShell steps (package §7–§8); tracker, roadmap, report, metrics | `test_feasibility.py` 27, `test_power_plan.py` 12, rehearsal strengthened |

Follow-up while CI ran: the research status API (`walk_forward_summaries`) and the dashboard's
walk-forward table show each report's `min_detectable_margin`, with the note that a verdict
without certification does not rule out a smaller margin (audit §9 item 4). Backend test in
`test_research_status.py`; the frontend test is in `ResearchStatusBanner.test.tsx` and runs in CI
only (no node_modules in this container; a strict tsc pass on the changed files shows no error
beyond the missing packages).

## Reconciliation in short

- Audited revision `1cc2693`; reconciled against `2b63d4c`. Every implementation claim was checked against the code at
  HEAD; every derived number is reproduced by a test.
- One number corrected: the n_min ratio between the 2R and 3R geometries is about 2.0, not 1.9 (423 vs 215 trades at
  margin 0.05, alpha 0.05).
- Five qualifications. The most important one: the net RR ≥ 1.5 floor bounds the reward-to-risk ratio. It does not
  bound the win rate, and it does not by itself exclude high-win-rate strategies. R-1 is reworded to match.
- The audit's recommendations are classed: F2/F3/F4 implemented as diagnostics, F1 partly, F5 is a preregistration
  statement for the owner, R-1..R-8 open, the I-items deferred.

## What was deliberately not done

- No change to the official selection objective, the certification test, the grid or any accepted protocol.
- No conditional-control ablation (the ladder gives counts only; the controlled version is a proposal in the package §5).
- No econometric or ML infrastructure from the audit's "later" list.
- No H2/H3 work.

## Evidence

- Before the review fixes: `tests/unit/backtest tests/unit/research tests/unit/strategies
  tests/unit/evaluation` 604 passed, 2 skipped (pyarrow, fastapi), exit 0.
- After the fixes: feasibility 27 passed, power plan 12, number pinning 8, the rehearsal passes with
  the stronger exit-mix assertions. Full unit suite on `0fdf1e1`: 1941 passed, 2 skipped, 16 failed.
  The 16 are the known environment-only failures (11 async tests without pytest-asyncio, 5 without
  httpx; CI has both). Before this session: 1901 passed with the same 16.
- `ruff check`, `ruff format --check` (src, tests, scripts) and `mypy --strict` on the changed modules
  are clean (apart from the known missing yaml stubs).
- Mutants: the reviewer ran 17 (13 killed, 4 survived). After the fixes all four are killed, plus four
  new ones on the fix code: no month clipping, no prior family rows, no per-rung hour count, and a
  constant floor flag. The month-clipping mutant survived the first version of the new tests; a
  mid-month case now kills it.

## Review

Fable reviewer (fresh context): **CONDITIONAL PASS, 0 blocking.** Nothing reaches selection or
certification; every recomputed number matches (take-profit table, R-4, n_min, cost invariance).

| Finding | Fix |
|---|---|
| M1 The hour-of-day output was effectively untested: four mutants survived on a uniform histogram | Direct `utc_hour` tests on 15m and 1h close times, including `hh:59:59.999`; non-uniform `busiest_share`; a candidate share that differs between the first and last stage; window clipping in `oos_share_of` |
| M2 `oos_candidates_upper_bound` assumed candidates spread evenly over the range, so it was not a bound; prior family rows were not in the planned alpha | Feasibility now records `candidates_by_utc_month`; the plan counts candidates inside the test windows (partial months pro rata) and states its basis (`oos_candidates`, `oos_candidates_basis`). Old counts fall back to the time share, labelled as an even-spread estimate. `prior_family_rows` (CLI `--prior-family-rows`) enters `n_trials_planned`. power-plan-v2 |
| M3 The package claimed per-rung counts by hour, which did not exist | Added (`ablation_ladder[].by_utc_hour`, `busiest_hours_share`); the package wording names the fields |
| M4 `_exit_mix` had no guard and `_power_reading` caught too little | Both catch any exception into an `error` entry; tests for a malformed metrics shape and an `OverflowError`; the rehearsal now checks the real run metrics (reasons are `ExitReason` values, sums match, one run per fold) |
| Minor 1 Leave-one-out is degenerate for warm-up, features and stop | Stated in the module docstring and the output notes |
| Minor 2–4 "bind", freshness scope, floor timing | Reworded in Part 1 §1.3 (d) and §1.3 |
| Minor 5 `read_power` did not flag small n | `below_certification_floor` added |
| Minor 6 Authorization row not traceable to DECISIONS_PENDING | The row now says it is an engineering instruction, not a D-decision |
| Minor 7 Part 2 "unchanged" not confirmable | Checked against the Project original: same 6 links, 31 headings, spot-checked lines; the note now says so and names the source path |
| Minor 8 Tracker not updated | Done in this commit |
| Minor 9 Owner steps assume a push | The steps say so and print the checked-out commit |
| Minor 10 `WALKFORWARD_VERSION` not bumped | **Declined.** The version feeds the plan identity and the certification bootstrap seed. The two report keys are additive and change no computed value, so a bump would change verdict draws for no reason |
| Minor 11 Odd set expression | Simplified |
