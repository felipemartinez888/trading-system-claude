# ADR-0006: Features as pure windowed functions with a parity test

- **Status:** Proposed (2026-10-19)

## Context
Incremental (streaming) indicator implementations differ subtly from vectorized research versions,
and look-ahead bugs hide in alignment details.

## Decision
Each feature is a pure, versioned function `f(bars_up_to_T) -> values_at_T` implemented once in
Polars with a declared warm-up length. Research applies it vectorized over full history; live
recomputes it over a trailing window (warm-up + margin) at each bar close. A parity test asserts equal
values (within float tolerance) for every bar after warm-up, and look-ahead canary tests assert that
shifting future data never changes past values.

## Consequences
- Simple, auditable feature code; small recomputation cost live (negligible at 1m cadence for tens of symbols).
- Recursive indicators (EMA) need warm-up long enough to converge within tolerance; declared per feature.
