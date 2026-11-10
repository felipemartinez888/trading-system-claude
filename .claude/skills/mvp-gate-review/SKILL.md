---
name: mvp-gate-review
description: Review the evidence for one TSC milestone gate (M0–M7, or an MVP acceptance criterion AC-xx) against its literal text in docs/ROADMAP.md, and return a PASS / FAIL / INCONCLUSIVE / scope-limited recommendation with classified findings. Use when asked whether a milestone, gate or criterion is met, or before asking the owner for an acceptance decision. Advises only; never accepts.
---

# mvp-gate-review

Argument: a milestone (`M1`, `M3`, …) or criterion (`AC-08`). If none is given, ask which one.

## Hard rules
1. **The gate is the literal text.** Quote it from `docs/ROADMAP.md` ("Milestones" section, the
   `**Gate:**` line, or the "MVP acceptance criteria" list). Do not paraphrase it into something
   stricter or looser. Never add a clause, a test or a run length that the text does not contain.
2. **Owner authority.** Formal acceptance is the owner's alone and lives in
   `docs/DECISIONS_PENDING.md` (D-numbers). This review only *recommends*. If the owner has already
   decided, report that decision verbatim, with its D-number, and review only the material
   **new** evidence since it. Do not reopen an accepted gate without that new evidence.
3. **Do not touch research integrity.** No holdout access or unlock, no edits to preregistrations,
   protocols, ADR-0012 thresholds or acceptance criteria. If a criterion looks wrong, escalate it to the
   owner as a finding; never weaken it.
4. **Read-only.** No commits, pushes, config changes or long test suites. Cite existing evidence
   (CI run ids, evidence folders, devlogs). At most one focused test command, and only if the user asks.
5. **Truthful status.** "Demonstrated" is not "accepted". Tests passing on synthetic data are not a
   real-data result. A dashboard display is not validation.

## Procedure
1. Read `docs/STATUS.md`, then the gate text in `docs/ROADMAP.md`, then the decision rows in
   `docs/DECISIONS_PENDING.md` that mention the milestone.
2. Split the gate into its clauses. For each clause, find the strongest existing evidence (path,
   commit, CI run, test id) and give it one verdict:
   - `PASS`: the evidence meets the clause as written.
   - `FAIL`: the evidence contradicts it.
   - `INCONCLUSIVE`: the evidence is missing, partial or for a different configuration or commit.
3. Overall verdict:
   - `PASS`: every clause passes.
   - `SCOPE-LIMITED PASS (recommend)`: every clause passes within a stated scope (commit,
     configuration, operating mode), and the limitations are listed.
   - `FAIL`: at least one clause fails.
   - `INCONCLUSIVE` / `OPEN`: anything else.
   If the owner has decided, add: `Owner decision: D-xx (<date>)`.
4. Classify every finding:
   - **BLOCKING**: prevents safe or correct use within the requested scope.
   - **NON-BLOCKING**: should be fixed but does not prevent that scope.
   - **DEFERRED**: matters only for a later operating mode, or is optional.
   Environmental failures (e.g. native crashes on the owner PC) are recorded as they happened, with
   retries and their outcome, and labeled "cause not established" unless evidence proves a cause.

## Output (in chat; no file unless asked)
```markdown
## Gate review: <milestone> (<date>, at <commit>)
Gate (literal): "<quote>"
| Clause | Evidence | Verdict |
Overall: <verdict> · Scope: <commit / config / mode> · Owner decision: <D-xx or none>
Findings: BLOCKING … / NON-BLOCKING … / DEFERRED …
Not implied: <what this verdict does not mean, e.g. no strategy validated, no live trading>
Recommended owner action: <one line>
```
