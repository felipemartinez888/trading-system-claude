# Architecture Decision Records

| ADR | Title | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-single-process-runtime.md) | Single asyncio process with a research worker pool | Proposed |
| [0003](0003-storage.md) | SQLite event store + Parquet/DuckDB lake | Proposed |
| [0004](0004-canonical-1m-bars.md) | Canonical 1-minute bars; higher timeframes derived | Proposed |
| [0005](0005-one-simulator-two-clocks.md) | One simulator, two clocks | Proposed |
| [0006](0006-windowed-feature-parity.md) | Features as pure windowed functions with a parity test | Proposed |
| [0007](0007-advisory-agents.md) | Claude agents are advisory; shadow books measure value | Proposed |
| [0008](0008-frontend-stack.md) | FastAPI backend + React/TypeScript static frontend | Proposed |
| [0009](0009-preregistration-and-holdout-guard.md) | Pre-registration and an enforced holdout guard | Proposed |
| [0010](0010-provenance-typed-values.md) | Provenance carried in the API | Proposed |
| [0011](0011-hash-chained-event-log.md) | Hash-chained event log with daily chain-head commits | Proposed |

"Proposed" ADRs become "Accepted" when the owner approves the plan.
