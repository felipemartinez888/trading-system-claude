# Checkpoint WF-1: Claude Code workflow migration (2026-11-10)

## 1. Objective and scope
WF-1 moves the verified context into Claude Code with as little machinery as possible: two advisory skills, a single-page STATUS, the D-24 record, Sonnet for the builder agents, and Git safety rules.
**Out of scope:** `src/`, `configs/`, `tests/`, contracts, research gates, holdout, commits, pushes and merges.

## 2. Repository
- **Worktree:** `C:\Users\felip\Projects\tsc-cc-workflow`, branch `task/cc-workflow`.
- **HEAD and base:** both `f03da159443bc17a5edc997982c643ce649d315d`, because the changes are uncommitted.
- **Committed:** no. **Pushed:** no.
- **Outside the worktree:**
  - The owner-local file `C:\Users\felip\Projects\trading-system-claude\.claude\settings.local.json` was edited, as the owner authorized.
  - A backup is next to it: `settings.local.json.bak-20261110`. It shows as untracked in the main checkout.
  - The main checkout ran `git fetch` for `task/mvp-integration` and got a new worktree entry. Nothing else in it changed.

## 3. Files changed
- **Modified:**
  - `CLAUDE.md`
  - `.claude/agents/{data,quant,sim-risk,platform-ui,ai-agents}-engineer.md` (adds `model: sonnet`)
  - `docs/DECISIONS_PENDING.md` (D-24; D-A decided)
  - `docs/ROADMAP.md` (two M3 cells prefixed with D-24; gate text unchanged)
  - `docs/M3_ACCEPTANCE_REQUEST.md`
  - `docs/MVP_TASK_QUEUE.md`
  - `docs/devlog/metrics.csv` (one row)
- **New:**
  - `.claude/settings.json`
  - `.claude/skills/checkpoint-handoff/SKILL.md`
  - `.claude/skills/mvp-gate-review/SKILL.md`
  - `docs/STATUS.md`
  - `docs/devlog/2026-11-10-wf1-claude-code-workflow.md`
  - this file
- **Untouched:** `git diff f03da15 -- src configs tests` is empty.

## 4. Tests and checks
| Command | Result | Exit | When |
|---|---|---|---|
| `python -I permcheck.py` (isolated deny/ask/allow mock, scratchpad) | 15/15 PASS. The first run had 1 FAIL, a mock bug (the `//` path prefix), fixed in the mock | 0 | 2026-11-10 |
| JSON parse of both settings files | valid | 0 | 2026-11-10 |
| Skill frontmatter check | both OK | 0 | 2026-11-10 |
| Relative link and anchor check, 6 Markdown files | 0 problems | 0 | 2026-11-10 |
| Gate-review smoke test, M3 / M1 (procedure followed in the implementing session) | M3: SCOPE-LIMITED PASS, owner decision D-24. M1: OPEN | n/a | 2026-11-10 |
| Fresh-session `claude -p "/mvp-gate-review M1"` and `/checkpoint-handoff` | **Not completed**: CLI OAuth expired. It did surface the inert `Write(...)` deny rule, which was removed (covered by `Edit(...)`) | 1 | 2026-11-10 |
| `gh run view 42888483003` | success, headSha `f03da15`, 7/7 jobs | 0 | 2026-11-10 |
| Independent `reviewer` pass on the diff | CHANGES REQUESTED, 1 BLOCKING (this file missing), now resolved | n/a | 2026-11-10 |

No real `git push`, force-push or reset was executed. No pytest, ruff or mypy run: no code changed.

## 5. Evidence
- `C:\Users\felip\tsc_runs\readiness-f03da15\TSC_M3_DA_evidence_f03da15_20261110-162620.zip`, verified to list 180 files.
- `…\phaseC-attempt1\run-20261110-162620\phaseC_summary.json` (COMPLETE; `screenshots=WARN`).
- `…\phaseA-20261110-150204\gate_tests_exit.txt` (`-1073741819`) and `gate_tests_run2_exit.txt` (0).
- `…\phaseB-attempt1\prepare-20261110-153230\config_digest_first_try_exit.txt` (`-1073741819`).
- `…\linux-crosscheck\phaseC\README.txt`.
- `TSC_M3_DA_VALIDATION_F03DA15.md`: **OWNER-PROVIDED (not in repo)**.

## 6. Status levels (quoted from STATUS.md)
- **M3 operational:** ACCEPTED, scope-limited (D-24).
- **Operational MVP:** supervised Paper, REFERENCE strategy, agents off.
- **M4 research validity:** NOT STARTED.
- **Paper signal validation:** NONE.

## 7. Findings
- **BLOCKING:** none open.
- **NON-BLOCKING:**
  - (a) README and MVP_SEMANTIC_MAP still show M3 and D-A as open. Listed in STATUS, not edited.
  - (b) MVP_LIMITATIONS has stale lines (`allow_all`; M5/M6 "not implemented").
  - (c) Extra deny forms are not covered (`push -d`, `:ref`, `+ref`, `git -C … push --force|reset --hard`, aliases via the local `git config *` allow). They fall back to an ask or default prompt; owner to decide.
  - (d) The project deny rules apply only in checkouts that contain `.claude/settings.json`, which today means this worktree, until a merge.
  - (e) The main checkout `task/m3-partb` is stale.
- **DEFERRED:**
  - the root cause of E-1, E-2 and E-3;
  - worktree and folder cleanup;
  - skills quant-research-review and safe-paper-release;
  - `docs/reviews/` absent.

## 8. Documented limitations carried
E-1, E-2, E-3, the missing screenshots, U1, U2 (P-1, NB-1 / D-21), and D-B. Details are in [DECISIONS_PENDING D-24](../DECISIONS_PENDING.md#2026-11-10-m3-acceptance-d-24) and [MVP_LIMITATIONS](../MVP_LIMITATIONS.md).

## 9. Next approved action
Awaiting the owner's decision on three things:
1. accepting WF-1;
2. authorizing a commit on `task/cc-workflow`;
3. later, a push.

After that comes the M4 path in [STATUS](../STATUS.md): the 1h feasibility direction, then D-C, D-E, D-F1 and D-G, then the H1 freeze.

## 10. Later (not mandatory)
- Fix the drift in (a) and (b).
- Owner decision on the extra deny forms in (c).
- A fresh-session `/mvp-gate-review` run from inside the worktree.
