# Current status (read this first)

One-page summary for new sessions. It points to the authoritative records and never overrides them:
[ROADMAP](ROADMAP.md) (gates), [DECISIONS_PENDING](DECISIONS_PENDING.md) (owner decisions), devlogs.
If this page and a record disagree, the record wins. Fix this page in the same change.
Last update: 2026-11-10 (WF-1).

## Four separate status levels (never merge them)
| Level | State | Basis |
|---|---|---|
| **M3 operational acceptance** | **ACCEPTED, scope-limited**: supervised, paper-only, at `f03da159443bc17a5edc997982c643ce649d315d` | [D-24](DECISIONS_PENDING.md#2026-11-10-m3-acceptance-d-24) (owner, 2026-11-10); CI `42888483003` 7/7 |
| **Operational MVP** | Functional for **supervised** Paper with the REFERENCE strategy (`bracket_reference`, not validated), agents `off` | [MVP_VALIDATION_REPORT](MVP_VALIDATION_REPORT.md), [MVP_LIMITATIONS](MVP_LIMITATIONS.md) |
| **M4 research validity** | **NOT STARTED**: `NO_VALIDATED_STRATEGY`; tooling tested on synthetic data; no real walk-forward, no H1 run, no trial consumed | [MVP_TASK_QUEUE](MVP_TASK_QUEUE.md) P1/P2 |
| **Paper signal validation** | **NONE**: no strategy book has traded in a real session (U1) | [M3_ACCEPTANCE_REQUEST](M3_ACCEPTANCE_REQUEST.md) |

## Milestones (as recorded; see ROADMAP for the full rows)
- **M0:** code complete, not formally accepted.
- **M1:** **OPEN**. The 2-hour live run, induced disconnect and hard-kill gate is not met. Shorter supervised sessions are fine within their validated scope.
- **M2:** accepted, limited scope (D-22): Spot 1m, 5 symbols, 2022-01 → 2025-12.
- **M3:** accepted, scope-limited (D-24, above).
- **M4:** not started formally.
- **M5:** mock-tested. Real-key cost/latency gate open; **paid API not authorized**; agents `off`.
- **M6:** export implemented and tested; protocol D-06 open; analytics not started.
- **M7:** not started (post-MVP).

## M3 acceptance scope (D-24): what it carries
- **Evidence:** `C:\Users\felip\tsc_runs\readiness-f03da15\TSC_M3_DA_evidence_f03da15_20261110-162620.zip`.
  - Phase A: 126 gate tests passed on the 2nd attempt; 3 historical replays; 7 smoke checks.
  - Phase B: about 30 min supervised Paper session.
  - Phase C: 186/186 batches, 174 observed bars, 253 chain events, ledger hashes 3/3 equal (session end = replay = export). Linux cross-check PASS. Config digest `676d69b6…`.
- **Exceptions (cause not established; do not classify as fixed or harmless):**
  - **E-1:** native crash `0xC0000005` in the 1st Windows gate-test run; the identical retry passed.
  - **E-2:** native crash `0xC0000005` in the Phase B config-digest helper's 1st try; the retry passed.
  - **E-3:** one caught `websocket frame dispatch failed` (usdm-public); no observed ledger impact.
  - **Screenshots:** the four dashboard screenshots are missing (supporting-evidence WARN).
- **Limitations kept:**
  - U1: no live strategy/risk-engine execution path was exercised; benchmark book only.
  - U2: restart equivalence not proven (P-1; D-19/D-20/D-21, NB-1 residual risk).
  - D-B: no kill-switch reset in the app.
- **Not authorized by D-24:** live trading, autonomous or unattended operation, agents, a validated strategy, paid API use.

## Next objective: first meaningful M4 experiment (H1)
No engineering blocker is known. The path waits on **owner decisions** (details:
[H1_DECISION_PACKAGE](research/H1_DECISION_PACKAGE.md), [H1_FEASIBILITY_FINDINGS](research/H1_FEASIBILITY_FINDINGS.md)):
1. **Research direction after the 1h feasibility count.** 351 candidates, about 146 out of sample, training ceilings 153–289 vs `n_min` 300, T1/T2 not met. 15m is a recorded no-go.
2. **D-C** trial counting; **D-E** record "keep net R/R ≥ 1.5" plus ADR-0012 Amendment 6; **D-F1** SOL/universe; **D-G** lock files (recommended).
3. Owner freezes H1 (H1-d), then the first walk-forward (H1-e) runs on the owner PC.

D-D and D-F2 are needed only for validation and the holdout. D-B and D-H do not affect research.

## Working conventions (Claude Code)
- **Models:**
  - Opus: lead, design, gate conflicts.
  - Builder agents in `.claude/agents/` run on Sonnet.
  - `reviewer` and `causality-auditor` inherit the session model.
- **Concurrency:** one implementation workstream plus one fresh-context read-only review.
- **Skills:**
  - `/checkpoint-handoff`: end of task. Writes `docs/checkpoints/`.
  - `/mvp-gate-review <M#|AC-##>`: advisory gate check.
- **Existing integrity tools (reuse, don't rebuild):**
  - `tsclaude data` (QA), `verify-chain`, `verify-replay`;
  - `tsclaude evaluation export/verify/score`, `tsclaude prereg validate`;
  - `scripts/owner_checks/`.
- **Pushes need explicit owner approval each time.** Never force-push or rewrite history.

## Known documentation drift (NON-BLOCKING, not yet fixed)
- [MVP_LIMITATIONS](MVP_LIMITATIONS.md) still says backtests apply `allow_all`. That is superseded by P1-6 (`risk_policy: live`).
- MVP_LIMITATIONS also still says "M5 agents; M6 export" are not implemented, which is stale.
- [README](../README.md) and [MVP_SEMANTIC_MAP](MVP_SEMANTIC_MAP.md) still describe M3 as not formally accepted / D-A open (superseded by D-24).
- `docs/reviews/` is referenced by DEV_ORCHESTRATION but does not exist.
- The validation write-up `TSC_M3_DA_VALIDATION_F03DA15.md` is owner-provided and not in the repo.
