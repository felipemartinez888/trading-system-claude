# Claude-Only Multi-Agent Development Workflow

All AI development and AI review in this repository is done by Claude (lead session + Claude
subagents). Conventional deterministic tools (git, pytest, ruff, mypy, CI) are used freely; they do
not make the process multi-model. All reviews are labeled **mono-model review** in their records.

## 1. Roles

Five builder roles and two reviewer roles — enough to separate concerns and give reviews a fresh
context, not so many that coordination costs exceed the work.

| Role | Owns (write access) | Typical deliverable |
|---|---|---|
| **Lead / Integrator** (main session) | `docs/`, `src/tsclaude/contracts/`, `configs/`, CI, merges | ADRs, task briefs, contract changes, integration, roadmap status |
| **Data Engineer** | `marketdata/`, `quality/`, `store/` | Ingestors, bar builder, importer, gap repair + tests |
| **Quant Engineer** | `features/`, `strategies/`, `research/`, `evaluation/`, `preregistrations/` (drafts only) | Features, strategies, walk-forward, statistics, reports |
| **Simulation & Risk Engineer** | `sim/`, `risk/`, `signals/` | Simulator, ledger, books, risk gate, lifecycle |
| **Platform & UI Engineer** | `runtime/`, `api/`, `frontend/`, `scripts/` | Supervisor, API, dashboard, Windows launcher |
| **AI Agents Engineer** | `agents/` | Evidence packets, prompts, client, synthesis, budget |
| **Reviewer** (fresh context per review) | read-only | Review record: contract conformance, tests, errors, security |
| **Causality Auditor** (fresh context) | read-only + may add failing tests | Look-ahead / leakage / survivorship audit on anything touching features, strategies, sim, research |

Ownership prevents concurrent edits to shared files. Only the Lead edits `contracts/`; a builder who
needs a contract change requests it in its report, and the Lead applies it via a small ADR-backed commit
before dependent work proceeds.

## 2. Unit of work

```mermaid
sequenceDiagram
  participant L as Lead
  participant B as Builder (own worktree)
  participant R as Reviewer
  participant C as Causality Auditor
  participant CI as CI (Win + Linux)
  L->>B: Task brief (goal, contracts, files owned, acceptance tests, out-of-scope)
  B->>B: Implement + tests on branch task/<id>
  B->>L: Report (what changed, evidence, open issues)
  L->>R: Diff + brief only (no builder reasoning)
  L->>C: (if causality-sensitive) Diff + brief
  R->>L: Review record (blocking / non-blocking findings)
  C->>L: Audit record (+ failing tests if leakage found)
  L->>CI: Run full suite
  CI->>L: Pass/fail evidence
  L->>L: Merge only on evidence; update ROADMAP + devlog
```

- Task briefs live in `docs/tasks/<id>.md`; reviews in `docs/reviews/<id>.md`. Both are committed.
- Builders work in isolated git worktrees on `task/<id>` branches; independent tasks run in parallel,
  tasks touching the same module never do.
- Reviewers see the brief and the diff, not the builder's narrative, to reduce anchoring.
- "Done" requires: acceptance tests passing in CI, review record with no open blocking findings,
  roadmap entry updated with links to evidence.
- Blocking findings go back to the builder; a finding that reveals a wrong acceptance criterion is
  escalated to the Lead, who may change the criterion only via a recorded decision (never silently).

## 3. Agent definitions

Concrete subagent definitions are in `.claude/agents/`. Each one states its scope, write boundaries,
required outputs and the rules from `CLAUDE.md`.

## 4. Development metrics (REQ-EVAL-03)

Recorded from day 0 in `docs/devlog/metrics.csv` (one row per task):

`task_id, milestone, role, started_utc, finished_utc, agent_sessions, tokens_in, tokens_out (if
available), est_cost_usd, human_minutes, review_findings_blocking, review_findings_nonblocking,
ci_failures, rework_cycles, defects_escaped_later, notes`

Plus a narrative `docs/devlog/YYYY-MM-DD.md` per working day: decisions, surprises, manual
interventions by the owner. Token counts are recorded only when the tooling exposes them; otherwise
the field is left empty rather than estimated.

## 5. Escalation to the owner

Only for: material scope changes, security risks, destructive repository operations, meaningful
costs (any recurring spend or API budget change), major architectural decisions, and pushes to GitHub
(until the owner grants standing push approval).
