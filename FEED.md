# Feed de agentes — Trading System Claude

Actualizado: **2026-10-20 00:18** (hora de Costa Rica). Se refresca cada ~3 minutos mientras los agentes trabajan. Solo muestra acciones (herramienta + descripción), nunca contenido de archivos ni secretos. El historial de commits de esta rama es el registro.

## M0-T4 · API, dashboard y lanzador Windows
Agente `platform-ui-engineer` · rama `task/m0-t4` · 122 acciones · última actividad 00:18 · commits 1 · archivos con cambios 20

**Commits:**
- `3416ba7 wip(m0-t4): checkpoint before usage-limit pause (unreviewed, untested)`

**Archivos tocados (sin commit):**
- `M frontend/src/api/hooks.ts`
- `M frontend/src/components/StatusBar.tsx`
- `M src/tsclaude/api/app.py`
- `M src/tsclaude/api/context.py`
- `M tests/integration/test_api.py`
- `M tests/integration/test_api_core.py`
- `?? frontend/src/App.test.tsx`
- `?? frontend/src/App.tsx`
- `?? frontend/src/api/client.test.ts`
- `?? frontend/src/components/EmptyState.tsx`
- `?? frontend/src/components/ProvenanceBadge.test.tsx`
- `?? frontend/src/components/StatusBar.test.tsx`
- … y 8 más

**Últimas acciones:**
- `00:18` **Bash** — Withhold projections on failed chain; rerun tests
- `00:18` **Bash** — Locate coverage and previous-session builders
- `00:18` **Bash** — Confirm preview servers stopped
- `00:17` **Bash** — Stop preview servers and locate derived read models
- `00:17` **Read** — /tmp/claude-0/-home-claude/58cb29c4-0183-51d6-95f4-9bf3173191f4/scratchpad/shots/06-fallb…
- `00:17` **Read** — /tmp/claude-0/-home-claude/58cb29c4-0183-51d6-95f4-9bf3173191f4/scratchpad/shots/05-chain…
- `00:17` **Bash** — Take chain-failure and fallback screenshots
- `00:17` **Bash** — Start preview servers for failure and fallback states
- `00:17` **Bash** — Rebuild preview bundle and seed tampered db
- `00:17` **Bash** — Inspect state after interrupted command

## M1-T1 · Ingesta en vivo de Binance
Agente `data-engineer` · rama `task/m1-t1` · 64 acciones · última actividad 00:18 · commits 1 · archivos con cambios 12

**Commits:**
- `dad0bb7 wip(m1-t1): checkpoint before usage-limit pause (unreviewed, untested)`

**Archivos tocados (sin commit):**
- `M src/tsclaude/marketdata/bars.py`
- `M src/tsclaude/marketdata/binance/__init__.py`
- `M src/tsclaude/marketdata/binance/limits.py`
- `M src/tsclaude/marketdata/config.py`
- `M src/tsclaude/quality/freshness.py`
- `M src/tsclaude/quality/gaps.py`
- `M src/tsclaude/store/livebars.py`
- `?? src/tsclaude/marketdata/backfill.py`
- `?? src/tsclaude/marketdata/binance/rest.py`
- `?? src/tsclaude/marketdata/binance/ws.py`
- `?? src/tsclaude/marketdata/models.py`
- `?? src/tsclaude/marketdata/service.py`

**Últimas acciones:**
- `00:18` **Bash** — Fix WS connection disposal and bounded stop
- `00:16` **Bash** — Show mypy errors only in our sources
- `00:16` **Bash** — Fix typing issues and re-run mypy with httpx visible
- `00:16` **Bash** — Run strict mypy on owned packages
- `00:16` **Bash** — Fix overlong docstring lines and re-lint
- `00:16` **Bash** — Show the overlong lines
- `00:15` **Bash** — Fix SIM118 and show remaining lint errors
- `00:15` **Bash** — Format, lint and import-check new modules
- `00:15` **Write** — src/tsclaude/marketdata/service.py
- `00:13` **Write** — src/tsclaude/marketdata/models.py

## M2-T2 · Simulador (órdenes, fills, ledger)
Agente `sim-risk-engineer` · rama `task/m2-t2` · 23 acciones · última actividad 00:18 · commits 1 · archivos con cambios 3

**Commits:**
- `1758a26 wip(m2-t2): checkpoint before usage-limit pause (unreviewed, untested)`

**Archivos tocados (sin commit):**
- `M src/tsclaude/sim/costs.py`
- `?? src/tsclaude/sim/fills.py`
- `?? src/tsclaude/sim/ledger.py`

**Últimas acciones:**
- `00:16` **Bash** — Add limit-price cap to CostModel.price
- `00:14` **Bash** — Rename tick_rounding_cost to rounding_adjustment
- `00:10` **Write** — src/tsclaude/sim/ledger.py
- `00:08` **Bash** — Rewrite resolve_exit with conservative same-bar SL rule
- `00:06` **Write** — src/tsclaude/sim/fills.py
- `00:00` **Bash** — Check WIP checkpoint commit and worktree state
- `20:03` **Write** — src/tsclaude/sim/orders.py
- `19:56` **Write** — src/tsclaude/sim/costs.py
- `19:51` **Write** — src/tsclaude/sim/clock.py
- `19:45` **Write** — src/tsclaude/sim/money.py

## M2-T3 · Estadística: win rate certificado
Agente `quant-engineer` · rama `task/m2-t3` · 71 acciones · última actividad 00:18 · commits 1 · archivos con cambios 10

**Commits:**
- `f24daa1 wip(m2-t3): checkpoint before usage-limit pause (unreviewed, untested)`

**Archivos tocados (sin commit):**
- `M src/tsclaude/research/stats/binomial.py`
- `M src/tsclaude/research/stats/normal.py`
- `?? src/tsclaude/research/stats/__init__.py`
- `?? src/tsclaude/research/stats/bootstrap.py`
- `?? src/tsclaude/research/stats/breakeven.py`
- `?? src/tsclaude/research/stats/certify.py`
- `?? src/tsclaude/research/stats/multiple_testing.py`
- `?? src/tsclaude/research/stats/power.py`
- `?? src/tsclaude/research/stats/sharpe.py`
- `?? tests/unit/research/`

**Últimas acciones:**
- `00:18` **Bash** — Run new research unit tests
- `00:18` **Write** — tests/unit/research/test_research_power.py
- `00:18` **Write** — tests/unit/research/test_research_normal_breakeven_mt.py
- `00:17` **Edit** — src/tsclaude/research/stats/normal.py
- `00:17` **Edit** — src/tsclaude/research/stats/normal.py
- `00:17` **Bash** — Probe norm_ppf in extreme tail
- `00:17` **Bash** — Re-run binomial tests
- `00:17` **Edit** — tests/unit/research/test_research_binomial.py
- `00:17` **Read** — tests/unit/research/test_research_binomial.py
- `00:16` **Bash** — Re-run binomial tests with durations

