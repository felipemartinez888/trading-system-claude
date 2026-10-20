# ADR-0010: Provenance carried in the API, not inferred by the UI

- **Status:** Proposed (2026-10-19)

## Context
The dashboard must distinguish live, recovered, historical, replay, backtest, simulated, estimated,
demo, stale and unavailable values. If the frontend guesses, it will eventually guess wrong.

## Decision
Read models return values with a `provenance` enum and, where relevant, `as_of` and `freshness`.
Missing values are explicit `UNAVAILABLE` with a reason, never zero or null-rendered-as-zero. A
contract test fails if any API field rendered as a metric lacks provenance.

## Consequences
Slightly heavier payloads; impossible to show a demo or stale number as live.
