# MVP overnight report (2026-11-10)

Updated incrementally during the session. Status words: IMPLEMENTED / TESTED / VERIFIED / ACCEPTED /
BLOCKED. A completed implementation is never validated effectiveness. Tracker: [MVP_TASK_QUEUE](MVP_TASK_QUEUE.md).
Map: [MVP_SEMANTIC_MAP](MVP_SEMANTIC_MAP.md).

## Branch and HEAD
- Branch `task/mvp-integration` (pushed under D-07, **not merged**). Worktree `/home/claude/trading-system-claude`
  (cloud workspace; the owner's PC was not written to).
- Start of the overnight directive: `08f8953` (end of the Track A / Mission 2 / Mission 3 sequence).
- Current HEAD: see `git log`; the last checkpoint is listed at the end of this file.
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
| P0 | M3 acceptance request | `a0e2d76` | — | written; **D-A is the owner's** |
| Review | Fable review of P1 + M6: CHANGES REQUESTED (2 blocking: a selection record could unlock the holdout; free-text policy values) | fixes `afcd1a2` | prereg 37, evaluation 42 + 1 skipped, resume run-side 2 | fixed |
| M5-1 | Agent layer, mock client, advise-only, budget, breaker, cache, mode off, status API | `78fdbd2`…`0605795` + review fixes | `tests/unit/agents` 148 passed | IMPLEMENTED, TESTED; Fable review CHANGES REQUESTED (budget not a hard cap) fixed; not wired into the runtime |
| H1 | Draft H1 strategy (separate DRAFT_REGISTRY: no trial possible), `--draft` feasibility, H1-v2 draft | `0030114` `fec95ba` `ba69590` `a23d0db` | 45 + 10 + 2 | IMPLEMENTED, TESTED; causality audit CONDITIONAL PASS: the embargo was not checked against the holding period, fixed `78ed753` |

## M0–M6 status (no acceptance is claimed here)
| Milestone | State |
|---|---|
| M0, M1 | Implemented and used daily; not formally accepted (ROADMAP live status) |
| M2 | **ACCEPTED, limited scope** (D-22) |
| M3 | Gate VERIFIED technically; formal acceptance open (D-A, [M3_ACCEPTANCE_REQUEST](M3_ACCEPTANCE_REQUEST.md)) |
| M4 | Tooling IMPLEMENTED and TESTED; no preregistration frozen, no hypothesis implemented as runnable, no real-data research run |
| M5 | Mock-testable part IMPLEMENTED and TESTED (148 passed, reviewed, fixed); runtime wiring, `agent_gated` book and Agent tab DEFERRED; the real-key clause BLOCKED (paid API, owner) |
| M6 (export) | IMPLEMENTED, TESTED locally; CI pending; real-session export is an owner step |

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
   Copy-Item C:\Users\felip\tsc_runs\paper-20261109-2201\data\state.db $env:TEMP\tsc-export.db
   uv run tsclaude evaluation export --db $env:TEMP\tsc-export.db --out $env:TEMP\tsc-export
   uv run tsclaude evaluation verify --export $env:TEMP\tsc-export --db $env:TEMP\tsc-export.db
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
| later | `2658aa4` and after | see the last checkpoint |

## Checkpoints
- 2026-11-10 ~02:30 local: `e63743b` + this report.
- 2026-11-10 ~04:00 local: `95ae656` (M5 review fixes, H1 draft + causality fixes, prereg review fixes, agent status card, semantic map update).
