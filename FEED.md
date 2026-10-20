# Feed de agentes — Trading System Claude

Actualizado: **2026-10-20 00:09** (hora de Costa Rica). Se refresca cada ~3 minutos mientras los agentes trabajan. Solo muestra acciones (herramienta + descripción), nunca contenido de archivos ni secretos. El historial de commits de esta rama es el registro.

## M0-T4 · API, dashboard y lanzador Windows
Agente `platform-ui-engineer` · rama `task/m0-t4` · 93 acciones · última actividad 00:09 · commits 1 · archivos con cambios 15

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
- `00:09` **Write** — scripts/README.md
- `00:08` **Bash** — Rewrite cmd launcher cleanly and check labels
- `00:08` **Bash** — Add one-time bundle build to launchers
- `00:08` **Bash** — Rewrite launchers without fragile timestamp logic
- `00:07` **Bash** — Write Windows launcher scripts with CRLF
- `00:05` **Bash** — Syntax-check frontend with esbuild; typecheck non-React modules
- `00:05` **Bash** — Apply review fixes to tests, hooks, logs page, app
- `00:03` **Write** — frontend/src/api/client.test.ts
- `00:03` **Write** — frontend/src/App.test.tsx
- `00:03` **Write** — frontend/src/components/StatusBar.test.tsx

## M1-T1 · Ingesta en vivo de Binance
Agente `data-engineer` · rama `task/m1-t1` · 51 acciones · última actividad 00:09 · commits 1 · archivos con cambios 5

**Commits:**
- `dad0bb7 wip(m1-t1): checkpoint before usage-limit pause (unreviewed, untested)`

**Archivos tocados (sin commit):**
- `M src/tsclaude/marketdata/bars.py`
- `M src/tsclaude/quality/gaps.py`
- `M src/tsclaude/store/livebars.py`
- `?? src/tsclaude/marketdata/backfill.py`
- `?? src/tsclaude/marketdata/binance/rest.py`

**Últimas acciones:**
- `00:09` **Bash** — See how Sourced generic is used in tests
- `00:08` **Bash** — Check spike script for WS URL forms used
- `00:07` **Bash** — Fix REST pagination typing and fallback counting
- `00:07` **Write** — src/tsclaude/marketdata/binance/rest.py
- `00:05` **Bash** — Make observation index range-based; add first_open_time to bar store
- `00:04` **Write** — src/tsclaude/marketdata/backfill.py
- `00:02` **Write** — src/tsclaude/marketdata/bars.py
- `00:01` **Edit** — src/tsclaude/quality/gaps.py
- `00:01` **Read** — src/tsclaude/quality/gaps.py
- `00:00` **Bash** — List gaps.py signatures

## M2-T2 · Simulador (órdenes, fills, ledger)
Agente `sim-risk-engineer` · rama `task/m2-t2` · 20 acciones · última actividad 00:08 · commits 1 · archivos con cambios 1

**Commits:**
- `1758a26 wip(m2-t2): checkpoint before usage-limit pause (unreviewed, untested)`

**Archivos tocados (sin commit):**
- `?? src/tsclaude/sim/fills.py`

**Últimas acciones:**
- `00:08` **Bash** — Rewrite resolve_exit with conservative same-bar SL rule
- `00:06` **Write** — src/tsclaude/sim/fills.py
- `00:00` **Bash** — Check WIP checkpoint commit and worktree state
- `20:03` **Write** — src/tsclaude/sim/orders.py
- `19:56` **Write** — src/tsclaude/sim/costs.py
- `19:51` **Write** — src/tsclaude/sim/clock.py
- `19:45` **Write** — src/tsclaude/sim/money.py
- `19:36` **Bash** — Look up documented cost defaults and lifecycle notes
- `19:31` **Bash** — Search for any local hypothesis installation
- `19:20` **Bash** — Check hypothesis/numpy availability and gitignore

## M2-T3 · Estadística: win rate certificado
Agente `quant-engineer` · rama `task/m2-t3` · 37 acciones · última actividad 00:09 · commits 1 · archivos con cambios 4

**Commits:**
- `f24daa1 wip(m2-t3): checkpoint before usage-limit pause (unreviewed, untested)`

**Archivos tocados (sin commit):**
- `?? src/tsclaude/research/stats/bootstrap.py`
- `?? src/tsclaude/research/stats/breakeven.py`
- `?? src/tsclaude/research/stats/multiple_testing.py`
- `?? src/tsclaude/research/stats/power.py`

**Últimas acciones:**
- `00:08` **Bash** — Sanity-check bootstrap indices and block length vs AR(1) theory
- `00:08` **Write** — src/tsclaude/research/stats/bootstrap.py
- `00:06` **Bash** — Validate n_min_exact against brute-force scan
- `00:06` **Write** — src/tsclaude/research/stats/power.py
- `00:04` **Bash** — Time binomial tail evaluation by n
- `00:03` **Write** — src/tsclaude/research/stats/multiple_testing.py
- `00:03` **Write** — src/tsclaude/research/stats/breakeven.py
- `00:00` **Bash** — Exact integer-arithmetic tail check vs implementation
- `00:00` **Bash** — Check WIP checkpoint commit state
- `19:52` **Bash** — Compare mine and scipy against exact Fraction sums at n up to 3000

