# ADR-0002: Single asyncio process with a research worker pool

- **Status:** Accepted (2026-10-19)

## Context
The app must start and stop simply on a personal Windows PC, with no services to install, while
ingesting several WebSocket streams and running CPU-heavy backtests on demand.

## Decision
One Python process running an asyncio event loop hosts the supervisor, ingestors, bar builder,
signal/risk engines, paper simulator, agent queue and the FastAPI server. CPU-bound work (archive
import, backtests, walk-forward) runs in a `ProcessPoolExecutor`; workers read the Parquet lake and
write results/events through the main process's single writer.

## Consequences
- One lifecycle to manage; graceful shutdown in one place; no broker.
- Ingestion latency protected from research jobs by process isolation.
- Must keep all I/O non-blocking in the main loop (enforced by review and an event-loop lag metric).
- Horizontal scaling is not a goal; migrating to a mini-PC is the same process.

## Alternatives
Microservices + Redis/NATS (operational burden on Windows); separate ingestor and API processes with
IPC (more failure modes, harder start/stop semantics).
