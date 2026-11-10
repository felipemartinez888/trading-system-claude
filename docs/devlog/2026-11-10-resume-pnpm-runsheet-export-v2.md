# 2026-11-10: resume after the owner's smoke check: pnpm 12, H1 run sheet, M5 export v2

Lead session. Branches `task/mvp-integration` and `task/m5-runtime-wiring` (separate, not merged).
Directive: "TSC — Resume autonomous MVP development (post-audit integration and dashboard
validation)". This session froze nothing, recorded no trial, read no holdout data, changed no
dependency or risk rule, and merged nothing.

## Ground truth
- `0955749` was the owner's HEAD and the remote HEAD. CI `42859002211` 7/7 (python ubuntu 2338,
  windows 2336; frontend both OS).
- The research safeguards the directive lists are present at HEAD:
  - 3,000 ms latency;
  - `risk_policy: live`;
  - the `min_rr_net` 1.5 floor, enforced at validate and freeze;
  - embargo validation;
  - the Bonferroni trial correction;
  - feasibility-v2 and power-plan-v2;
  - the evaluation export;
  - the holdout guard.
- Owner smoke check at `0955749`: 7/7 automated checks PASS; visual confirmation pending.

## Work
| Commit | What | Tests |
|---|---|---|
| `6fd2fd7` | `frontend/pnpm-workspace.yaml`: `packages: ["."]` (pnpm 9 requires it) and `allowBuilds: {esbuild: true}` (pnpm >= 10.26; pnpm 11+ read only this). package.json's `pnpm.onlyBuiltDependencies` kept for 10.0–10.25. README section on versions and the lock file (D-G) | `test_frontend_toolchain.py` 2 |
| `c14769c` | `docs/research/H1_FEASIBILITY_RUN.md`, the owner's exact NORMAL PowerShell sequence; power-plan `--out` | `test_owner_feasibility_run.py` 3; power plan 12 |
| `f7a957d` | `docs/research/H1_DECISION_RECORD.md`, blank | — |
| `94e821e` | event-type export guard; M5-3 decision sheet; D-06 interface questions | `test_eval_event_coverage.py` 2 |
| M5 `48ed4d7`, `f5c2538` | export format v2 (`agents.jsonl`, v2 schemas, v1 still checked); import order fix; guard test | `test_agents_export.py` 8; evaluation +3 |

## Findings
- **pnpm.** pnpm 11 and later no longer read the `pnpm` field of package.json (pnpm 11 release
  notes), so the esbuild approval was silently dropped on the owner's pnpm 12.18.0. The local check
  used pnpm 10.28's own config resolution: the workspace file alone yields `onlyBuiltDependencies
  ["esbuild"]`. pnpm 9 rejects a workspace file without `packages`; this is from the docs and a
  known error, and CI exercises it. pnpm 12 itself could not run here. The run sheet §1 has the
  owner rebuild without manual approval.
- **The owner's untracked `frontend/pnpm-workspace.yaml`** would block the next checkout, since git
  refuses to overwrite an untracked file. The run sheet moves it to `%TEMP%` first.
- **PowerShell comma lists.** Per about_Parsing, a native program receives `a,b` as one argument,
  so the earlier commands were not wrong. The run sheet quotes the lists anyway.
- **M5 defect.** No agent_gated session could be exported to M6. The agent events sit inside paper
  batches and the exporter refuses unmapped types there. It failed closed. Fixed by format v2 on
  the M5 branch; details in that branch's devlog. The new guard test on both branches stops a
  future contract type from repeating this.
- **Mode-off identity.** Re-verified independently: the `9c681ca` and `127e445` trees, each with its
  own code, produce the same paper session (39 batches, journal digest `61c6163a…`, ledgers,
  fingerprint).

## Not done, and why
- Lock files and the pnpm pin: owner decision D-G. The options are in the decision record.
- The H1 count and power plans need the owner's local lake. No result was generated here.
- The M5 Agent tab and the real adapter: out of scope (paid API, owner approval).
- Merging M5: D-H.

## Review
Independent review requested for the code commits on both branches; result appended below.

## Feasibility evidence integrated (same session, owner direction at 11:38 local)

- **Evidence.** The owner's 15m and 1h counts at `0955749` were staged read-only and checked:
  content hash, config hash and every counter. The 1h file matches every structural value the
  independent review predicted. Both are kept byte for byte in `docs/research/evidence` and pinned
  by `test_h1_feasibility_evidence.py`.
- **Findings.** In `docs/research/H1_FEASIBILITY_FINDINGS.md`:
  - 15m is a P&L-free no-go;
  - 1h is measured and awaits the owner's review;
  - the independent review is reconciled claim by claim.
- **Code.**
  - feasibility-v3 splits registered downtime from unexplained missing data with the runner's
    registry, and records the counting code;
  - power-plan-v3 adds training-window ceilings per fold;
  - both write LF on every platform. The owner's v2 files have CRLF from text mode on Windows.
- **The February 2022 mismatch.** Origin `08f8953`; the run sheet now counts from the anchor.
- **Unchanged:** H1-v3, the floor, the cost model.
