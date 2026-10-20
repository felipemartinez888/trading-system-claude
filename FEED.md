# Feed de agentes — Trading System Claude

Actualizado: **2026-10-20 01:01** (hora de Costa Rica). Se refresca cada ~3 minutos mientras los agentes trabajan. Solo muestra acciones (herramienta + descripción), nunca contenido de archivos ni secretos. El historial de commits de esta rama es el registro.

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
Agente `data-engineer` · rama `task/m1-t1` · 110 acciones · última actividad 00:39 · commits 4 · archivos con cambios 0

**Commits:**
- `b322e01 test(data): fail instead of skip when network libraries are missing in CI`
- `d71c8e8 test(data): REST MockTransport, service end-to-end, fake WS server, live smoke`
- `b901462 feat(data): bar builder, gap registry, backfill, WS manager, quality monitors`
- `dad0bb7 wip(m1-t1): checkpoint before usage-limit pause (unreviewed, untested)`

**Últimas acciones:**
- `00:38` **Bash** — Final local verification summary
- `00:36` **Bash** — Commit, push and wait for CI
- `00:36` **Bash** — Make integration imports mandatory under CI; verify both modes
- `00:36` **Bash** — Inspect log access and job ids
- `00:36` **Bash** — Look for pytest summary lines in CI logs
- `00:35` **Bash** — Wait for CI on the new commit and show results
- `00:34` **Bash** — Read CI wait helper and list CI runs for branch
- `00:34` **Bash** — Commit integration tests and fixes, push branch
- `00:34` **Bash** — Check push approval decision and pending changes
- `00:33` **Bash** — Avoid version-specific websockets API in fake server test

## M2-T2 · Simulador (órdenes, fills, ledger)
Agente `sim-risk-engineer` · rama `task/m2-t2` · 47 acciones · última actividad 00:45 · commits 2 · archivos con cambios 1

**Commits:**
- `ff33fd8 wip(m2-t2): checkpoint at usage-limit pause (unreviewed)`
- `1758a26 wip(m2-t2): checkpoint before usage-limit pause (unreviewed, untested)`

**Archivos tocados (sin commit):**
- `?? tests/unit/sim/`

**Últimas acciones:**
- `00:45` **Write** — tests/unit/sim/test_sim_golden.py
- `00:42` **Bash** — Add invariant checker; run over 40 seeds
- `00:39` **Bash** — Add unrealized_pnl view; compute G5 drawdown values
- `00:37` **Bash** — Cross-check remaining golden arithmetic
- `00:36` **Bash** — Verify hand-computed golden values with Decimal
- `00:34` **Bash** — Tally rejection reasons across random scenarios
- `00:34` **Bash** — Run random scenarios as a smoke check
- `00:34` **Bash** — Add seeded random scenario generator
- `00:32` **Bash** — Add scripted source and run helpers to testing module
- `00:30` **Bash** — Record sizing basis in position-opened events

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

