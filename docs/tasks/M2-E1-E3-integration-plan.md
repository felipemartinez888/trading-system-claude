# Integration plan: E-1/E-1b and E-3 into `feat/mvp-4-dashboard` (NOT EXECUTED)

**Status: plan only.** Nothing here has been run. It needs explicit owner approval. Pushing and `main` stay under
D-07.

## Starting state
| Ref | Commit | Base |
|---|---|---|
| `feat/mvp-4-dashboard` (main working tree) | `c327bbd` | — |
| `task/m2-e1` (worktree `tsc-e1`) | `4e88ba0` (E-1), then the E-1b commit on top | `c327bbd` |
| `task/m2-e3` (worktree `tsc-e3`) | `e5a5e2e` | `ebefbc1` (an ancestor of `c327bbd`) |

**Overlap:**
- the only shared path is `docs/devlog/metrics.csv`, where each branch appends rows;
- E-3 touches `marketdata/archive/*`, `store/lake.py` and its tests;
- E-1/E-1b touch `sim/`, `research/`, `quality/`, `runtime/paper_runtime.py`, `frontend/`, `configs/` and the
  docs.

**Expected conflicts:** none in code. One trivial conflict in `metrics.csv`; resolve it by keeping both appended
rows in commit order.

## Steps (the owner's main working tree, normal PowerShell, no administrator rights)
0. **Pre-checks.**
   - `git status` is clean apart from untracked owner data (`data*`, `evidence/`, `uv.lock`,
     `frontend/package-lock.json`). Nothing is staged.
   - Record the rollback point: `git rev-parse feat/mvp-4-dashboard` (expected `c327bbd`).
   - Optionally create a local safety tag (local only, never pushed):
     `git tag pre-m2-integration c327bbd`.
1. **E-1/E-1b by fast-forward.**
   - `git merge --ff-only task/m2-e1`.
   - `feat/mvp-4-dashboard` sits exactly at E-1's base, so this cannot create a merge commit or a conflict. If it
     is refused, stop and report.
   - Rollback point R1 = the new HEAD (E-1 or E-1b).
2. **E-3 by an explicit merge commit.**
   - `git merge --no-ff task/m2-e3 -m "merge: M2-E3 importer integrity into feat/mvp-4-dashboard"`.
   - Expected conflict in `docs/devlog/metrics.csv` only: keep every row from both sides, E-1 rows then E-3 rows.
     Each row must be valid CSV.
   - Any other conflict: `git merge --abort`, then stop and report. No improvised resolution.
   - Rollback point R2 = the merge commit.
3. **Docs reconciliation** (a small follow-up commit if needed).
   - `ROADMAP.md` current work and blockers; DECISIONS links; the E-3 devlog status (committed, then integrated).
   - Docs only; reviewed before committing.

## Post-integration tests (focused and chunked, as for E-1; logged to files)
- **Archive and importer** (E-3): `tests/unit/archive`, the lake and store tests.
- **Simulator** (E-1): `tests/unit/sim`.
- **Backtest and research** (E-1/E-1b): `tests/unit/backtest`, `tests/unit/research`, `tests/unit/strategies`,
  `tests/unit/risk`.
- **Paper integration:** `test_mvp2_paper`, `test_mvp21_paper_hardening`, `test_mvp3_reference_paper`,
  `test_mvp4_paper_api`, `test_mvp2_paper_api`; and `test_o4_zero_trade_capture`.
- **Frontend:** `pnpm test` for the Paper and Backtests pages; `tsc --noEmit`.
- **Static checks:** `ruff check --no-cache`, `ruff format --check`, `mypy src` (scratch cache).
- **Expected:** the golden hashes equal the values pinned on `task/m2-e1`. E-3 does not touch the runner or the
  simulator.

## Rollback
- **Before any commit:** `git merge --abort`.
- **After step 1 or 2, preferred (no history rewrite):**
  - `git revert -m 1 <merge commit>` undoes E-3;
  - `git revert <E-1b> <E-1>` undoes the fast-forwarded commits.
- **Alternative, explicit owner approval only:** moving the local, never-pushed branch back to a recorded
  point (`R0 = c327bbd`, `R1`) rewrites local history, so it is never done without that approval.
- **The work is never lost:** the task branches `task/m2-e1` and `task/m2-e3` stay untouched either way.

## Not part of the integration
- No push, no merge into `main`, no branch deletion, no worktree removal.
- No data operation, no import, no backtest.
- **Paper:** after integration the simulator is sim-v1, so existing paper journals do not restore and fail
  closed. Starting a new session is the owner's separate decision; nothing is migrated or reset automatically.
