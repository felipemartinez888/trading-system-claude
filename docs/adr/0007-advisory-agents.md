# ADR-0007: Claude agents are advisory; deterministic gate decides; shadow books measure value

- **Status:** Proposed (2026-10-19)

## Context
The product requires multi-agent AI evaluation, but decisions must be reproducible, risk controls
unbypassable, and the app functional without the API. Multiple same-model agents are not
statistically independent.

## Decision
- Two LLM roles (Analyst, Skeptic) with schema-validated outputs referencing evidence-packet fields;
  a deterministic synthesis table maps their outputs to APPROVED / DOWNGRADED / VETOED_AGENT.
- Agents can only approve, downgrade or veto; risk gate runs before and after.
- Packets mask symbol, dates and absolute prices; reviews are cached by packet+prompt+model hash.
- Paper engine runs `baseline` and `agent_gated` books in parallel so agent value is measured
  prospectively; vetoed signals keep shadow outcomes.

## Consequences
- Bounded cost (2 calls per reviewed signal), reproducible records, measurable incremental value.
- Less "agent theatre" than six-agent debates; additional roles can be added later only if the
  ablation shows the current ones add value.
