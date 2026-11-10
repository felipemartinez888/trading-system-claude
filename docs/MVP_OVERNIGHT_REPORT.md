# MVP overnight report (2026-11-10)

Updated incrementally during the session. Status words: IMPLEMENTED / TESTED / VERIFIED / ACCEPTED /
BLOCKED. A completed implementation is never validated effectiveness. Tracker: [MVP_TASK_QUEUE](MVP_TASK_QUEUE.md).
Map: [MVP_SEMANTIC_MAP](MVP_SEMANTIC_MAP.md).

## Branch and HEAD
- Branch `task/mvp-integration` (pushed under D-07, **not merged**). Worktree `/home/claude/trading-system-claude`
  (cloud workspace; the owner's PC was not written to).
- Start of the overnight directive: `08f8953` (end of the Track A / Mission 2 / Mission 3 sequence).
- Final code HEAD: `2fa8bbc` (CI `42791493309` 7/7); a docs-only commit after it records this CI result.
- Agent worktrees (local only): `wt-m5` (`task/m5-agents`), `wt-m6` (`task/m6-export`, integrated),
  `wt-h1` (`task/m4-h1-prep`), `wt-feas` (`task/m4-feasibility`, integrated).

## Completed before the overnight directive (same session)
| Work | Commits | Evidence |
|---|---|---|
| Track A: WF-1, WF-2, trial run ids, README/link, D-A/D-B proposals | `67bb18e` `6f9ee18` `04307f5` | CI `42766333677` 7/7 (ubuntu 1902 passed, windows 1900); Fable review APPROVE WITH NOTES; causality audit CONDITIONAL PASS; findings fixed |
| M4 infrastructure: max-loss cache, resume, pins, prereg ids, validity checks, budget, coverage floor, final selection, feasibility count | `6377cea` `fcc5f99` `b90ebda` `0921e9c` `8bdc844` `9ef847a` | CI `42770742512` 7/7 (ubuntu 1965, windows 1963) |
| Final Fable review of the M4 infrastructure: CHANGES REQUESTED, 4 major | fixes `0a87c27` `08f8953` | local: 404 passed, 1 skipped (pyarrow) across backtest, research, strategies and the import guard; resume suite 20 passed |

## Overnight work
| ID | Work | Commits | Tests | State |
|---|---|---|---|---|
| P1-1/2/3 | Preregistration schema `tsc-prereg-v1`, `tsclaude prereg validate/freeze/select`, TEMPLATE, walk-forward conformance, budget from the file | `f10daad` | `test_preregistration.py` 30 passed | IMPLEMENTED, TESTED; CI pending; review pending |
| P1-4 | Research/live risk compatibility (engine's own RR) | `7ef4ba8` | `test_live_compat.py` 11 passed | IMPLEMENTED, TESTED |
| P1-5 | Research status lists walk-forward verdicts | `597ebf8` | `test_research_status.py` 18 passed | IMPLEMENTED, TESTED |
| Map | `docs/MVP_SEMANTIC_MAP.md` (Sonnet agent, grep-verified symbols) | `f10daad` | — | written |
| Queue | `docs/MVP_TASK_QUEUE.md` | `f1a604f` | — | written |
| M6-1 | Evaluation export v1, 11 schemas, verifier, stdlib scorer, CLI | `2d7191c` `088f3a3` `e63743b` | `tests/unit/evaluation` 41 passed, 1 skipped (fastapi), plus the import guard 3 | IMPLEMENTED, TESTED locally; CI pending |
| M6-real | Export, verify and score of **three real recorded Windows sessions** (copies of the staged owner runs) | — | 9 of 9 ledger hashes equal the session-end evidence | **VERIFIED on real sessions** (see the M6 devlog) |
| P0 | M3 acceptance request | `a0e2d76` | — | written; **D-A is the owner's** |
| Review | Fable review of P1 + M6: CHANGES REQUESTED (2 blocking: a selection record could unlock the holdout; free-text policy values) | fixes `afcd1a2` | prereg 37, evaluation 42 + 1 skipped, resume run-side 2 | fixed |
| M5-1 | Agent layer, mock client, advise-only, budget, breaker, cache, mode off, status API | `78fdbd2`…`0605795` + review fixes | `tests/unit/agents` 148 passed | IMPLEMENTED, TESTED; Fable review CHANGES REQUESTED (budget not a hard cap) fixed; not wired into the runtime |
| H1 | Draft H1 strategy (separate DRAFT_REGISTRY: no trial possible), `--draft` feasibility, H1-v2 draft | `0030114` `fec95ba` `ba69590` `a23d0db` | 45 + 10 + 2 | IMPLEMENTED, TESTED; causality audit CONDITIONAL PASS: the embargo was not checked against the holding period, fixed `78ed753` |
| Re-review | Fable re-review of every fix commit: CHANGES REQUESTED, narrow. Nothing failed open (gate not wired, no preregistration frozen). N1: three malformed ledger lines raised instead of refusing. N2: `anchor: auto` left the folds free and the config hash was optional | `8200ffe` (N1, N3 tests, N4 bounded Windows lock), `93846a0` (N2 freeze pins anchor and backtest config hash; N5 unknown keys refused) | agents 156 passed; prereg + resume 66 passed; full unit suite 1887 passed, 2 skipped, 16 failed from missing pytest-asyncio/httpx in this container only (CI has them) | VERIFIED: CI `42786327337` 7/7 on `b6f9522` |
| Closure | Fable closure review of `8200ffe` `93846a0`: CONDITIONAL PASS, 0 blocking, no fail-open path. R1 the folds were still planned from the run's own first fold (and no folds passed); R2 nested key typos (`data.dataset_id`) accepted; R3 a mid-review lock timeout gave AI_UNAVAILABLE, counted against the breaker, and `snapshot()` raised; R4 the lock test would hang on a regression | `1d253e2` | agents 157 + prereg 44 passed locally (new tests fail on the previous code); run-side prereg 4 passed | VERIFIED: CI `42791493309` 7/7 |

## M0–M6 status (no acceptance is claimed here)
| Milestone | State |
|---|---|
| M0, M1 | Implemented and used daily; not formally accepted (ROADMAP live status) |
| M2 | **ACCEPTED, limited scope** (D-22) |
| M3 | Gate VERIFIED technically; formal acceptance open (D-A, [M3_ACCEPTANCE_REQUEST](M3_ACCEPTANCE_REQUEST.md)) |
| M4 | Tooling IMPLEMENTED and TESTED, most of it VERIFIED in CI (risk policy in backtests, 3,000 ms latency, preregistration and freeze). P&L-free diagnostics and power planning IMPLEMENTED, TESTED (integration session, below). No preregistration frozen, no trial consumed, no real-data research run |
| M5 | Mock-testable part VERIFIED (mock scope). Runtime wiring and the `agent_gated` book VERIFIED on the separate branch `task/m5-runtime-wiring` (CI `42844539087` 7/7 on `127e445`), **not merged** (D-H). Agent tab not started; the real-key clause BLOCKED (paid API, owner) |
| M6 (export) | IMPLEMENTED, TESTED (CI `42776226802`); **verified on three real recorded sessions**: the stdlib scorer reproduces all 9 session-end ledger hashes from the export alone. Not accepted: D-06 (format agreed with the other team) and the gate's wider dashboard metrics remain |

## Research readiness
**No validated strategy** (NO_VALIDATED_STRATEGY). The tooling can run a first preregistered H1
walk-forward once D-C, D-E and D-F1 are decided and the owner freezes a revised H1. New finding:
**every one of the 24 H1-v2 configurations is incompatible with the live risk floor (net reward/risk
1.5)**, as are all ADR-0012 geometries of the reference (regression tests in `test_live_compat.py` and
the H1 tests), so D-E decides whether any H1 result could ever trade in the baseline book.

## First recommended next actions (owner, morning)
All **NORMAL PowerShell** (no Administrator). Nothing below trades, sends an order or uses a paid API.

1. **Update the branch checkout and run the dashboard smoke check (A6, 5 minutes).** It uses an empty,
   separate data folder and port 8799, so your real app and data are untouched.
   ```powershell
   cd C:\Users\felip\Projects\trading-system-claude
   git fetch origin task/mvp-integration
   if (Test-Path ..\tsc-mvp) { git -C ..\tsc-mvp checkout --detach origin/task/mvp-integration } else { git worktree add --detach ..\tsc-mvp origin/task/mvp-integration }
   cd ..\tsc-mvp
   powershell -ExecutionPolicy Bypass -File scripts\owner_checks\dashboard_smoke.ps1
   ```
2. **H1-1 feasibility count on real development data** (P&L-free, records no trial; writes only
   `<data>\feasibility\`). It compares the draft H1 rules on 15m and 1h:
   ```powershell
   cd C:\Users\felip\Projects\tsc-mvp
   $env:TSC_DATA_DIR = 'C:\Users\felip\Projects\trading-system-claude\data'
   uv run tsclaude backtest feasibility --draft --strategy h1_compression_breakout `
     --grid docs\research\grids\feasibility_h1_draft_probe.json `
     --symbols spot:BTCUSDT,spot:ETHUSDT,spot:DOGEUSDT,spot:XRPUSDT --from 2022-03-01 --to 2025-01-01
   ```
3. **Decide D-C, D-E and D-F1** ([DECISIONS_PENDING](DECISIONS_PENDING.md)); D-A (M3 acceptance) can be
   decided independently ([M3_ACCEPTANCE_REQUEST](M3_ACCEPTANCE_REQUEST.md)). D-E matters most: under the
   current live floor, every H1-v2 configuration is rejected by the baseline book.
4. Optional: the walk-forward mechanics dry run (REFERENCE strategy, about half an hour, about 21 permanent
   trial rows in the `bracket_reference` family only) and an M6 export of a **copy** of a recorded session:
   ```powershell
   cd C:\Users\felip\Projects\tsc-mvp
   uv run tsclaude backtest walkforward --strategy bracket_reference --grid docs\research\grids\bracket_reference_dryrun.json --symbols spot:BTCUSDT --hypothesis B2-DRYRUN
   Copy-Item C:\Users\felip\tsc_runs\paper-20261109-2201\data $env:TEMP\tsc-export-data -Recurse   # with state.db-wal
   uv run tsclaude evaluation export --db $env:TEMP\tsc-export-data\state.db --out $env:TEMP\tsc-export
   uv run tsclaude evaluation verify --export $env:TEMP\tsc-export --db $env:TEMP\tsc-export-data\state.db
   uv run tsclaude evaluation score --export $env:TEMP\tsc-export
   ```

## Remaining owner decisions
D-A, D-B, D-C, D-D, D-E, D-F1, D-F2, D-G, D-H ([DECISIONS_PENDING](DECISIONS_PENDING.md)).

## CI evidence (overnight)
| Run | Commit | Result |
|---|---|---|
| `42770742512` | `9ef847a` | 7/7 green: python ubuntu 1965 passed, windows 1963 |
| `42775771210` | `597ebf8` | **failed**: mypy strict on `research/preregistration.py` (fixed in `6c9d57c`) |
| `42776226802` | `45cf45a` | 7/7 green: python ubuntu 2166 passed, windows 2164; frontend vitest + build both OS (covers preregistration tooling, live compatibility, status, M6, M5 first version, the walk-forward table) |
| `42780296915` | `2658aa4` | 7/7 green (M5 review fixes, prereg review fixes, embargo, H1 draft) |
| `42786327337` | `b6f9522` | 7/7 green: python ubuntu 2284 passed, windows 2282; frontend both OS (re-review fixes N1–N5) |
| `42791493309` | `2fa8bbc` | 7/7 green: python ubuntu 2285 passed, windows 2283; frontend both OS (closure-review fixes R1–R4; final code HEAD) |

## Checkpoints
- 2026-11-10 ~02:30 local: `e63743b` + this report.
- 2026-11-10 ~04:00 local: `95ae656` (M5 review fixes, H1 draft + causality fixes, prereg review fixes, agent status card, semantic map update).
- 2026-11-10 ~06:30 local: `b6f9522` (re-review fixes N1–N5, M6 real-session evidence, agent status card, morning actions); CI `42786327337` 7/7.
- 2026-11-10 ~08:30 local: closure-review fixes `1d253e2` and this report; pushed; CI `42791493309` 7/7 on `2fa8bbc`.

## Morning session (2026-11-10, owner present)
Owner direction under D-E: keep the 1.5 floor, design a compatible search space for review, and
apply identical risk policies in backtesting and Paper. Nothing was frozen, merged or accepted, no
official trial was recorded, and no holdout data was touched.

| Item | Commits | Evidence | Status |
|---|---|---|---|
| P1-6: backtests apply the paper risk policy (`risk_policy: live`). `data_freshness` is computed from bars as in the paper tracker; kill switch and base cost model in every book | `9f2cabc` `0429135` | `test_backtest_risk_policy.py`; Fable review CONDITIONAL PASS, 0 blocking; F1–F5 and F8–F10 fixed; 3 surviving mutants now killed | VERIFIED: CI `42829517341` 7/7 on `0429135` (python ubuntu 2307 passed, windows 2305; frontend both OS) |
| A kept floor is enforced at `validate`, `freeze` and run time (run cost model and risk config) | `5ea287e` `b7abde7` | `test_preregistration.py` (47), run-side test | validate/freeze VERIFIED in CI `42829517341`; run-side check (`b7abde7`) IMPLEMENTED, TESTED |
| H1-v3 live-compatible draft (2, 2.5, 3 R); [H1_DECISION_BRIEF](research/H1_DECISION_BRIEF.md) (D-C, D-E, D-F1, ADR-0012 Amendment 6, sequence, commands) | `9c681ca` + later edits | validates; 0 problems with the floor kept (v2: 24) | DRAFT for owner review |
| Walk-forward status and table show the risk rules used | `79908c7` | `test_research_status.py`; frontend test runs in CI | IMPLEMENTED, TESTED (backend) |
| Decision latency 3,000 ms (SIMULATION §2: at least the live p95). Measured p95 2,170 ms, max 2,920 ms over 576 live bars; latency 0 had filled entries one minute earlier than Paper can | `1cc2693` (CI `42837576325` 7/7) | `docs/research/evidence/live_release_latency_2026-11-10.json`; latency test | VERIFIED: CI `42837576325` 7/7 on `1cc2693` |
| Walk-forward reports sum the risk-policy blocks per phase; end-to-end rehearsal of the first-experiment pipeline (synthetic data, REFERENCE strategy) | `f716a71` `ce751fc` | `test_backtest_risk_policy.py` (24) | IMPLEMENTED, TESTED |
| M5-3: agent gate in the paper runtime, `agent_gated` book, journaled and replayable | branch `task/m5-runtime-wiring` `cf8cfe9`…`97168b2` (pushed, **not merged**) | 60 new tests; mode off byte-identical (reproduced by the reviewer); Fable review CONDITIONAL PASS, 0 blocking, findings fixed or recorded (L-13); CI `42838323339` 7/7 on `97168b2` (fastapi status test included) | VERIFIED on its branch (mock client only); not merged (D-H) |

**Findings for the owner.**
- 2 R holds the floor only while take-profits fill as maker orders: it nets 1.52, or 1.4995 if
  they fill as taker orders. {2.5, 3, 3.5} keeps a margin either way (brief §3).
- Applying the paper policy and the real latency makes backtests more conservative, by design.

## Integration session (2026-11-10, "consolidated MVP and mathematical research integration")
Ground truth checked first: `1cc2693` (CI `42837576325` 7/7), then `2b63d4c` (CI `42844534280` 7/7)
and the M5 branch at `127e445` (CI `42844539087` 7/7). Nothing was frozen, merged or accepted; no
trial was recorded; no holdout data, risk threshold or Codex repository was touched.

| Item | Commits | Evidence | Status |
|---|---|---|---|
| The mathematical audit added as `docs/research/MATHEMATICAL_STRATEGY_FOUNDATIONS.md`: Part 1 reconciliation against `2b63d4c` (claims table, one number corrected, five qualifications of the floor's meaning, recommendation status), Part 2 verbatim | `9126512` + review wording fixes | `test_math_foundations_numbers.py` (8) reproduces every derived number with the engine's own `net_rr` and `n_min` | IMPLEMENTED, TESTED |
| P1-9: P&L-free feasibility diagnostics (stage filter, UTC-hour split overall, per symbol and per ablation rung, month totals) | `87ad417` + review fixes | `test_feasibility.py` (27) | IMPLEMENTED, TESTED |
| P1-10: power plan (`tsclaude backtest power-plan`), walk-forward `exit_mix` and `power_reading` | `c750fe8` + review fixes | `test_power_plan.py` (12), rehearsal | IMPLEMENTED, TESTED |
| Decision package: D-C with R-4, D-E with Amendment 6 and the take-profit sets compared mechanically, D-F1, D-G, R-1..R-8, authorized vs proposed, readiness, owner steps | `9126512` + this checkpoint | [H1_DECISION_PACKAGE](research/H1_DECISION_PACKAGE.md) | for owner decision |
| Independent review of the three commits | `347e87f` (fixes) | Fable: **CONDITIONAL PASS, 0 blocking**; M1–M4 and minors fixed, one declined with a reason (devlog); full unit suite 1941 passed, 16 env-only failures | **VERIFIED: CI `42854101526` 7/7 on `f6ce164`** (python ubuntu 2338 passed, windows 2336; frontend both OS) |
| The detectable margin in the research status API and the walk-forward table | `1e2339b` | `test_research_status.py` 18 passed; frontend test in CI | VERIFIED: CI `42859002211` 7/7 on `0955749` (`42858830369` was cancelled by the next push) |

**H1 readiness (package §7):** no engineering blocker remains up to the out-of-sample verdict. Still
open: the owner's feasibility count and power plan (NORMAL PowerShell, package §8), then D-C, D-E with
Amendment 6 and R-1, the take-profit set, D-F1, then the freeze. Validation and the holdout stay
blocked (D-D, D-F2, P1-7).

## Resume session (2026-11-10, after the owner's smoke check at `0955749`)

**Owner evidence received:**
- the Windows frontend build passed (typecheck, Vite build, exit 0);
- the smoke check passed 7/7 (`NO_VALIDATED_STRATEGY`, Paper disabled, agents OFF);
- visual confirmation of the panels is not yet reported.

The build first needed a manual esbuild approval under pnpm 12.18.0. The local files `frontend/pnpm-lock.yaml` and
`frontend/pnpm-workspace.yaml` were not committed (D-G).

| Item | Commits | Evidence | Status |
|---|---|---|---|
| PL-1: esbuild approved for pnpm 9 to 12. pnpm 11 and later ignore package.json's `pnpm` field; CI's pnpm 9 never blocks scripts, so CI could not see it | `6fd2fd7` | `test_frontend_toolchain.py`; pnpm 10.28 here maps the file correctly; pnpm 9 in CI | IMPLEMENTED, TESTED; CI after the push; the owner's pnpm 12 check is in the run sheet §1 |
| P1-12: tested owner run sheet for H1-1 (count, dataset identity, power plans for both take-profit sets, evidence zip); power-plan `--out` | `c14769c` | `test_owner_feasibility_run.py` parses every documented command and rehearses the sequence on a synthetic lake | IMPLEMENTED, TESTED |
| H1 decision record (blank, nothing decided) | `f7a957d` | [H1_DECISION_RECORD](research/H1_DECISION_RECORD.md) | for the owner |
| M6-3: every contract event type is exported or deliberately not | `94e821e` | `test_eval_event_coverage.py`; it fails on the old exporter with the M5-3 contract | IMPLEMENTED, TESTED |
| M5: no `agent_gated` session could be exported; fixed by export format v2 (`agents.jsonl`), v1 still checkable | branch `task/m5-runtime-wiring` `48ed4d7` `f5c2538` | 8 export tests + 3 format tests; the 9c681ca v1 exporter and the v2 exporter (rewritten to v1) give byte-identical output on one store | IMPLEMENTED, TESTED on its branch; not merged (D-H) |
| M5 mode-off identity re-verified independently | — | the paper scenario on `9c681ca` and `127e445`, each with its own code: same 39 batches, journal digest, ledgers and fingerprint | VERIFIED (local) |
| M5-3 decisions and D-06 interface questions as owner sheets | `94e821e` | [M5_RUNTIME_DECISIONS](M5_RUNTIME_DECISIONS.md), [M6_D06_INTERFACE_QUESTIONS](M6_D06_INTERFACE_QUESTIONS.md) | for the owner |

**Correction to the earlier owner steps:** none needed for correctness. Per the PowerShell
documentation (about_Parsing), PowerShell passes an
unquoted `a,b` to a native program as one argument. The run sheet still quotes the comma lists,
because a token that starts with a digit (`2,2.5,3`) is less clear-cut.

## Feasibility evidence integrated (2026-11-10, owner direction "integrate independent H1 feasibility findings")

| Item | Commits | Evidence | Status |
|---|---|---|---|
| The owner's 15m and 1h counts at `0955749`, copied byte for byte | `da8e609`, `5a8eebd` | SHA-256, content hash, config id and config hash checked; `test_h1_feasibility_evidence.py` | VERIFIED as measured |
| 15m recorded as a P&L-free no-go (167 candidates, ceilings ≤ 109 against 300, 42.7 out of sample) | `da8e609` | [H1_FEASIBILITY_FINDINGS](research/H1_FEASIBILITY_FINDINGS.md) §2 | recorded; no trial consumed |
| 1h measured (351, 146.1 out of sample, ceilings 153–289; T1 and T2 not met) | `da8e609` | findings §4 | awaiting the owner's review; no redesign proposed |
| Independent review reconciled against HEAD: every derived number reproduced except one not derivable from the file (4.48 % / 14.76 %) | `da8e609` | findings §5 | done |
| Feasibility-v3: registered downtime apart from unexplained missing minutes; code identity in each count | `3573509` | feasibility 30 tests | IMPLEMENTED, TESTED |
| Power-plan-v3: training-window ceilings per fold against `n_min` | `6fc6398` | power plan 15 tests; reproduces 70 … 109 | IMPLEMENTED, TESTED |
| The February 2022 mismatch: origin `08f8953`; the run sheet now counts from the anchor 2022-02-01 | `da8e609` | findings §6 | documented, fixed for future counts |
| CRLF on Windows in feasibility.json and power-plan output; now LF everywhere; evidence kept byte for byte | `5a8eebd` | LF assertions (effective on the Windows CI job) | IMPLEMENTED, TESTED |

H1-v3, the 1.5 floor, the cost model and the hypothesis definition are unchanged.
