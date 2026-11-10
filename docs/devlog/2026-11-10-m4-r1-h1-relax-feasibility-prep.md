# 2026-11-10: M4-R1, H1 relaxation feasibility preparation (outcome-blind)

**Scope.** This is owner-approved M4-R1. The work is in worktree `C:\Users\felip\Projects\tsc-m4-r1`, on branch `task/m4-r1` from `21c4910` (WF-1).
- Nothing is committed or pushed.
- Nothing changes under `src/` or `configs/`.
- No trial was recorded, no outcome was read, and the holdout was not touched. Nothing is frozen.

## What changed
- **D-25** is recorded in `docs/DECISIONS_PENDING.md`:
  - research direction D with its pre-stated selection rule;
  - T1, T2 and T3 defined against `power-plan-v3` fields, with T3 at 10 points and its economic meaning;
  - D-C a, D-E a with Amendment 6, take-profit set B, D-F1 a, D-G a, F5 a, R-6 a (rung 1).
  
  The D-C, D-E, D-F1 and D-G rows are marked decided. The proposal text is kept.
- **`docs/research/H1_DECISION_RECORD.md`:** the decisions are filled in. R-1 and R-4 stay open.
- **`docs/adr/0012-win-rate-margin-objective.md`:** Amendment 6 added (adopted). Amendments 1–5 are unchanged.
- **`docs/research/grids/feasibility_h1_relax_probe.json`:** 1h, `rank_max` 0.5 / 0.75 / 1.0, with all other parameters at the H1 draft defaults.
- **`docs/research/H1_RELAX_FEASIBILITY_RUN.md`:** the owner run sheet (NORMAL PowerShell). It covers the count, dataset verification, power plans for set B, a threshold read-out and hashing.
- **`tests/unit/backtest/test_owner_relax_feasibility_run.py`:**
  - the grid equals the ladder exactly;
  - every sheet command parses with the real CLI;
  - the count and power plans run end to end on a synthetic lake, and the T1–T3 fields exist;
  - no elevation command appears in the sheet.
- **`uv.lock`** (D-G, Python part). Its provenance has three checks:
  - the file is the untracked lock of `tsc-review-f03da15`, and its SHA-256 `f6b75e9a7a1b81a376f4f5410e598954b0af84dd8d1f75696aded74a2321710b` equals `uv_lock_sha256.txt` in the M3 Phase C evidence;
  - `uv lock --locked --offline` passes against the unchanged `pyproject.toml`;
  - all 38 versions match the Phase A `deps_freeze.txt`, so there is no upgrade.
- **`docs/STATUS.md`:** the next-objective section is updated.

## Verification
| Check | Result |
|---|---|
| `uv run --locked python -m pytest tests/unit/backtest/test_owner_relax_feasibility_run.py tests/unit/backtest/test_owner_feasibility_run.py tests/unit/research/test_h1_feasibility_evidence.py -q` | 11 passed (new sheet 4, existing H1-1 sheet, evidence pins) |
| `ruff check` / `ruff format --check` on the new test | pass, after two lint fixes |
| `git diff 21c4910 -- src configs` | empty |
| Trial registry | untouched: the count runs only on a synthetic lake in tests |

## Findings and limits (recorded, not thresholds)
- **Candidates overstate trades.** The count does not simulate position overlap, consecutive breakout bars on one symbol, portfolio rules or exits. Relaxing compression probably adds more clustered consecutive bars, so the overstatement grows with `rank_max`. T1–T3 are necessary, not sufficient.
- **Cross-symbol correlation** lowers `n_eff`. `n_eff_ratio` 0.75 and 0.5 are reported as diagnostics. Certification keeps its own block-bootstrap and `n_eff` safeguards.
- **Still the owner's:**
  - the R-1 wording;
  - R-4 (before H2);
  - the frontend part of D-G: pnpm lock, `packageManager` pin and frozen CI installs. Those need a CI run, which needs a push.
- **Code identity.** The owner's count records the code commit. If M4-R1 is uncommitted when the count runs, `git_status.txt` in the evidence shows the docs-only dirt.

## Run-sheet verification before the commit (owner request)
- **File patterns** were already correct (`feasibility_1h_rank*.json`, `power_1h_rank*.json`). The apparent directory patterns were Markdown italics in the chat report.
- **Fixed: filename locale.** `"rank{0}" -f 0.5` gives `rank0,5` under a comma-decimal culture (checked under es-ES). The sheet now names files with `[cultureinfo]::InvariantCulture`, giving `rank0.5`, `rank0.75` and `rank1`.
- **Fixed: fail-stop.** Step 1 sets `$stop`. Steps 2–6 are each one guarded block that checks exit codes and the expected file counts (3 counts, 3 power plans, ≥ 1 verified dataset). A failed or empty count therefore cannot produce an evidence zip.
  - Checked by running blocks 2–6 with `$stop = $true`: each printed `STOPPED`, ran nothing, and created no folder.
- **Every block parses** with the PowerShell parser: 6 blocks, 0 errors, not executed.
- **Tests:** a test pins the guards and the invariant naming; the focused tests pass (11) and ruff is clean.
- **Owner notes:** a "Before you start" section was added with NORMAL PowerShell, the time estimate, the resources and the output location.
