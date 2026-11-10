# MVP validation report

Only evidence that was actually produced appears here: real counts, run ids, artifact paths. A
scenario that has not been validated stays open. Nothing here is evidence of profitability, M3
acceptance or formal (Target C) MVP acceptance.

**Current status: MVP FUNCTIONAL — VALIDATION INCOMPLETE** (§5 lists what is missing).

## 1. Environments
| Environment | What runs there | Limits |
|---|---|---|
| Linux container (this session): Python 3.13.18, numpy, pydantic, pytest, mypy, ruff 0.16.9 | Unit and most integration tests | Package installs are refused (network policy), and the owner's venv files are hardlinks the bridge will not copy. So pyarrow, duckdb, fastapi, httpx, websockets, hypothesis and pytest-asyncio are missing. Their tests error or skip **for that reason only**: the failing set on the base commit and on this branch is identical |
| GitHub Actions CI (Ubuntu + Windows), `.github/workflows/ci.yml` | The full suite: ruff, mypy strict, pytest, vitest, typecheck, build, M2 cross-OS gate, safety guards | The authoritative run |
| Owner PC (Windows 11, Python 3.13.2) | Live sessions, canonical lake, launcher | Hardware restarts (WHEA, open; out of scope) |

## 2. Test evidence
| Run | Commit | Result | Notes |
|---|---|---|---|
| Local baseline | `1160a71` | **1524 passed**, 53 failed + 54 errors, 8 skipped (487 s) | Every failure or error is a missing package: `pyarrow` 79 (D-17 promote), `pytest-asyncio` 11, `httpx` 5, `fastapi`/`hypothesis` 12 collection errors |
| Local branch | `f1658fe` | **1562 passed** (+38 new), the same 53 + 54 environment failures (the set is identical, checked by diff), 9 skipped | New tests: research status 15, operator hints 23 |
| Local walk-forward | `1c40da2` | `tests/unit/research/test_walkforward.py` **17 passed**; `tests/unit/backtest/test_walkforward_runner.py` **1 passed** (synthetic lake through `run_backtest`) | Includes the train-only selection canary |
| CI `42751246117` | `f1658fe` | frontend ubuntu ✅, frontend windows ✅, M2 cross-OS ubuntu ✅ / windows ✅, safety guards ✅; python jobs **cancelled** by the next push | The vitest run covers the research panel and the Paper hint |
| CI `42752436724` | `28b4fe7` | frontend ✅ ✅, M2 cross-OS ✅ ✅, safety guards ✅; **python (ubuntu, windows): pending at the time of writing** | To be recorded |
| Pre-existing CI flakes (before this session) | `954c7c6`, `1dec031`, `1acd38a` | 1–3 paper-runtime failures per run, Windows (jobs `129324514607`, `129332534067`, `129342046002`) and once Ubuntu (`129307675701`) | Being root-caused (§6) |

## 3. Owner runs read in this session (artifacts in `C:\Users\felip\tsc_runs`)

### Stage B: production profile, paper off (`prod-20261109-125221`)
- **Two Start/Stop cycles, both clean**, then `session_stopped` (user_quit).
- **First Start:** the 24 h catch-up for 6 pairs (spot and USD-M BTC/ETH/SOL), 2 REST pages each, 1,440 rows per pair, 0 errors, in about 4 s.
- **Second Start:** fetched only what was missing. One 1-minute gap (USD-M BTCUSDT) was detected and resolved as `recovered`.
- **Clock round:** offset −3,411 ms (threshold 1,000 ms): `clock_skew_changed exceeded: true`.
- `bar_late` events for the start minute (REST-recovered after the deadline), as designed.
- **Verdict:** PASS for the Stage B criteria in [RUNBOOK_INGESTION](RUNBOOK_INGESTION.md), except the `/api/ingestion` JSON, which was not captured. This is from the event store and checkpoints, not owner-reported.

### Stage C: Paper O-1 (`paper-20261109-125923`, `paper.yaml` enabled, `strategy: bracket_reference`)
- **9-day history loaded on demand:** 6 pairs × 12 pages, 11,520 rows each (69,120), 0 errors, from 19:00:00 to 19:00:29 UTC. That is well inside the 120 s per-pair and 600 s per-run budgets.
- **Paper opened:** `sim_run_started`, then 48 paper steps (6 instruments × 8 minutes, 18:59 to 19:06 UTC).
- **No bar was observed.** The clock round gave −3,409 ms, the anchor was refused (`clock_offset_skew`), and the fresh-round retries backed off (17, 33, 62, 122 s). The reference strategy is built at the first observed minute, so it **never started**. This is fail-closed as designed.
- **The session ended abruptly** about 19:07:03 UTC. The heartbeat at 19:07:02.99 is the last write; there is no `monitoring_stopped` and the WAL was not checkpointed. Owner: **"pc restarted"**. The app was in light steady state at that moment. No `verify-replay` is possible for this session (no session-end evidence).

