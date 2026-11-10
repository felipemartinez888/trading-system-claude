# MVP task queue (autonomous execution tracker)

Linked from [MVP_MASTER_PLAN](MVP_MASTER_PLAN.md). States: **READY / ACTIVE / VERIFIED / BLOCKED / DEFERRED**.
VERIFIED means: implemented, focused tests pass locally, CI green on the commit or a later one, and an
independent review where the task is research-critical. It never means "effective" or "accepted".
Owner decisions are in [DECISIONS_PENDING](DECISIONS_PENDING.md); a task that needs one is BLOCKED and
names it. Evidence for each row is the commit, the test file and the CI run listed.

Last update: 2026-11-10, resume session after the owner's smoke check at `0955749`:
- pnpm 12 build approval;
- tested H1-1 run sheet;
- H1 decision record;
- M5 export format v2 on its branch;
- export event-coverage guard.

CI on `f6ce164`: `42854101526` 7/7; on `0955749`: `42859002211` 7/7. Later runs are in
[MVP_OVERNIGHT_REPORT](MVP_OVERNIGHT_REPORT.md).

## P0: previous sequence (closure handoff Track A, Missions 2 and 3)

| ID | Task | Acceptance | State | Evidence | Owner dependency |
|---|---|---|---|---|---|
| A1 | Walk-forward fails closed on invalid runs (WF-1) | grade-X train ineligible; grade-X test voids; per fold | VERIFIED | `67bb18e`; CI `42766333677` 7/7; Fable review + causality audit | — |
| A2 | Warm-up-aware anchor (WF-2) | earlier anchors refused; every synthetic run integrity-clean | VERIFIED | `67bb18e`; same | — |
| A3 | Trial-registry run ids | `last_run_id == RunOutcome.run_id`; crash keeps provisional id | VERIFIED | `6f9ee18`, nonce/attach fix `6377cea`; CI `42770742512` 7/7 | — |
| A4 | README status, broken link | real status; 0 broken links | VERIFIED | `04307f5` | — |
| A5 | M3 acceptance proposals D-A, D-B | written as OPEN | VERIFIED (proposal only) | `04307f5`; [M3_ACCEPTANCE_REQUEST](M3_ACCEPTANCE_REQUEST.md) | **D-A, D-B** to decide |
| A6 | Owner dashboard smoke check | owner reports panels render | BLOCKED (visual confirmation only). Owner ran it at `0955749` on Windows: all 7 automated checks PASS (bundle_built, app_started, research/paper/agents status APIs, dashboard_served, clean_quit); status `NO_VALIDATED_STRATEGY`, Paper disabled, agents OFF. The build first needed a manual esbuild approval under pnpm 12, now fixed (PL-1) | owner report 2026-11-10; `scripts/owner_checks/dashboard_smoke.ps1` | owner confirms the panels visually |
| A7 | Reviews + CI for A1–A3 | both green | VERIFIED | MVP_VALIDATION_REPORT §12 | — |
| M2-1 | Verified max-loss cache (WF-4) | identical hashes hit vs none; fail-closed key | VERIFIED | `6377cea`, `0a87c27`; `test_max_loss_cache.py`; Fable final review | — |
| M2-2 | Resumable walk-forward (WF-4) | interrupted == uninterrupted; untrusted record refuses | VERIFIED | `fcc5f99`, `0a87c27`; `test_walkforward_resume.py` (20) | — |
| M2-3 | `--datasets`, `--prereg` (WF-6) | refused before anything runs; recorded | VERIFIED | `fcc5f99` | — |
| M2-4 | Validity checks, coverage floor, budget projection | void on failure; floor governed | VERIFIED | `fcc5f99`, `0a87c27` | — |
| M2-5 | Final selection mechanism (WF-7) | after positive verdict only; PENDING GOVERNANCE | VERIFIED (mechanism) | `fcc5f99` | use needs **D-D** |
| M2-6 | P&L-free feasibility count (H1-1) | no outcome field; dev only; no trial | VERIFIED (tool) | `b90ebda`, `8bdc844`; `test_feasibility.py` | real-data run: owner PC |
| M3-1 | Docs, smoke command, final report | — | VERIFIED | `9ef847a`, `08f8953` | — |

## P1: M4 research infrastructure

