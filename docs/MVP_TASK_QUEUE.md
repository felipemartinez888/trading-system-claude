# MVP task queue (autonomous execution tracker)

Linked from [MVP_MASTER_PLAN](MVP_MASTER_PLAN.md). States: **READY / ACTIVE / VERIFIED / BLOCKED / DEFERRED**.
VERIFIED means: implemented, focused tests pass locally, CI green on the commit or a later one, and an
independent review where the task is research-critical. It never means "effective" or "accepted".
Owner decisions are in [DECISIONS_PENDING](DECISIONS_PENDING.md); a task that needs one is BLOCKED and
names it. Evidence for each row is the commit, the test file and the CI run listed.

Last update: 2026-11-10 (overnight session).

## P0: previous sequence (closure handoff Track A, Missions 2 and 3)

| ID | Task | Acceptance | State | Evidence | Owner dependency |
|---|---|---|---|---|---|
| A1 | Walk-forward fails closed on invalid runs (WF-1) | grade-X train ineligible; grade-X test voids; per fold | VERIFIED | `67bb18e`; CI `42766333677` 7/7; Fable review + causality audit | — |
| A2 | Warm-up-aware anchor (WF-2) | earlier anchors refused; every synthetic run integrity-clean | VERIFIED | `67bb18e`; same | — |
| A3 | Trial-registry run ids | `last_run_id == RunOutcome.run_id`; crash keeps provisional id | VERIFIED | `6f9ee18`, nonce/attach fix `6377cea`; CI `42770742512` 7/7 | — |
| A4 | README status, broken link | real status; 0 broken links | VERIFIED | `04307f5` | — |
| A5 | M3 acceptance proposals D-A, D-B | written as OPEN | VERIFIED (proposal only) | `04307f5`; [M3_ACCEPTANCE_REQUEST](M3_ACCEPTANCE_REQUEST.md) | **D-A, D-B** to decide |
| A6 | Owner dashboard smoke check | owner reports panels render | BLOCKED (owner PC) | `scripts/owner_checks/dashboard_smoke.ps1` | owner runs it |
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
| P1-2 | Walk-forward conformance to a frozen preregistration | any deviation refused before running, folds and config hash included | IMPLEMENTED, TESTED (re-review N2: folds and config were not pinned under `anchor: auto`; fixed `93846a0`) | `f10daad` `93846a0` | — |
| P1-3 | Trial-budget enforcement from the preregistration | registry-rows cap applied | IMPLEMENTED | `f10daad` | the counting rule is **D-C** |
| P1-4 | Research/live risk compatibility (I-1) | engine's own RR; regression test of the 1.5 floor vs ADR-0012 geometries | IMPLEMENTED, TESTED | `7ef4ba8`; `test_live_compat.py` (11) | wiring live rules into backtests: **D-E** |
| P1-5 | Research status lists walk-forward verdicts | invalid/insufficient shown; never "validation" | IMPLEMENTED, TESTED | `597ebf8` | — |
| P1-6 | Wire risk rules into backtests | research evaluates what live accepts | BLOCKED | — | **D-E** |
| P1-7 | Holdout guard integration of selection records | guard unlock via a selection record | BLOCKED: a record has no top-level `strategy_hash`, so the unchanged guard cannot accept it (review B1 fix) | `f10daad`, review fix | **D-D**, then an owner-approved guard change |
| P1-8 | Independent review of P1-1…P1-5 and M6 | Fable review findings fixed | VERIFIED: review fixes in CI `42780296915` 7/7; Fable re-review CHANGES REQUESTED (N2 folds not pinned, N5 unknown keys), fixed `93846a0` | `afcd1a2` `93846a0` | — |

## P2: hypotheses

| ID | Task | Acceptance | State | Evidence | Owner dependency |
|---|---|---|---|---|---|
| H1-a | H1 strategy scaffolding (draft registry, finite 1h trend, time exit, diagnose, live geometry) | causality canary; no trial possible | IMPLEMENTED, TESTED (45 + 10 + 2); causality audit CONDITIONAL PASS, findings fixed | `0030114` `fec95ba` `ba69590` `78ed753` | promotion to REGISTRY: owner, with the freeze |
| H1-b | Corrected H1-v2 preregistration draft (schema v1) | validates with only open owner decisions | VERIFIED as a draft: `prereg validate` ok, 24 configurations, open: D-C, D-D, D-E, DRAFT | `docs/research/H1-v2.preregistration.DRAFT.yaml` | every configuration is INCOMPATIBLE with the live floor 1.5 (**D-E**) |
| H1-c | Owner feasibility count on real data (15m vs 1h) | counts per symbol/year | BLOCKED (owner PC) | MVP_USER_GUIDE §5 | owner runs it |
| H1-d | H1 freeze | committed frozen file | BLOCKED | — | **D-C, D-E, D-F1**, owner freeze |
| H1-e | First H1 walk-forward | `--prereg H1-v2 --datasets …` | BLOCKED | — | H1-d |
| H2/H3 | Implement H2, H3 | as RESEARCH_PROTOCOL §8 | DEFERRED | — | after H1's protocol path is proven; no new hypothesis invented |

## P3: M5 agents (mocked)

| ID | Task | Acceptance | State | Evidence | Owner dependency |
|---|---|---|---|---|---|
| M5-1 | Agent package, mock client, schemas, budget, timeouts, mode off | every gate path tested with a mock | VERIFIED (mock scope): review fixes in CI `42780296915` 7/7; Fable re-review CHANGES REQUESTED (N1 three ledger corruptions raised; N3 mutants; N4 Windows lock), fixed `8200ffe` (CI `42786327337` 7/7); closure R3/R4 `1d253e2`, 157 passed locally | `78fdbd2`…`0605795`, `59b10b0` `494ae3c` `6bbb0e3` `8200ffe` | — |
| M5-3 | Wire the gate into the paper runtime; `agent_gated` book; Agent tab | gate called on CONFIRMED signals; book journaled and replayed | DEFERRED | — | needs new contract events (Lead) and a paper journal change; not done unattended overnight |
| M5-2 | Measured cost/latency with a real key on live signals | the gate's last clause | BLOCKED | — | API key + live signals (D-03; paid API needs the owner) |

## P4: M6 export (MVP part)

| ID | Task | Acceptance | State | Evidence | Owner dependency |
|---|---|---|---|---|---|
| M6-1 | JSONL export, schemas, verifier, independent scorer | scorer reproduces dashboard metrics from the export | IMPLEMENTED, TESTED locally (42 passed, 1 skipped) | `2d7191c` `088f3a3` `e63743b` | real-session export: owner |
| M6-2 | Protocol finalized with the other team | — | BLOCKED | — | D-06 (other team) |

## Owner-only (never done autonomously)
D-A M3 acceptance · D-B kill-switch reset · D-C trial counting · D-D two-stage preregistration · D-E live risk
floor · D-F1 SOL/universe · D-F2 holdout months · D-G lock files · D-H merge · any freeze, holdout unlock,
paid API, live trading.
