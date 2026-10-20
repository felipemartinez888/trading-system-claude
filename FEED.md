# Feed de agentes — Trading System Claude

Actualizado: **2026-10-20 00:12** (hora de Costa Rica). Se refresca cada ~3 minutos mientras los agentes trabajan. Solo muestra acciones (herramienta + descripción), nunca contenido de archivos ni secretos. El historial de commits de esta rama es el registro.

## M0-T4 · API, dashboard y lanzador Windows
Agente `platform-ui-engineer` · rama `task/m0-t4` · 95 acciones · última actividad 00:10 · commits 1 · archivos con cambios 15

**Commits:**
- `3416ba7 wip(m0-t4): checkpoint before usage-limit pause (unreviewed, untested)`

**Archivos tocados (sin commit):**
- `M frontend/src/api/hooks.ts`
- `M src/tsclaude/api/app.py`
- `?? frontend/src/App.test.tsx`
- `?? frontend/src/App.tsx`
- `?? frontend/src/api/client.test.ts`
- `?? frontend/src/components/EmptyState.tsx`
- `?? frontend/src/components/ProvenanceBadge.test.tsx`
- `?? frontend/src/components/StatusBar.test.tsx`
- `?? frontend/src/main.tsx`
- `?? frontend/src/pages/`
- `?? frontend/src/styles.css`
- `?? frontend/src/test/`
- … y 3 más

**Últimas acciones:**
- `00:10` **Bash** — Run strict mypy using system site-packages
- `00:10` **Bash** — Run strict mypy for linux and win32 platforms
- `00:09` **Write** — scripts/README.md
- `00:08` **Bash** — Rewrite cmd launcher cleanly and check labels
- `00:08` **Bash** — Add one-time bundle build to launchers
- `00:08` **Bash** — Rewrite launchers without fragile timestamp logic
- `00:07` **Bash** — Write Windows launcher scripts with CRLF
- `00:05` **Bash** — Syntax-check frontend with esbuild; typecheck non-React modules
- `00:05` **Bash** — Apply review fixes to tests, hooks, logs page, app
- `00:03` **Write** — frontend/src/api/client.test.ts

## M1-T1 · Ingesta en vivo de Binance
Agente `data-engineer` · rama `task/m1-t1` · 54 acciones · última actividad 00:12 · commits 1 · archivos con cambios 6

**Commits:**
- `dad0bb7 wip(m1-t1): checkpoint before usage-limit pause (unreviewed, untested)`

**Archivos tocados (sin commit):**
- `M src/tsclaude/marketdata/bars.py`
- `M src/tsclaude/quality/gaps.py`
- `M src/tsclaude/store/livebars.py`
- `?? src/tsclaude/marketdata/backfill.py`
- `?? src/tsclaude/marketdata/binance/rest.py`
- `?? src/tsclaude/marketdata/binance/ws.py`

**Últimas acciones:**
- `00:12` **Bash** — Bound gap retries and scope caught_up to live gaps
- `00:12` **Bash** — Simplify the WS serve loop wake-up logic
- `00:11` **Write** — src/tsclaude/marketdata/binance/ws.py
- `00:09` **Bash** — See how Sourced generic is used in tests
- `00:08` **Bash** — Check spike script for WS URL forms used
- `00:07` **Bash** — Fix REST pagination typing and fallback counting
- `00:07` **Write** — src/tsclaude/marketdata/binance/rest.py
- `00:05` **Bash** — Make observation index range-based; add first_open_time to bar store
- `00:04` **Write** — src/tsclaude/marketdata/backfill.py
- `00:02` **Write** — src/tsclaude/marketdata/bars.py

## M2-T2 · Simulador (órdenes, fills, ledger)
Agente `sim-risk-engineer` · rama `task/m2-t2` · 21 acciones · última actividad 00:10 · commits 1 · archivos con cambios 2

**Commits:**
- `1758a26 wip(m2-t2): checkpoint before usage-limit pause (unreviewed, untested)`

**Archivos tocados (sin commit):**
- `?? src/tsclaude/sim/fills.py`
- `?? src/tsclaude/sim/ledger.py`

**Últimas acciones:**
- `00:10` **Write** — src/tsclaude/sim/ledger.py
- `00:08` **Bash** — Rewrite resolve_exit with conservative same-bar SL rule
- `00:06` **Write** — src/tsclaude/sim/fills.py
- `00:00` **Bash** — Check WIP checkpoint commit and worktree state
- `20:03` **Write** — src/tsclaude/sim/orders.py
- `19:56` **Write** — src/tsclaude/sim/costs.py
- `19:51` **Write** — src/tsclaude/sim/clock.py
- `19:45` **Write** — src/tsclaude/sim/money.py
- `19:36` **Bash** — Look up documented cost defaults and lifecycle notes
- `19:31` **Bash** — Search for any local hypothesis installation

## M2-T3 · Estadística: win rate certificado
Agente `quant-engineer` · rama `task/m2-t3` · 52 acciones · última actividad 00:12 · commits 1 · archivos con cambios 8

**Commits:**
- `f24daa1 wip(m2-t3): checkpoint before usage-limit pause (unreviewed, untested)`

**Archivos tocados (sin commit):**
- `M src/tsclaude/research/stats/binomial.py`
- `?? src/tsclaude/research/stats/__init__.py`
- `?? src/tsclaude/research/stats/bootstrap.py`
- `?? src/tsclaude/research/stats/breakeven.py`
- `?? src/tsclaude/research/stats/certify.py`
- `?? src/tsclaude/research/stats/multiple_testing.py`
- `?? src/tsclaude/research/stats/power.py`
- `?? src/tsclaude/research/stats/sharpe.py`

**Últimas acciones:**
- `00:12` **Bash** — Exercise certify scenarios and DSR example
- `00:12` **Bash** — Re-run lint, format, mypy
- `00:12` **Edit** — src/tsclaude/research/stats/multiple_testing.py
- `00:12` **Bash** — Re-run lint, format, mypy
- `00:12` **Edit** — src/tsclaude/research/stats/multiple_testing.py
- `00:12` **Bash** — Re-run lint, format, mypy
- `00:12` **Edit** — src/tsclaude/research/stats/power.py
- `00:12` **Read** — src/tsclaude/research/stats/power.py
- `00:12` **Edit** — src/tsclaude/research/stats/multiple_testing.py
- `00:12` **Bash** — Fix long lines and re-run lint and mypy

