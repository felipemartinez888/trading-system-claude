---
name: checkpoint-handoff
description: Write a one-page, evidence-linked checkpoint of the current TSC task (scope, commit, changed files, tests, evidence, blockers, limitations, next approved action) to docs/checkpoints/. Use at the end of a task, before a handoff between sessions or models, or when the owner asks for a checkpoint or status handoff.
---

# checkpoint-handoff

Goal: a later session resumes from this file plus `docs/STATUS.md`, without re-deriving history or
re-verifying accepted work.

## Rules
- **Read-only except the checkpoint file itself.** Do not commit, push, fetch, switch branches or run
  test suites to fill the checkpoint. Report only test runs that already happened in this task, with
  their exact command and exit code.
- **Every claim carries evidence or a label.** Evidence = commit hash, CI run id, file path, or a test
  command plus its result. Otherwise label it `UNVERIFIED` or `OWNER-PROVIDED (not in repo)`.
- **Do not change status.** A checkpoint never accepts, closes or reopens a milestone, gate or
  decision. It quotes `docs/STATUS.md` / `docs/DECISIONS_PENDING.md`. If they look wrong, list it
  under "Findings", classified.
- **Classify findings** as BLOCKING (prevents safe or correct use in the stated scope),
  NON-BLOCKING (fix later; does not prevent the scope) or DEFERRED (later operating mode or optional).
  A new idea is never a new mandatory gate. Put it under "Later".
- **No secrets.** Never copy `.env` values, tokens or keys.
- One page: about 60 lines at most. Link, don't paste.

## Facts to gather (read-only)
```bash
git rev-parse --show-toplevel
git rev-parse HEAD
git branch --show-current
git status --short
git log --oneline -5
git diff --stat <base>..HEAD   # base = the task's starting commit
git diff --stat                # uncommitted changes
```
Optional, if a CI run is cited: `gh run view <id> --json conclusion,headSha`.
Then read `docs/STATUS.md` for the milestone levels and open owner decisions.

## Output
Write `docs/checkpoints/YYYY-MM-DD-<task-slug>.md` (UTC date) with exactly these sections:

```markdown
# Checkpoint <task id>: <title> (YYYY-MM-DD)
## 1. Objective and scope
<one or two sentences; what is explicitly out of scope>
## 2. Repository
repo path · branch · HEAD <hash> · base <hash> · committed? pushed? (yes/no each)
## 3. Files changed
<git diff --stat summary; flag any change under src/, configs/, tests/, contracts/>
## 4. Tests and checks
| Command | Result | Exit | When |
## 5. Evidence
<paths / CI ids / commits; each one verified or labeled>
## 6. Status levels (quoted from STATUS.md, unchanged)
M3 operational · operational MVP · M4 research validity · Paper signal validation
## 7. Findings
BLOCKING / NON-BLOCKING / DEFERRED (each: one line + evidence)
## 8. Documented limitations carried
<IDs only, e.g. E-1, E-2, E-3, U1, U2, NB-1; link to where they are described>
## 9. Next approved action
<only an action the owner has approved; otherwise "awaiting owner decision: <what>">
## 10. Later (not mandatory)
```

Then tell the user the file path and the one-line next action. Nothing more.
