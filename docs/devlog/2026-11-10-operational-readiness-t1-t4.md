# 2026-11-10 — Operational-readiness closure, T1–T4 only

**Scope:** tasks T1–T4 of the independent operational-readiness review
(`claude/TSC_MVP_OPERATIONAL_READINESS_REVIEW.md` §7; findings R-01 to R-04). Nothing else from §7
(T5–T17) was started. No production threshold, execution rule, H1 file or research record changed.
M3 is **not** accepted by this work; D-A stays the owner's decision.

## Commits (on `task/mvp-integration`)

| Task | Commit | What changed |
|---|---|---|
| T1 (R-02) | `5f4628e` | Target A takes its decision latency from `build_paper_config(PaperSettings(), configs/)` (3,000 ms today). The test that encoded the latency-0 bound now states the rule for any latency (`earliest_execution_ms`: the first 1m open strictly after decision + latency) and runs at the production latency and at 0. A guard test fails if Target A and the production latency drift apart |
| T2 (R-01) | `3cf2ef3` | `M3_ACCEPTANCE_REQUEST.md`, `DECISIONS_PENDING.md` (D-A), `MVP_LIMITATIONS.md`: Paper reads `decision_latency_ms` from `configs/backtest.yaml`; `1cc2693` changed it 0 → 3,000 ms; the three D-A sessions carry digest `7a68bcfc…`, the current configuration is `676d69b6…`; old and current evidence are kept apart; older default-config sessions do not restore; the decision must say which configuration it covers |
| T3 (R-04) | `ed9d9d3` | `sim/paper.py`: `ConfigurationMismatch(RestoreError)` raised by `replay(verify=True)` before any batch when the configuration recorded in the first batch differs from the current one, naming each field (`decision_latency_ms: 0 -> 3000`). `operator_hints.paper_hint` gives the recovery (the session's own `TSC_CONFIG_DIR`, or a new session in a new `TSC_DATA_DIR`; the old one stays verifiable with `verify-replay`) |
| T4 (R-03) | `59c826a` | `mvp_evidence.ps1` defaults `-Repo` to its own checkout (`$PSScriptRoot\..\..`) and refuses a copy outside a checkout before writing anything; `MVP_USER_GUIDE.md` §4 runs the tracked script with `-SkipBacktest`; `RUNBOOK_INGESTION.md` no longer names the older branch |

| Review follow-ups | `8f8b2f5` | Non-blocking findings that touch T1–T4: instruments compared by `market:symbol` and long values shortened (one dropped instrument gave a 1,321-character reason); every canonical difference reported, order and JSON type included; the hint gives the in-app recovery (Stop, **Operator action → Record new paper session**) and `uv run tsclaude verify-replay … --evidence <file>`; p3 checks the published snapshot and runs that recovery; the `7a68bcfc…` digest read from the three sessions' session-end files; wording fixes |

## Evidence

- Target A: 15 passed (the 13 originals at 3,000 ms, the latency-0 case, the guard).
- Owner gate subset (readiness review §6 step 2 paths) at `59c826a`: **124 passed** (120 at `0955749`; +2 Target A, +2 restart).
- At `59c826a`: `tests/unit/sim`, `tests/unit/risk`, hints, owner scripts and the paper integration tests: 705 passed.
- Full unit suite at `59c826a`: 1966 passed, 2 skipped, 16 failed, all 16 environment-only (11 `test_subscription_nowait`, 5 `test_o4_zero_trade_capture`: no pytest-asyncio here). ruff, ruff format, mypy: clean.
- CI `42878174027` on `59c826a`: 7/7 success (python ubuntu 2363 passed, 1 skipped; windows 2361 passed, 1 skipped). The follow-up commit `8f8b2f5` and this record run in the next CI.
- After the follow-ups: sim, risk, hints, owner scripts and integration (modules that import fastapi cannot be collected here): 753 passed, 9 skipped.
- Independent review (`reviewer`, fresh context, on `git archive` copies): **CONDITIONAL PASS, 0 blocking**. Conditions: a green CI on `59c826a` (met, above) and a Windows run of the T4 script (owner). Mutants: the drift guard catches latency 0 in `paper_config()` and in `backtest.yaml`; `[production_latency]` catches an engine that ignores the latency; the T3 check disabled and `ConfigurationMismatch` outside `RestoreError` are caught; a T4 `throw` → `Write-Warning` mutant survives the static tests.
- Digests read from the owner PC (read-only staging of `data\session_end\*.json`): `config_digest` `7a68bcfcfd52ac5a…` in `paper-bracket-20261106-104529` (360 batches), `paper-20261109-2201` (150) and `recovery-20261109-2227` (3,498).

## Review findings not fixed here (non-blocking)

- The T4 tests only read the script text; PowerShell is not available on the Linux job. The owner's Windows run covers it.
- A relative `-Repo` breaks the later `Join-Path $Repo` calls after `Set-Location` (older than this change).
- Target A still uses the fixture instrument spec (tick 0.1, lot 0.001, minimum notional 0); the docs claim only latency, risk and costs for it.
- The `+1` in `earliest_execution_ms` dropped is an equivalent mutant for decisions at `:59.999`.

## Behaviour notes

- `verify-replay` is unchanged: it compares the configuration digest before it replays, so a
  different configuration is still INCONCLUSIVE ("configuration differs"), never FAIL or PASS.
- Restore with the same configuration is unchanged (the recorded and current descriptions are equal,
  so the batch comparison runs as before).
- **One tightening, fail-closed:** before T3, a session recorded under another configuration was
  refused only when a recomputed batch differed from the journal, so a difference that no journaled
  batch depended on could restore under the new configuration. Every recorded difference is now
  refused with `ConfigurationMismatch`. Recovery as in the hint.
- A first batch with no recorded configuration keeps the batch comparison only.

## Not done here

- No real paper session under the current configuration (owner, readiness review §6 steps 4–7).
- T5–T17.
