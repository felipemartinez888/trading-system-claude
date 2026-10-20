# Feed de agentes — Trading System Claude

Actualizado: **2026-10-20 00:21** (hora de Costa Rica). Se refresca cada ~3 minutos mientras los agentes trabajan. Solo muestra acciones (herramienta + descripción), nunca contenido de archivos ni secretos. El historial de commits de esta rama es el registro.

## M0-T4 · API, dashboard y lanzador Windows
Agente `platform-ui-engineer` · rama `task/m0-t4` · 125 acciones · última actividad 00:20 · commits 1 · archivos con cambios 22

**Commits:**
- `3416ba7 wip(m0-t4): checkpoint before usage-limit pause (unreviewed, untested)`

**Archivos tocados (sin commit):**
- `M frontend/src/api/hooks.ts`
- `M frontend/src/components/StatusBar.tsx`
- `M src/tsclaude/api/app.py`
- `M src/tsclaude/api/context.py`
- `M src/tsclaude/api/process.py`
- `M src/tsclaude/api/server.py`
- `M tests/integration/test_api.py`
- `M tests/integration/test_api_core.py`
- `?? frontend/src/App.test.tsx`
- `?? frontend/src/App.tsx`
- `?? frontend/src/api/client.test.ts`
- `?? frontend/src/components/EmptyState.tsx`
- … y 10 más

**Últimas acciones:**
- `00:20` **Bash** — Rerun tests and mypy after fixes
- `00:20` **Bash** — Adjust Windows bind semantics and catch sqlite errors
- `00:19` **Read** — src/tsclaude/api/server.py
- `00:18` **Bash** — Withhold projections on failed chain; rerun tests
- `00:18` **Bash** — Locate coverage and previous-session builders
- `00:18` **Bash** — Confirm preview servers stopped
- `00:17` **Bash** — Stop preview servers and locate derived read models
- `00:17` **Read** — /tmp/claude-0/-home-claude/58cb29c4-0183-51d6-95f4-9bf3173191f4/scratchpad/shots/06-fallb…
- `00:17` **Read** — /tmp/claude-0/-home-claude/58cb29c4-0183-51d6-95f4-9bf3173191f4/scratchpad/shots/05-chain…
- `00:17` **Bash** — Take chain-failure and fallback screenshots

## M1-T1 · Ingesta en vivo de Binance
Agente `data-engineer` · rama `task/m1-t1` · 71 acciones · última actividad 00:21 · commits 1 · archivos con cambios 14

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
- … y 2 más

**Últimas acciones:**
- `00:21` **Write** — tests/unit/marketdata/test_md_gaps.py
- `00:21` **Write** — tests/unit/marketdata/test_md_livebars.py
- `00:21` **Bash** — Fix assertions and run parser tests
- `00:21` **Bash** — Split helpers from conftest and run parser tests
- `00:20` **Write** — tests/unit/marketdata/test_md_parse.py
- `00:20` **Write** — tests/unit/marketdata/conftest.py
- `00:20` **Bash** — Write Binance-format JSON fixtures and validate them
- `00:18` **Bash** — Fix WS connection disposal and bounded stop
- `00:16` **Bash** — Show mypy errors only in our sources
- `00:16` **Bash** — Fix typing issues and re-run mypy with httpx visible

## M2-T2 · Simulador (órdenes, fills, ledger)
Agente `sim-risk-engineer` · rama `task/m2-t2` · 27 acciones · última actividad 00:21 · commits 1 · archivos con cambios 5

**Commits:**
- `1758a26 wip(m2-t2): checkpoint before usage-limit pause (unreviewed, untested)`

**Archivos tocados (sin commit):**
- `M src/tsclaude/sim/clock.py`
- `M src/tsclaude/sim/costs.py`
- `?? src/tsclaude/sim/book.py`
- `?? src/tsclaude/sim/fills.py`
- `?? src/tsclaude/sim/ledger.py`

**Últimas acciones:**
- `00:21` **Bash** — Fix last lint errors and re-run checks
- `00:21` **Bash** — Fix lint issues in sim modules
- `00:20` **Bash** — Refine book module, then format, lint and type-check
- `00:20` **Write** — src/tsclaude/sim/book.py
- `00:16` **Bash** — Add limit-price cap to CostModel.price
- `00:14` **Bash** — Rename tick_rounding_cost to rounding_adjustment
- `00:10` **Write** — src/tsclaude/sim/ledger.py
- `00:08` **Bash** — Rewrite resolve_exit with conservative same-bar SL rule
- `00:06` **Write** — src/tsclaude/sim/fills.py
- `00:00` **Bash** — Check WIP checkpoint commit and worktree state

## M2-T3 · Estadística: win rate certificado
Agente `quant-engineer` · rama `task/m2-t3` · 80 acciones · última actividad 00:21 · commits 1 · archivos con cambios 10

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
- `00:21` **Edit** — tests/unit/research/test_research_normal_breakeven_mt.py
- `00:21` **Bash** — Re-run research unit tests
- `00:21` **Edit** — tests/unit/research/test_research_normal_breakeven_mt.py
- `00:20` **Bash** — Re-run research unit tests
- `00:20` **Edit** — tests/unit/research/test_research_normal_breakeven_mt.py
- `00:20` **Edit** — tests/unit/research/test_research_normal_breakeven_mt.py
- `00:20` **Edit** — tests/unit/research/test_research_power.py
- `00:20` **Write** — tests/unit/research/stats_exact_ref.py
- `00:20` **Bash** — Show norm_cdf comparison failure
- `00:18` **Bash** — Run new research unit tests

