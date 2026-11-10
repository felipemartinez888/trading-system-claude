# MVP master plan: reconciled status, plan and execution progress

**Started 2026-11-09 (UTC-6), autonomous MVP session.** Branch `task/mvp-integration`, based on
`feat/ingestion-bounded` @ `1160a71`. This plan adds a delivery view. It does **not** change any
gate in [ROADMAP](ROADMAP.md): M0 to M7, their literal gates, the 16 acceptance criteria and
"MVP = end of M5 plus the M6 export" stay as written. It also does not change the targets in
[MVP_DELIVERY_TARGETS_PROPOSAL](MVP_DELIVERY_TARGETS_PROPOSAL.md).

Companion documents:
- [MVP_VALIDATION_REPORT](MVP_VALIDATION_REPORT.md): evidence and counts;
- [MVP_USER_GUIDE](MVP_USER_GUIDE.md): how to run it;
- [MVP_LIMITATIONS](MVP_LIMITATIONS.md): what is not done or not proven.

## 1. What "MVP" means here
- **Engineering MVP** (the owner's 2026-11-09 directive): a locally executable, evidence-backed,
  paper-only system. It covers live data → canonical bars → strategy → signal → risk → paper →
  ledger → dashboard, plus a research workflow that can reject strategies honestly.
  - A validated profitable strategy is **not** required.
  - Its absence must be stated, and it is (`NO_VALIDATED_STRATEGY`).
- **It is roughly Target A plus the research tooling of Target B.**
- **Target C, the formal roadmap MVP, is still open.** Formal M4 acceptance, M5 (agents) and the
  M6 export have no new code here.

## 2. Ground truth, re-verified 2026-11-09 (not taken from earlier summaries)

**Repository**
- **Owner PC:**
  - the main checkout is on `task/m3-partb` @ `928aeb7`;
  - newest work: `feat/ingestion-bounded` @ `1160a71` (pushed); its worktree `tsc-ingestion-bounded` has no source edits since checkout;
  - local-only refs: `task/m2-closure` @ `f18eb9f` (never published: holdout metadata) and `refs/preserve/e4b-2026-11-04` @ `07f3023`.
- **Anchors:** PB.3 `0ee0e4a`, PB.4 `d1e280a`, PB.5 `2a024c7` and Replay `19ac13f` are ancestors of `1160a71` (verified with `git merge-base --is-ancestor`).
- **Not touched:** the Codex and original `trading-system` repositories.

**Status**

| Item | Status | Evidence |
|---|---|---|
| M0 Foundations | Code done; formal acceptance not recorded | ROADMAP live status |
| M1 Live tape | Code done; 2 h gate not accepted | ROADMAP |
| M2 Data + backtest core | **Accepted, limited scope** (D-22) | M2_ACCEPTANCE_RECORD |
| M3 Paper + risk | Literal gates open (fault-injection acceptance; strategy books never exercised in a real session) | ROADMAP, evidence matrix |
| M4 Research | Not started formally. Tooling: stats, trials, holdout guard, runner, and since this session walk-forward and research status. No preregistration, no hypothesis, no real-data run | this plan §4 |
| M5 / M6 | No code | ROADMAP |
| P-1 | Implemented; NB-1 closed under D-21 (residual risk accepted) | DECISIONS_PENDING |
| E-4(b) | Uncommitted in `tsc-e4b`, preserved; not an MVP blocker; untouched | M2_CLOSURE_CHECKLIST |
| D-23 bounded ingestion | Implemented on `feat/ingestion-bounded`. Dev profile: owner PASS. **Production profile: clean on 2026-11-09** (artifacts read in this session) | MVP_VALIDATION_REPORT §3 |

## 3. Pipeline map (as implemented)

| Stage | Module | State |
|---|---|---|
| Binance public REST + WebSockets | `marketdata/binance/{rest,ws}.py` | Real session 2026-11-09: 6 pairs, spot and USD-M |
| Ingestion: bounded catch-up and on-demand history | `marketdata/{ingest,backfill,service}.py` | 24 h catch-up for 6 pairs in about 4 s; 9-day history in about 33 s (owner run) |
| Canonical bars, gaps, freshness, clock | `store/livebars.py`, `quality/*` | Gaps detected and recovered in the real run |
| Strategy and signals | `strategies/bracket_reference.py`, `live_adapter.py` | REFERENCE only; built at the first *observed* minute |
| Risk | `risk/*` | Fail-closed tests; production thresholds unchanged |
| Paper execution, ledgers, journal, replay | `sim/*`, `runtime/paper_runtime.py`, `session_evidence.py` | Real 1 h replay PASS on 2026-11-06. **2026-11-09: every bar unobserved** because of a clock offset (§5) |
| Dashboard | `api/*`, `frontend/` | Health, Markets, Signals, Logs, Paper, Backtests. New: research-status panel and paper "What to do" hints |
| Research | `research/*` | Runner, certification, random control, holdout guard, trials. New: walk-forward |

## 4. Execution plan and progress (this session)

| # | Phase | Task | Owner model | State | Evidence |
|---|---|---|---|---|---|
| A1 | A | Reconcile repo and milestone status | Opus (lead) | ✅ done | §2 |
| A2 | A | Analyze the owner's 2026-11-09 runs (Stage B, Stage C) | Opus | ✅ done | MVP_VALIDATION_REPORT §3 |
| A3 | A | Root-cause and fix the paper-runtime CI flakes, without weakening any assertion | **Fable** subagent (separate worktree) | 🔄 running | §6 |
| A4 | A/D | Clock offset: measured −3.41 s against Binance, confirmed −3.45 s against NTP by the owner, fixed by "Sync now" (0.001 s) | Owner + Opus | ✅ resolved operationally | MVP_VALIDATION_REPORT §3 |
| B1 | B/E | Research status API plus a NO VALIDATED STRATEGY panel | Opus | ✅ code (`a268ab7`, `f1658fe`); frontend CI green | MVP_VALIDATION_REPORT §2 |
| B2 | D/E | Actionable paper-blocker hints (API + Paper page) | Opus | ✅ code (`84653e1`); frontend CI green; Python CI pending | same |
| C1 | C | Anchored walk-forward, train-only selection, stitched OOS verdict, CLI | Opus | ✅ code (`1c40da2`); local tests 18 passed; **no real-data run** | same |
| F1 | F | Unattended owner evidence run (clock check, crash recovery, Paper O-1, backtest reproducibility) | Opus (script) + owner (run) | 🔄 handed to the owner | `scripts/owner_checks/mvp_evidence.ps1` |
| F2 | F | MVP docs (these 4) | Opus | ✅ first version | — |
| F3 | F | Final independent acceptance review | **Fable** subagent | ⏳ after A3 and CI | — |

**Next engineering actions**, in order:
1. Merge the Fable flake fix after review, then get CI green on both OS.
2. Read and record the owner's evidence run.
3. Independent final review.
4. Next research step: H1 (compression → expansion breakout) preregistration and strategy, then the first real-data walk-forward.
   - Owner-run, minutes to hours.
   - The expected and reportable outcome is `NO_EDGE_DEMONSTRATED`.

## 5. Decisions and findings that need the owner
- **Clock.**
  - The 1 s `clock_skew_max_ms` (`configs/risk.yaml`) is an accepted risk policy. It made every bar of the 2026-11-06 (6.9 s) and 2026-11-09 (3.4 s) sessions unobserved.
  - **Recommended default:** keep the limit, sync Windows time before each session. The evidence script refuses above 0.5 s.
  - Loosening the limit is an owner decision, not proposed.
- **PC restart on 2026-11-09 around 13:07**, owner-confirmed.
  - It happened during light steady-state load, 6.5 minutes after ingestion finished.
  - Nothing in the app's evidence points to the app. The hardware investigation stays out of scope, per the directive.
- **Push policy.** `task/mvp-integration` was pushed as a new branch under D-07 (standing approval for non-destructive pushes of tested milestones). No existing branch was touched. Merging needs the owner.

## 6. Model allocation (verified, not assumed)
- **Verified capability:** the Agent tool here accepts `model: fable | opus | sonnet | haiku`; worktree isolation is not permitted. A Fable subagent was launched on 2026-11-09 in a separate git worktree (`/home/claude/wt-flakes`).
- **Effort:** development agents run at **medium** effort (owner preference).
- **Fable** is used for:
  - the most demanding open defect: the CI flake family, where a production race had to be ruled in or out;
  - the final independent acceptance review.
- **Opus** (the lead session): implementation, integration and docs.
- **Sonnet:** mechanical work (one staging task; it failed because the bridge refuses hardlinked venv files).
- **Never used:** paid APIs or usage-based billing.

## 7. Graphify (directive §9): **REJECT for the MVP**, on the owner's own pilot data
- **Data:** the pilot in `C:\Users\felip\Projects\tsc-graphify-pilot` (bench run 2026-11-06), with its outputs re-scored in this session using its `bench.py` scoring rules.
- **Cost:**
  - Across 3 navigation tasks, plain `grep` returned about 4.9k tokens of context, Graphify about 10.4k (2.1×).
  - Graphify cost more on every task: A 2.9k vs 3.7k, B 0.8k vs 3.7k, C 1.1k vs 3.1k.
- **Recall:** Graphify was equal or lower.
  - Symbols: grep 4/4 on all three tasks, Graphify 3/4 on all three.
  - Tests: task A, grep 4/4 vs Graphify 2/4.
  - Its one gain was one more production file in task B, plus fewer "trap" files.
- **Scope of the pilot:** small (3 tasks, one snapshot); determinism and coverage outputs from `analyze.py` were not saved.
- **No change made:** no repository hook, execution policy or instruction file was changed for Graphify.
- **Revisit** only if a future pilot shows lower token cost at equal recall.

## 8. Rust
Deferred, per the directive. There is no measured bottleneck that a rewrite would fix: the
bounded ingestion already removed the Start-time load.

## 9. Continuation notes (if the session is interrupted)
- **Code:** `origin/task/mvp-integration`. CI: GitHub Actions on push; run ids are in the validation report.
- **Flake fix:** branch `fix/paper-runtime-test-determinism`. Look in the session worktree `/home/claude/wt-flakes` if it still exists; otherwise redo it from §6 of the validation report.
- **Owner evidence:** `C:\Users\felip\tsc_runs\mvp-evidence-<stamp>\summary.json`.
- **Checklist:** the open items are the ⏳/🔄 rows of §4, in order.
