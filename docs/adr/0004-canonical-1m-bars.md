# ADR-0004: Canonical 1-minute bars; higher timeframes derived

- **Status:** Accepted (2026-10-19)

## Context
Strategies use 5m/15m/1h, but SL/TP resolution inside a strategy bar needs a finer path, and storing
each timeframe separately creates multiple sources of truth.

## Decision
Store 1m bars as the canonical market record (spot and perps; mark price 1m for perps). Derive higher
timeframes by deterministic UTC-aligned aggregation. A periodic check compares derived bars against
native Binance klines on a sample and reports discrepancies.

## Consequences
- One source of truth; intrabar ambiguity resolved at 1m with conservative tie rules.
- Roughly 5× more rows than 5m-only storage (still a few GB for the planned universe).
- Ambiguity *within* a 1m bar still exists and is handled pessimistically.

## Alternatives
Native klines per timeframe (consistency risk, no intrabar path); 1s klines (much larger, spot only).
