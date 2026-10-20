# Feed de agentes — Trading System Claude

Actualizado: **2026-10-20 00:34** (hora de Costa Rica). Se refresca cada ~3 minutos mientras los agentes trabajan. Solo muestra acciones (herramienta + descripción), nunca contenido de archivos ni secretos. El historial de commits de esta rama es el registro.

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
Agente `data-engineer` · rama `task/m1-t1` · 101 acciones · última actividad 00:33 · commits 2 · archivos con cambios 5

**Commits:**
- `b901462 feat(data): bar builder, gap registry, backfill, WS manager, quality monitors`
- `dad0bb7 wip(m1-t1): checkpoint before usage-limit pause (unreviewed, untested)`

**Archivos tocados (sin commit):**
- `M src/tsclaude/marketdata/bars.py`
- `M src/tsclaude/marketdata/binance/rest.py`
- `M src/tsclaude/marketdata/service.py`
- `M tests/unit/marketdata/test_md_bars.py`
- `?? tests/integration/`

**Últimas acciones:**
- `00:33` **Bash** — Avoid version-specific websockets API in fake server test
- `00:33` **Bash** — Run strict mypy on all src and the CI private-endpoint guard
- `00:32` **Bash** — Fix lint and run the whole tree except property tests
- `00:32` **Bash** — Show lines flagged by lint
- `00:32` **Bash** — Show collection error and remaining lint errors
- `00:32` **Bash** — Lint and run the full test tree with CI-like addopts
- `00:32` **Write** — tests/integration/marketdata/test_mdi_live_smoke.py
- `00:31` **Write** — tests/integration/marketdata/test_mdi_ws_fake_server.py
- `00:30` **Bash** — Separate service stop and close; rerun tests
- `00:30` **Bash** — Fix time-gated retry test and rerun all tests

## M2-T2 · Simulador (órdenes, fills, ledger)
Agente `sim-risk-engineer` · rama `task/m2-t2` · 39 acciones · última actividad 00:33 · commits 1 · archivos con cambios 9

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
- `00:32` **Bash** — Add scripted source and run helpers to testing module
- `00:30` **Bash** — Record sizing basis in position-opened events
- `00:29` **Bash** — Fix drawdown zero and end-of-data expiry
- `00:27` **Bash** — Fix zero formatting and add equity curve origin
- `00:26` **Bash** — Smoke-run a market long TP scenario
- `00:25` **Bash** — Add test bar factories; causal risk equity; recheck
- `00:24` **Bash** — Make sizing use previous-minute equity and cash
- `00:24` **Read** — src/tsclaude/sim/book.py
- `00:23` **Bash** — Wrap long signature and recheck
- `00:23` **Bash** — Add package init and SOURCE_ERROR; lint and type-check

## M2-T3 · Estadística: win rate certificado
Agente `quant-engineer` · rama `task/m2-t3` · 110 acciones · última actividad 00:33 · commits 5 · archivos con cambios 0

**Commits:**
- `51b3066 Merge branch 'main' into task/m2-t3`
- `34d2d9f test(research): exact-reference and property tests for the statistics core`
- `d988bc6 feat(research): exact statistics core for certified win rates (M2-T3)`
- `20b29e8 build: add numpy to runtime dependencies for the research statistics core`
- `f24daa1 wip(m2-t3): checkpoint before usage-limit pause (unreviewed, untested)`

**Últimas acciones:**
- `00:32` **Bash** — Print reference values for the final report
- `00:32` **Bash** — Commit dependency, implementation, and tests separately
- `00:32` **Bash** — Inspect metrics CSV and roadmap entries
- `00:31` **Bash** — Full local verification: lint, format, mypy, unit and property tests
- `00:31` **Bash** — Add tiny-margin test and re-run checks
- `00:31` **Edit** — src/tsclaude/research/stats/certify.py
- `00:30` **Edit** — src/tsclaude/research/stats/certify.py
- `00:30` **Bash** — Re-run lint, mypy and certify tests
- `00:30` **Edit** — src/tsclaude/research/stats/certify.py
- `00:30` **Edit** — src/tsclaude/research/stats/certify.py

