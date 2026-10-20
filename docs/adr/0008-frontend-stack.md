# ADR-0008: FastAPI backend + React/TypeScript static frontend

- **Status:** Accepted (2026-10-19)

## Context
The dashboard needs real-time updates, dense professional tables and charts, and many views; the end
user should not need a Node toolchain to run the app.

## Decision
FastAPI serves REST read models and a `/ws/live` push channel, plus the built React/TypeScript (Vite)
bundle from `src/tsclaude/web/static`. TypeScript types are generated from the OpenAPI schema.
Charts: lightweight-charts (price) and ECharts (analytics). Bound to 127.0.0.1 with a per-launch token
and Origin checks for mutating requests.

## Consequences
- Professional UX and live push; typed end-to-end contract.
- Two toolchains for developers (Python + Node/pnpm); CI builds the bundle.

## Alternatives
Streamlit (rerun model unsuited to live push and dense multi-page apps), Dash/NiceGUI (viable but
weaker component ecosystem for this scope), Electron/Tauri desktop shell (packaging overhead; can wrap
the same web app later).
