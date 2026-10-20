# Feed de agentes — Trading System Claude

Actualizado: **2026-10-20 00:25** (hora de Costa Rica). Se refresca cada ~3 minutos mientras los agentes trabajan. Solo muestra acciones (herramienta + descripción), nunca contenido de archivos ni secretos. El historial de commits de esta rama es el registro.

## M0-T4 · API, dashboard y lanzador Windows
Agente `platform-ui-engineer` · rama `task/m0-t4` · 130 acciones · última actividad 00:24 · commits 4 · archivos con cambios 0

**Commits:**
- `a0b878d feat(scripts): Windows launcher (cmd + PowerShell) and setup guide`
- `e2fe988 feat(web): dashboard shell with status bar, workspace rail and empty states`
- `07df705 fix(api): withhold projections on a failed chain, harden startup and CSP`
- `3416ba7 wip(m0-t4): checkpoint before usage-limit pause (unreviewed, untested)`

**Últimas acciones:**
- `00:24` **Bash** — Verify line endings, no bundle committed, branch status
- `00:24` **Bash** — Commit API fixes, frontend and launcher
- `00:23` **Bash** — Lock before logging, corepack prompt, docs; retest
- `00:22` **Bash** — Show working tree changes since the WIP commit
- `00:22` **Write** — frontend/README.md
- `00:20` **Bash** — Rerun tests and mypy after fixes
- `00:20` **Bash** — Adjust Windows bind semantics and catch sqlite errors
- `00:19` **Read** — src/tsclaude/api/server.py
- `00:18` **Bash** — Withhold projections on failed chain; rerun tests
- `00:18` **Bash** — Locate coverage and previous-session builders

## M1-T1 · Ingesta en vivo de Binance
Agente `data-engineer` · rama `task/m1-t1` · 79 acciones · última actividad 00:24 · commits 1 · archivos con cambios 14

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
- `00:24` **Bash** — Add jittered delay before WS reconnect
- `00:24` **Bash** — Correct backfill test expectations and run all unit tests
- `00:23` **Bash** — Run all market-data unit tests
- `00:23` **Write** — tests/unit/marketdata/test_md_config.py
- `00:23` **Write** — tests/unit/marketdata/test_md_backfill.py
- `00:23` **Write** — tests/unit/marketdata/test_md_bars.py
- `00:22` **Write** — tests/unit/marketdata/test_md_limits.py
- `00:22` **Write** — tests/unit/marketdata/test_md_freshness_clock.py
- `00:21` **Write** — tests/unit/marketdata/test_md_gaps.py
- `00:21` **Write** — tests/unit/marketdata/test_md_livebars.py

## M2-T2 · Simulador (órdenes, fills, ledger)
Agente `sim-risk-engineer` · rama `task/m2-t2` · 33 acciones · última actividad 00:24 · commits 1 · archivos con cambios 8

**Commits:**
- `1758a26 wip(m2-t2): checkpoint before usage-limit pause (unreviewed, untested)`

**Archivos tocados (sin commit):**
- `M src/tsclaude/sim/clock.py`
- `M src/tsclaude/sim/costs.py`
- `M src/tsclaude/sim/orders.py`
- `?? src/tsclaude/sim/__init__.py`
- `?? src/tsclaude/sim/book.py`
- `?? src/tsclaude/sim/engine.py`
- `?? src/tsclaude/sim/fills.py`
- `?? src/tsclaude/sim/ledger.py`

**Últimas acciones:**
- `00:24` **Bash** — Make sizing use previous-minute equity and cash
- `00:24` **Read** — src/tsclaude/sim/book.py
- `00:23` **Bash** — Wrap long signature and recheck
- `00:23` **Bash** — Add package init and SOURCE_ERROR; lint and type-check
- `00:23` **Write** — src/tsclaude/sim/engine.py
- `00:22` **Bash** — Add ExitRequest type and locate submit
- `00:21` **Bash** — Fix last lint errors and re-run checks
- `00:21` **Bash** — Fix lint issues in sim modules
- `00:20` **Bash** — Refine book module, then format, lint and type-check
- `00:20` **Write** — src/tsclaude/sim/book.py

## M2-T3 · Estadística: win rate certificado
Agente `quant-engineer` · rama `task/m2-t3` · 86 acciones · última actividad 00:24 · commits 1 · archivos con cambios 10

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
- `00:24` **Bash** — Measure bootstrap throughput
- `00:24` **Read** — src/tsclaude/research/stats/certify.py
- `00:22` **Bash** — Run certify tests
- `00:22` **Write** — tests/unit/research/test_research_certify.py
- `00:22` **Bash** — Run bootstrap and Sharpe tests
- `00:22` **Write** — tests/unit/research/test_research_bootstrap_sharpe.py
- `00:21` **Edit** — tests/unit/research/test_research_normal_breakeven_mt.py
- `00:21` **Bash** — Re-run research unit tests
- `00:21` **Edit** — tests/unit/research/test_research_normal_breakeven_mt.py
- `00:20` **Bash** — Re-run research unit tests

