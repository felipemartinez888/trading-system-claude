# ADR-0009: Pre-registration and an enforced holdout guard

- **Status:** Accepted (2026-10-19)

## Context
"Sealed holdout" is only a convention unless the code enforces it; repeated peeking silently turns the
holdout into training data.

## Decision
Hypotheses are frozen as hashed, committed YAML before validation. The data access layer refuses to
serve holdout-period bars to research jobs unless a single `holdout_unlock` event exists for that
preregistration and strategy hash. Trial counts are recorded automatically for every evaluated
configuration and feed the deflated Sharpe ratio.

## Consequences
Discipline is enforced and auditable; researchers (agents included) cannot "just check" the holdout.
Warm-up bars before the holdout are served normally.