| ID | Task | Acceptance | State | Evidence | Owner dependency |
|---|---|---|---|---|---|
| P1-1 | Preregistration schema, validate, freeze, selection records, template | policy fields PENDING; freeze refuses open decisions and edits after freeze; a frozen file pins anchor and backtest config; unknown keys refused | VERIFIED (CI `42786327337` 7/7 incl. re-review N2/N5 fixes `93846a0`; closure R1/R2 `1d253e2`) | `f10daad` `afcd1a2` `93846a0`; `test_preregistration.py` | freezing a hypothesis: owner |
| P1-2 | Walk-forward conformance to a frozen preregistration | any deviation refused before running, folds and config hash included | VERIFIED: N2 fix `93846a0` and closure R1 `1d253e2` in CI `42791493309` 7/7; closure review CONDITIONAL PASS, conditions fixed | `f10daad` `93846a0` `1d253e2` | — |
| P1-3 | Trial-budget enforcement from the preregistration | registry-rows cap applied | IMPLEMENTED, TESTED | `f10daad`; H1 as drafted: up to 199 registry rows (brief §2) | the counting rule is **D-C** |
| P1-4 | Research/live risk compatibility (I-1) | engine's own RR; regression test of the 1.5 floor vs ADR-0012 geometries | IMPLEMENTED, TESTED; enforced at validate/freeze when the floor is kept (`5ea287e`) | `7ef4ba8` `5ea287e`; `test_live_compat.py`, `test_preregistration.py` | — |
| P1-5 | Research status lists walk-forward verdicts | invalid/insufficient shown; never "validation" | IMPLEMENTED, TESTED | `597ebf8` | — |
| P1-6 | Wire risk rules into backtests | research evaluates what live accepts | IMPLEMENTED, TESTED (20 tests: verdict parity with the paper engine, view parity with the paper tracker, freshness from bars, no look-ahead, kill switch and base costs in every book, fail closed, end to end); Fable review CONDITIONAL PASS 0 blocking, F1–F6/F8–F10 fixed (F6 by a run-side check, `b7abde7`), F7 minor open; VERIFIED in CI `42829517341` 7/7 on `0429135` | `9f2cabc` + review fixes; `test_backtest_risk_policy.py`; [devlog](devlog/2026-11-10-m4-p1-6-risk-policy.md) | unblocked by the owner direction 2026-11-10 (keep the floor); D-E's formal record stays the owner's |
| P1-7 | Holdout guard integration of selection records | guard unlock via a selection record | BLOCKED: a record has no top-level `strategy_hash`, so the unchanged guard cannot accept it (review B1 fix) | `f10daad`, review fix | **D-D**, then an owner-approved guard change |
| P1-8 | Independent review of P1-1…P1-5 and M6 | Fable review findings fixed | VERIFIED: review fixes in CI `42780296915` 7/7; Fable re-review CHANGES REQUESTED (N2 folds not pinned, N5 unknown keys), fixed `93846a0` | `afcd1a2` `93846a0` | — |
| P1-9 | P&L-free feasibility diagnostics (audit F2/F3, F4 counts) | per-stage cumulative, marginal and leave-one-out counts; candidates by UTC hour (overall, per symbol, per ablation rung) with the busiest-6-hour share; month totals; ablation-ladder counts; no outcome field; no trial; selection untouched | VERIFIED (CI `42854101526` 7/7 on `f6ce164`); was IMPLEMENTED, TESTED (feasibility 27 passed; ladder's last rung equals the confirmed count); Fable review CONDITIONAL PASS 0 blocking, M1/M3 fixed (hour tests, per-rung hours) | `87ad417` + review fixes; `test_feasibility.py` | real-data reading: H1-c |
| P1-10 | Power planning; walk-forward exit mix and power reading (audit F1 part, A7) | candidates inside the test windows from month totals (basis stated), prior family rows in the planned alpha, minimum detectable margin per take-profit and n_eff ratio; report gains `exit_mix` (time-exit share) and `power_reading`; certification and selection unchanged | VERIFIED (CI `42854101526` 7/7 on `f6ce164`; the status-API addition `1e2339b`: CI `42859002211` 7/7 on `0955749`); IMPLEMENTED, TESTED (12 power-plan tests pin margin 0.05 at 1804 trades and 0.10 at 452 under alpha/199; the rehearsal checks the exit mix against the real run metrics); review M2/M4 fixed. The research status API and the dashboard's walk-forward table show each report's detectable margin (backend tested; the frontend test runs in CI only, no node_modules here) | `c750fe8` + review fixes + `research/status.py`; `test_power_plan.py`, `test_backtest_risk_policy.py`, `test_research_status.py`, `ResearchStatusBanner.test.tsx` | — |
| P1-11 | Integrated mathematical audit, reconciliation, decision package, H1 readiness | every [I] claim checked at HEAD, every [D] number reproduced by a test; floor qualifications written; D-C/D-E/D-F1/D-G and R-1..R-8 packaged; authorized vs proposed separated | IMPLEMENTED, TESTED (8 number-pinning tests); review wording findings fixed, Part 2 checked against the Project original | `9126512` + readiness §7–§8; [MATHEMATICAL_STRATEGY_FOUNDATIONS](research/MATHEMATICAL_STRATEGY_FOUNDATIONS.md), [H1_DECISION_PACKAGE](research/H1_DECISION_PACKAGE.md), `test_math_foundations_numbers.py` | the decisions themselves: owner |
| P1-12 | Tested owner run sheet for H1-1 | every documented `tsclaude` command parses with the real CLI; count and power plans rehearsed as written; dataset identity and evidence capture steps; no result invented | IMPLEMENTED, TESTED (`test_owner_feasibility_run.py` 3; power-plan `--out` refusal tested) | `c14769c`; [H1_FEASIBILITY_RUN](research/H1_FEASIBILITY_RUN.md) | owner runs it (H1-c) |
| PL-1 | pnpm 9 through 12 build approval for esbuild | one workspace file read by pnpm 9 (`packages`) and by 10.26+ and 11/12 (`allowBuilds`); package.json kept for 10.0–10.25; no dependency, version or lockfile change | IMPLEMENTED, TESTED (`test_frontend_toolchain.py` 2; pnpm 10.28 maps the file to `onlyBuiltDependencies ["esbuild"]`; pnpm 9 via CI; pnpm 12 not runnable here, owner check in the run sheet §1) | `6fd2fd7`; [frontend/README](../frontend/README.md) | lock file and pnpm pin: **D-G** |

## P2: hypotheses

| ID | Task | Acceptance | State | Evidence | Owner dependency |
|---|---|---|---|---|---|
| H1-a | H1 strategy scaffolding (draft registry, finite 1h trend, time exit, diagnose, live geometry) | causality canary; no trial possible | IMPLEMENTED, TESTED (45 + 10 + 2); causality audit CONDITIONAL PASS, findings fixed | `0030114` `fec95ba` `ba69590` `78ed753` | promotion to REGISTRY: owner, with the freeze |
| H1-b | Corrected H1-v2 preregistration draft (schema v1) | validates with only open owner decisions | VERIFIED as a draft; **superseded for review by H1-f** (every v2 configuration is INCOMPATIBLE with the kept floor: 24 problems once D-E is set) | `docs/research/H1-v2.preregistration.DRAFT.yaml` | — |
| H1-f | Live-compatible H1-v3 draft (take-profits 2, 2.5, 3 R; all else v2) | every configuration COMPATIBLE with the 1.5 floor; validates with only open owner items | IMPLEMENTED: `prereg validate` ok, 24 configurations; with D-E set to keep the floor, 0 problems (v2: 24) | `9c681ca`; [H1_DECISION_BRIEF](research/H1_DECISION_BRIEF.md) | owner review; ADR-0012 Amendment 6 (proposed) |
| H1-c | Owner feasibility count on real data (15m vs 1h) | counts per symbol/year, with the P1-9 diagnostics and a power plan per take-profit set | BLOCKED (owner PC) | exact NORMAL PowerShell steps: [H1_DECISION_PACKAGE](research/H1_DECISION_PACKAGE.md) §8 | owner runs it |
| H1-d | H1 freeze | committed frozen file | BLOCKED | sequence in the brief §5 and package §6–§7; blank owner record [H1_DECISION_RECORD](research/H1_DECISION_RECORD.md) (`f7a957d`) | **D-C, D-E (+ Amendment 6), D-F1**, D-G recommended, owner freeze |
| H1-e | First H1 walk-forward | `--prereg <frozen H1-v3> --datasets …`; OOS verdict with power reading | BLOCKED | readiness: [H1_DECISION_PACKAGE](research/H1_DECISION_PACKAGE.md) §7 | H1-d |
| H2/H3 | Implement H2, H3 | as RESEARCH_PROTOCOL §8 | DEFERRED | — | after H1's protocol path is proven; no new hypothesis invented |

## P3: M5 agents (mocked)

| ID | Task | Acceptance | State | Evidence | Owner dependency |
|---|---|---|---|---|---|
| M5-1 | Agent package, mock client, schemas, budget, timeouts, mode off | every gate path tested with a mock | VERIFIED (mock scope): review fixes in CI `42780296915` 7/7; Fable re-review CHANGES REQUESTED (N1 three ledger corruptions raised; N3 mutants; N4 Windows lock), fixed `8200ffe` (CI `42786327337` 7/7); closure R3/R4 `1d253e2`, 157 passed locally | `78fdbd2`…`0605795`, `59b10b0` `494ae3c` `6bbb0e3` `8200ffe` | — |
| M5-3 | Wire the gate into the paper runtime; `agent_gated` book; Agent tab | gate called on CONFIRMED signals; book journaled and replayed | IMPLEMENTED, TESTED on the separate branch `task/m5-runtime-wiring` (pushed, **not merged**, so the D-A replay evidence on this branch is untouched). 60 new tests; mode off byte-identical to `9c681ca` (reproduced independently); Fable review CONDITIONAL PASS, 0 blocking (F3, F4 fixed in `ee0e2a4`; F1 recorded as operational rule L-13: replay the D-A sessions with `agents.yaml` mode off; F2 closed: the fastapi status test ran and passed in CI `42838323339`, 7/7 on `97168b2`, python ubuntu 2356 passed, windows 2354); Lead accepted the additive contract events (L-13). Not done: real adapter, Agent tab, signal projection fields | `cf8cfe9`…`97168b2`; devlog `2026-11-10-m5-runtime-wiring.md` on that branch Integration session: export format v2 fixes a defect, since no agent_gated session could be exported to M6 (`48ed4d7`, `f5c2538`, 8 + 3 tests, v1 exports still checkable). Mode-off identity re-verified on `127e445` against `9c681ca` with each tree's own code | owner: 4 design questions, now as [M5_RUNTIME_DECISIONS](M5_RUNTIME_DECISIONS.md) (mode change needs a new session; restart fail-closed; head-of-line coverage; downgrade = block). Merge: D-H |
| M5-2 | Measured cost/latency with a real key on live signals | the gate's last clause | BLOCKED | — | API key + live signals (D-03; paid API needs the owner) |

## P4: M6 export (MVP part)

| ID | Task | Acceptance | State | Evidence | Owner dependency |
|---|---|---|---|---|---|
| M6-1 | JSONL export, schemas, verifier, independent scorer | scorer reproduces dashboard metrics from the export | VERIFIED: CI 7/7 (`42776226802` onward); three real recorded sessions exported and verified, 9/9 session-end ledger hashes reproduced from the export alone (M6 devlog) | `2d7191c` `088f3a3` `e63743b` `ecc1090` | — |
| M6-2 | Protocol finalized with the other team | — | BLOCKED | 12 interface questions with TSC proposals: [M6_D06_INTERFACE_QUESTIONS](M6_D06_INTERFACE_QUESTIONS.md) (`94e821e`) | D-06 (other team) |
| M6-3 | Every contract event type has a deliberate place in the export | a new `EventType` fails a test until it is mapped or listed as not exported; schemas accept exactly their file's types | IMPLEMENTED, TESTED (2 tests; against the 9c681ca exporter with the M5-3 contract it fails on the three agent types) | `94e821e`; `test_eval_event_coverage.py` (also on the M5 branch) | — |

## Owner-only (never done autonomously)
D-A M3 acceptance · D-B kill-switch reset · D-C trial counting · D-D two-stage preregistration · D-E live risk
floor · D-F1 SOL/universe · D-F2 holdout months · D-G lock files · D-H merge · any freeze, holdout unlock,
paid API, live trading.
