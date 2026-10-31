# Architecture Decision Records

| ADR | Title | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-single-process-runtime.md) | Single asyncio process with a research worker pool | Accepted |
| [0003](0003-storage.md) | SQLite event store + Parquet/DuckDB lake | Accepted |
| [0004](0004-canonical-1m-bars.md) | Canonical 1-minute bars; higher timeframes derived | Accepted |
| [0005](0005-one-simulator-two-clocks.md) | One simulator, two clocks | Accepted |
| [0006](0006-windowed-feature-parity.md) | Features as pure windowed functions with a parity test | Accepted |
| [0007](0007-advisory-agents.md) | Claude agents are advisory; shadow books measure value | Accepted |
| [0008](0008-frontend-stack.md) | FastAPI backend + React/TypeScript static frontend | Accepted |
| [0009](0009-preregistration-and-holdout-guard.md) | Pre-registration and an enforced holdout guard | Accepted |
| [0010](0010-provenance-typed-values.md) | Provenance carried in the API | Accepted |
| [0011](0011-hash-chained-event-log.md) | Hash-chained event log with daily chain-head commits | Accepted |
| [0012](0012-win-rate-margin-objective.md) | Selection objective: certified win-rate margin over break-even | Accepted |
| [0013](0013-exchange-wide-interruptions.md) | Exchange-wide interruptions are registered non-trading periods, not missing data | Accepted in principle (D-09, D-10); implemented in E-1/E-1b, integrated locally (merge `03c9158`); read-only QA consumer in E-2, committed and integrated locally (`e973f86`), targeted regression and independent review passed (final review APPROVE WITH NOTES); O-7 not run, M2 not formally accepted, no statistical strategy validation implied |
| [0014](0014-zero-volume-candles.md) | Zero-volume candles are classified, never imputed or removed | Classification accepted (D-11); no-fill rule approved for implementation (D-12, after O-4); rules 4–5 implemented in E-1, integrated locally (merge `03c9158`); QA classification implemented in E-2, committed and integrated locally (`e973f86`), targeted regression and independent review passed (final review APPROVE WITH NOTES); O-7 not run, M2 not formally accepted, no statistical strategy validation implied |
| [0015](0015-qa-explained-open-difference.md) | Narrow QA exception for native-vs-derived open differences | Accepted in principle (D-13); implemented in E-2, committed and integrated locally (`e973f86`), targeted regression and independent review passed (final review APPROVE WITH NOTES); O-7 not run, M2 not formally accepted, no statistical strategy validation implied |

ADRs 0001–0012 accepted by the owner on 2026-10-19. ADRs 0013–0015 proposed on 2026-10-28; owner decisions D-09 … D-13 on 2026-10-28 (see DECISIONS_PENDING).
