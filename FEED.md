# Feed de agentes — Trading System Claude

Actualizado: **2026-10-20 00:28** (hora de Costa Rica). Se refresca cada ~3 minutos mientras los agentes trabajan. Solo muestra acciones (herramienta + descripción), nunca contenido de archivos ni secretos. El historial de commits de esta rama es el registro.

## M0-T4 · API, dashboard y lanzador Windows
Agente `platform-ui-engineer` · rama `task/m0-t4` · 131 acciones · última actividad 00:26 · commits 5 · archivos con cambios 0

**Commits:**
- `ff4da67 Merge branch 'main' into task/m0-t4`
- `a0b878d feat(scripts): Windows launcher (cmd + PowerShell) and setup guide`
- `e2fe988 feat(web): dashboard shell with status bar, workspace rail and empty states`
- `07df705 fix(api): withhold projections on a failed chain, harden startup and CSP`
- `3416ba7 wip(m0-t4): checkpoint before usage-limit pause (unreviewed, untested)`

**Últimas acciones:**
- `00:25` **Bash** — Final local verification run
- `00:24` **Bash** — Verify line endings, no bundle committed, branch status
- `00:24` **Bash** — Commit API fixes, frontend and launcher
- `00:23` **Bash** — Lock before logging, corepack prompt, docs; retest
- `00:22` **Bash** — Show working tree changes since the WIP commit
- `00:22` **Write** — frontend/README.md
- `00:20` **Bash** — Rerun tests and mypy after fixes
- `00:20` **Bash** — Adjust Windows bind semantics and catch sqlite errors
- `00:19` **Read** — src/tsclaude/api/server.py
- `00:18` **Bash** — Withhold projections on failed chain; rerun tests

## M1-T1 · Ingesta en vivo de Binance
Agente `data-engineer` · rama `task/m1-t1` · 87 acciones · última actividad 00:27 · commits 2 · archivos con cambios 1

**Commits:**
- `b901462 feat(data): bar builder, gap registry, backfill, WS manager, quality monitors`
- `dad0bb7 wip(m1-t1): checkpoint before usage-limit pause (unreviewed, untested)`

**Archivos tocados (sin commit):**
- `?? tests/integration/`

**Últimas acciones:**
- `00:27` **Bash** — Show the first two REST test failures
- `00:27` **Bash** — Run REST integration tests with cached httpx
- `00:27` **Write** — tests/integration/marketdata/test_mdi_rest.py
- `00:26` **Write** — tests/integration/marketdata/mdi_helpers.py
- `00:26` **Bash** — Commit implementation and unit tests
- `00:25` **Bash** — Repeat the unit suite 5 times and lint
- `00:25` **Bash** — Run WS manager unit tests
- `00:25` **Write** — tests/unit/marketdata/test_md_ws_manager.py
- `00:24` **Bash** — Add jittered delay before WS reconnect
- `00:24` **Bash** — Correct backfill test expectations and run all unit tests

## M2-T2 · Simulador (órdenes, fills, ledger)
Agente `sim-risk-engineer` · rama `task/m2-t2` · 36 acciones · última actividad 00:27 · commits 1 · archivos con cambios 9

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
- `?? src/tsclaude/sim/testing.py`

**Últimas acciones:**
- `00:27` **Bash** — Fix zero formatting and add equity curve origin
- `00:26` **Bash** — Smoke-run a market long TP scenario
- `00:25` **Bash** — Add test bar factories; causal risk equity; recheck
- `00:24` **Bash** — Make sizing use previous-minute equity and cash
- `00:24` **Read** — src/tsclaude/sim/book.py
- `00:23` **Bash** — Wrap long signature and recheck
- `00:23` **Bash** — Add package init and SOURCE_ERROR; lint and type-check
- `00:23` **Write** — src/tsclaude/sim/engine.py
- `00:22` **Bash** — Add ExitRequest type and locate submit
- `00:21` **Bash** — Fix last lint errors and re-run checks

## M2-T3 · Estadística: win rate certificado
Agente `quant-engineer` · rama `task/m2-t3` · 95 acciones · última actividad 00:27 · commits 1 · archivos con cambios 12

**Commits:**
- `f24daa1 wip(m2-t3): checkpoint before usage-limit pause (unreviewed, untested)`

**Archivos tocados (sin commit):**
- `M pyproject.toml`
- `M src/tsclaude/research/stats/binomial.py`
- `M src/tsclaude/research/stats/normal.py`
- `?? src/tsclaude/research/stats/__init__.py`
- `?? src/tsclaude/research/stats/bootstrap.py`
- `?? src/tsclaude/research/stats/breakeven.py`
- `?? src/tsclaude/research/stats/certify.py`
- `?? src/tsclaude/research/stats/multiple_testing.py`
- `?? src/tsclaude/research/stats/power.py`
- `?? src/tsclaude/research/stats/sharpe.py`
- `?? tests/property/research/`
- `?? tests/unit/research/`

**Últimas acciones:**
- `00:27` **Bash** — Format, lint, type-check and run all unit tests
- `00:27` **Edit** — pyproject.toml
- `00:26` **Bash** — Run property tests under four seeds with 5x examples
- `00:26` **Bash** — Run property tests with several shim seeds
- `00:26` **Edit** — src/tsclaude/research/stats/binomial.py
- `00:26` **Bash** — Write hypothesis shim in scratchpad and run property tests locally
- `00:26` **Write** — tests/property/research/test_research_stats_properties.py
- `00:25` **Bash** — Re-run certify tests
- `00:25` **Edit** — tests/unit/research/test_research_certify.py
- `00:24` **Bash** — Measure bootstrap throughput

