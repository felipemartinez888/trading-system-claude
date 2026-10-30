# Integration plan and record: E-1/E-1b and E-3 into `feat/mvp-4-dashboard` (EXECUTED 2026-10-30)

**Status: executed** locally on 2026-10-30 under the owner message "OWNER APPROVAL — CONTROLLED E-1/E-1b/E-3
INTEGRATION". The result is merge commit `03c91581d06360255e2f7f6bfb44f53cd6c974e0`; see [Result](#result-2026-10-30)
below. Nothing was pushed and `main` is untouched (D-07). This is an integration, not formal M2 acceptance.

The plan as approved follows unchanged; the result section records what actually happened.

## Starting state
| Ref | Commit | Base |
|---|---|---|
| `feat/mvp-4-dashboard` (main working tree) | `c327bbd` | — |
| `task/m2-e1` (worktree `tsc-e1`) | `4e88ba0` (E-1), then E-1b `5f623e9` on top | `c327bbd` |
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

## Result (2026-10-30)
**Phase 1, pre-checks: passed.**
- Refs as expected: `feat/mvp-4-dashboard` at `c327bbd`, `task/m2-e1` at `5f623e9`, `task/m2-e3` at `e5a5e2e`.
- The working tree had no tracked changes; only untracked owner data, which was preserved.
- No path collision between the incoming files and untracked or ignored files.
- No runtime process of this application was running, and no application port was listening.
- Rollback point: local annotated tag `pre-m2-integration` → `c327bbd` (never pushed).

**Phase 2, integration.**
1. `git merge --ff-only task/m2-e1`: `c327bbd` → `5f623e9` (E-1 `4e88ba0`, E-1b `5f623e9`).
2. `git merge --no-ff task/m2-e3` → merge commit `03c9158`.
   - The only conflict was `docs/devlog/metrics.csv`, as planned. Both sides only appended rows: 5 on the E-1
     side, 1 on the E-3 side.
   - It was resolved by deleting the three conflict-marker lines and nothing else. The result was verified
     byte-for-byte to equal the E-1 side's file followed by the E-3 row. No historical row was changed or
     reformatted.
3. Both task branches and all worktrees are unchanged.

**Phase 3, verification** (main working tree at `03c9158`, bounded sequential batches):

| Batch | Result |
|---|---|
| Simulator (`tests/unit/sim`, golden and determinism tests included) | 296 passed |
| Backtest runner (`tests/unit/backtest`, golden pins, E-1/E-1b regression) | 103 passed |
| Research, strategies, risk | 235 passed |
| Archive importer and lake (`tests/unit/archive`, `tests/integration/archive`, E-3 integrity tests) | 102 passed, 1 skipped (the existing symlink skip) |
| Paper trading and API (MVP-2 to MVP-4, restore, O-4 capture tool, sessions, event store, contracts) | 158 passed |
| Simulator property tests (`tests/property/sim`) | 8 passed, 1 failed (pre-existing, below) |
| `ruff check`, `ruff format --check`, `mypy src` | clean (116 source files) |
| Frontend: `tsc --noEmit`, vitest | clean; 97 tests in 10 files passed |

- **The one failure is pre-existing.** `test_restore_after_any_journal_failure` fails on seed 13073
  (`fail_batch` 124): the documented open restore-equivalence defect (see the
  [sim property devlog](../devlog/2026-10-27-sim-property-defects.md)).
- It was reproduced identically on the pre-integration code (`c327bbd`) and on the integrated code (`03c9158`):
  "ledgers differ after restore at batch 49". The test was not weakened or skipped.

**Independent review: PASS, no blocking findings.**
- The merge equals the union of both sides, byte-identical in both directions apart from `metrics.csv`.
- In code, the two sides touch disjoint files. E-3 only adds functions and manifest keys, and dataset IDs are
  unchanged.
- Non-blocking findings:
  - stale status statements (reconciled in the follow-up docs commit);
  - absolute local paths in the E-3 devlog (removed in the same commit);
  - pre-existing malformed rows in `metrics.csv` (left untouched);
  - new dataset manifests record an absolute package path (in data files only, not in git or the API).

**Paper sessions** (file metadata only; nothing opened, restored, migrated or reset):
- there is no operator session log (`paper_sessions.jsonl`), so there are no operator-created sessions;
- paper is disabled in `configs/paper.yaml`;
- any journal of the default session would be inside the application state database.

Under sim-v1, a sim-v0 journal fails closed instead of restoring. Starting a new session is the owner's separate
decision.
