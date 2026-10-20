# ADR-0003: SQLite event store + Parquet/DuckDB lake

- **Status:** Proposed (2026-10-19)

## Context
Two different workloads: (1) small, frequent, must-not-lose operational writes (events, signals,
fills, sessions, live bars); (2) large, read-heavy, columnar historical analytics.

## Decision
- **`state.db` (SQLite, WAL, synchronous=FULL for event commits):** append-only `events` table with
  monotonic `seq`, typed JSON payload, `prev_hash`, `hash = SHA-256(prev_hash ‖ canonical_json)`;
  projections (signals, positions, ledger, gaps, sessions, cost ledger) rebuilt from events on demand.
  Single writer in the main process.
- **Parquet lake + `catalog.duckdb`:** immutable, partitioned files for bars, funding, mark price;
  content-addressed `dataset_id`; DuckDB views for queries and backtest loading.
- Live closed bars land in `state.db` first and are compacted to Parquet daily and on clean shutdown.

## Consequences
- Zero-install, crash-safe, portable files; tamper-evidence for pre-outcome commitment.
- Two storage technologies to test; compaction must be idempotent and verified.

## Alternatives
Postgres/TimescaleDB (install and service management on Windows); DuckDB for everything (weaker for
many small concurrent transactional writes); CSV (no types, slow, error-prone).
