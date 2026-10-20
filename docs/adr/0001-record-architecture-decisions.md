# ADR-0001: Record architecture decisions

- **Status:** Accepted (2026-10-19)

## Context
The project is an experiment in Claude-only development; decisions and their rationale are part of
the evidence, and builders/reviewers need a stable reference.

## Decision
Use lightweight ADRs in `docs/adr/NNNN-title.md` (Status, Context, Decision, Consequences,
Alternatives). Decisions that change contracts, storage formats, the simulator, risk rules or the
evaluation protocol require an ADR. Superseded ADRs stay, marked `Superseded by ADR-XXXX`.

## Consequences
Small overhead per significant change; full traceability for reviewers and the final comparison report.
