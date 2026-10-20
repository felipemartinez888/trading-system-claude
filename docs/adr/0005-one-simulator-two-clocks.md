# ADR-0005: One simulator, two clocks

- **Status:** Proposed (2026-10-19)

## Context
Separate backtest and paper engines inevitably diverge in fill logic, fees, rounding and timing, and
the divergence is invisible until real results disappoint.

## Decision
A single event-driven simulator (orders, fills, ledger, books, risk hooks) consumes closed bars from a
`Clock` abstraction: `HistoricalClock` (lake) or `LiveClock` (bar builder). Replaying a recorded live
session through `HistoricalClock` must produce the identical event sequence and ledger hash; this is a
CI test.

## Consequences
- Parity by construction; paper results are directly comparable with backtests.
- The simulator must be fast enough in pure Python for multi-year 1m backtests (target: one symbol-year
  of 1m bars for one strategy in < 30 s on a laptop); optimize with Polars pre-computation and, if
  needed, numba for the bar loop.

## Alternatives
Vectorized backtester (fast but cannot share code with live) + separate paper engine; third-party
backtest frameworks (opaque fill assumptions, harder to audit).
