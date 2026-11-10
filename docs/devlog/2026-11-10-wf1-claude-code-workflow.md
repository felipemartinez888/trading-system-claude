# 2026-11-10: WF-1, Claude Code workflow migration (docs and config only)

Owner-approved plan WF-1. The work happens in worktree `C:\Users\felip\Projects\tsc-cc-workflow`, on branch `task/cc-workflow` from
`f03da15`. Nothing is committed or pushed. No `src/`, `configs/` or `tests/` change.

## What changed
- **Skills:** `.claude/skills/checkpoint-handoff/SKILL.md` and `.claude/skills/mvp-gate-review/SKILL.md`, both advisory only.
- **`docs/STATUS.md`:** a one-page current state with four separate status levels. It points to the records and never overrides them.
- **D-24:** the owner's M3 acceptance, recorded verbatim in DECISIONS_PENDING.
  - D-A is marked decided there.
  - The M3 rows in ROADMAP (live status and reconciliation table) gained a D-24 prefix. The earlier text is kept as history and the gate text is unchanged.
  - M3_ACCEPTANCE_REQUEST and MVP_TASK_QUEUE gained a "decided" note.
- **Agents:** the five builder agents now have `model: sonnet`. `reviewer` and `causality-auditor` are unchanged and inherit the session model.
- **`.claude/settings.json` (project, new):**
  - deny: force-push, remote delete, mirror push, `git reset --hard`, in both the Bash and PowerShell forms;
  - deny: Read/Edit (Edit covers all file-editing tools) under the original `trading-system` repository (CLAUDE.md rule 1);
  - ask: every other `git push`.
- **Owner-local `C:\Users\felip\Projects\trading-system-claude\.claude\settings.local.json`:**
  - backed up to `settings.local.json.bak-20261110`;
  - only `Bash(git push *)` removed (39 → 38 allow rules), with nothing else changed (checked by a JSON comparison).
- **`CLAUDE.md`:** a pointer to STATUS.md plus a 9-line "Claude Code workflow" section.

## Verification
| Check | Result |
|---|---|
| Permission precedence mock (`deny > ask > allow`, isolated script, no git command executed): 15 cases | 15/15 PASS. The first run had 1 FAIL, caused by the mock not handling the `//` absolute-path prefix; the mock was fixed and the rule left unchanged |
| Both settings files parse as JSON | PASS |
| Skill frontmatter (`name` = folder, description < 1024 chars) | PASS (both) |
| Relative links and anchors in the 6 edited or added Markdown files | 0 problems |
| `git diff f03da15 -- src configs tests` | empty |
| Gate-review smoke test, M3 | Gate quoted literally (ROADMAP l.163). Replay clause PASS (Phase C 186/186, hashes 3/3, plus the 3 latency-0 sessions). Fault-injection clause PASS by CI `42888483003` (tests cited in D-A). Overall **SCOPE-LIMITED PASS. Owner decision: D-24 (2026-11-10)**. E-1, E-2, E-3 and the screenshot WARN are listed as NON-BLOCKING, cause not established. U1 and U2 are carried. No new mandatory item |
| Gate-review smoke test, M1 | Gate quoted (ROADMAP l.149). The 2-hour run has no accepted evidence (INCONCLUSIVE), and no D-number accepts M1. Overall **OPEN** |
| Checkpoint-handoff smoke test | [docs/checkpoints/2026-11-10-wf1-claude-code-workflow.md](../checkpoints/2026-11-10-wf1-claude-code-workflow.md) written from the template, with every claim linked or labeled |

The smoke tests were run by following the skills' procedures in the implementing session itself.

Fresh-session attempt (2026-11-10, `claude -p` in the worktree, plan mode, read-only tools):
- **Not completed:** `Failed to authenticate: OAuth session expired`. Neither skill ran in a fresh session. This is an environment limitation, not a skill failure. To repeat it, re-authenticate the CLI (owner) and run `claude -p "/mvp-gate-review M1"` from the worktree.
- **What the attempt did show:** the CLI loaded the worktree `.claude/settings.json` and flagged the `Write(//…/trading-system/**)` deny rule as inert, because only `Edit(path)` rules are checked for file edits, Write included.
- **Fix:** the redundant `Write` rule was removed. Nothing is loosened: the `Edit` deny rule already covers Write.

## Notes
- The project `settings.json` deny rules take effect only in sessions whose project root is a checkout containing the file:
  this worktree now, every checkout after a merge. The main checkout (`task/m3-partb`) is not covered until then. The
  local `git push *` removal applies to the main checkout already.
- Deny rules match command strings only. A shell could still reach the original repository through `cat` and similar commands, so CLAUDE.md
  rule 1 remains the governing control.

## Independent review
- `reviewer`, fresh context, read-only, verdict **CHANGES REQUESTED**.
- **The only BLOCKING finding:** the checkpoint cited above did not exist yet at review time. It has been written since.
- **Other notes, all NON-BLOCKING and recorded for the owner, not applied:**
  - README and MVP_SEMANTIC_MAP drift, now listed in STATUS.
  - Extra deny forms (`git push -d`, `:ref`, `+ref`, `git -C <path> push --force|reset --hard`, aliases via the local `git config *` allow). These forms reach an ask or default prompt, so they are never silently allowed.
  - The local settings edit, which the owner had explicitly authorized.
  - The STATUS anchor NIT, fixed.
  - The CI `42888483003` NIT: the lead verified it with `gh run view` (success, `f03da15`, 7/7).

