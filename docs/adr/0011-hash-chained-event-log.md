# ADR-0011: Hash-chained event log with daily chain-head commits

- **Status:** Accepted (2026-10-19)

## Context
The A/B comparison requires showing that signals were recorded before outcomes were known, and that
records were not edited afterwards.

## Decision
Each event stores `prev_hash` and `hash`; an integrity check runs at startup and on demand. The chain
head hash is exported daily and at shutdown to `evidence/chain/YYYY-MM-DD.txt` and committed;
pushing those commits provides a third-party (GitHub) timestamp. Optional OpenTimestamps proofs.

## Consequences
Tamper-evident history at negligible cost. Any repair of a corrupted database is itself recorded as
an event and visible in the report.