### Clock finding
- **App measurement:** −3,409 ms against Binance (5 samples, median round trip 232 ms, so error at most ±116 ms).
- **Owner check:** `w32tm /stripchart /computer:time.windows.com` → **−3.447 s** (21:45 local), consistent with the app. The measurement is correct; there is no defect.
- **Owner pressed "Sync now":** −0.0005 to +0.0010 s (21:50 local).

## 4. Phase F scenarios (directive §6)
| # | Scenario | Status | Evidence / gap |
|---|---|---|---|
| 1 | Start the application locally | ✅ shown | Owner runs 2026-10-28, 2026-11-06, 2026-11-09 (launcher / `verify_live`) |
| 2 | Access the dashboard | ✅ shown | Owner (dev-profile PASS, "dashboard responsive"); the frontend builds green in CI |
| 3 | Connect to public market data | ✅ shown | Stage B/C: spot + USD-M streams and REST, 6 pairs |
| 4 | Observe canonical market-data updates | ✅ shown | `bars_1m` ws_live rows observed live (Stage C); Markets page |
| 5 | Evaluate a reference strategy | ⚠️ partial | Deterministic Target A tests (13 tests); real 2026-11-09 session blocked by the clock (no observed bar). **Pending:** owner evidence run step 3 |
| 6 | Generate or explicitly reject signals | ⚠️ partial | Target A: 3 CONFIRMED signals, 3/3 rejected by `rr_net` < 1.5 (deterministic). Real: none yet |
| 7 | Enforce risk rules | ✅ tests | Risk fault-injection tests green in CI (`42322235295`); literal M3 gate not accepted |
| 8 | Simulate paper orders safely | ⚠️ partial | Real 1 h session 2026-11-06: 3 simulated benchmark entries; strategy books never exercised in a real session. No order endpoint exists (paper only) |
| 9 | Inspect trades, positions, PnL | ✅ shown (benchmark) | Paper page (books, fills, activity) |
| 10 | Restart and recover | ⚠️ partial | Restart tests and runbook. Stage B second Start (gap recovered). **Pending:** crash-recovery run on a copy of the session the PC restart ended (owner evidence step 2) |
| 11 | Reproducible backtest / replay | ⚠️ partial | Replay: real 1 h `verify-replay` PASS (2026-11-06); synthetic backtest reproducibility test green. **Pending:** first real-data backtest, run twice (owner evidence step 4) |
| 12 | Review test evidence and limitations | ✅ | This report, [MVP_LIMITATIONS](MVP_LIMITATIONS.md) |
| 13 | Stop safely | ✅ shown | Stage B: two clean Stops plus quit; `safe_to_close_store: true` in the dev profile (owner) |

## 5. Definition of done (directive §11)
| Item | State |
|---|---|
| Mandatory core functions implemented | ✅ except the formal M4/M5/M6 scope (Target C) |
| Integration scenarios verified | ⚠️ 5, 6, 8, 10, 11 partial (§4) |
| Applicable safety gates | ⚠️ M3 literal gates open; paper-only isolation holds (no order code, no keys; safety-guard CI job green) |
| Relevant regression tests executed successfully | ⚠️ local and frontend CI green; **Python CI pending**; pre-existing flakes being fixed |
| Paper isolated from real trading | ✅ by construction (no signed endpoints, no keys; CLAUDE.md rule 2; safety guards in CI) |
| Dashboard reflects system state | ✅ for the parts shown. New: research status and actionable blockers |
| Recovery tested | ✅ tests; ⚠️ real crash recovery pending |
| Reproducible backtest / replay evidence | ⚠️ replay real ✅; backtest real pending |
| Startup/shutdown procedure | ✅ [MVP_USER_GUIDE](MVP_USER_GUIDE.md) |
| Limitations transparent | ✅ [MVP_LIMITATIONS](MVP_LIMITATIONS.md) |
| Independent final review, no critical blocker | ⏳ not yet run |

## 6. Open item: paper-runtime CI flakes
- **Failures:**
  - the anchor is None or exactly one minute late (`test_p1_observation_anchor`, `test_d21_fresh_clock_round`, `test_mvp3_reference_paper`);
  - or the state is read before the first poll (`test_mvp2_paper::test_p4_incomplete_backfill...`).
- **Working hypothesis:** the tests wait fixed real-time intervals (`asyncio.sleep(POLL * k)`) against a manual clock. Plain CPU load (6 parallel runs, 2 cores) did not reproduce it.
- **In progress:** a Fable subagent in a separate worktree, branch `fix/paper-runtime-test-determinism`, is proving the cause with injected latency, ruling a production race in or out, and making the tests deterministic without weakening assertions. Its result will be reviewed before any merge.
