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
| CI `42752436724` | `28b4fe7` | **python ubuntu: 1867 passed, 0 failed** (ruff lint, ruff format, mypy strict also green); **python windows: 1864 passed, 1 failed**: the pre-existing flake `test_mvp2_paper.py::test_p4_incomplete_backfill_stays_awaiting_and_processes_nothing` (`RUNNING is AWAITING_BACKFILL`, §6); frontend ✅ ✅, M2 cross-OS ✅ ✅, safety guards ✅ | First CI run covering the new API, hints, research status and walk-forward code |
| CI `42754283876` | `ceb393c` (flake fix included) | **7/7 green on the first attempt**: python ubuntu **1873 passed**, python windows **1871 passed**, frontend ✅ ✅, M2 cross-OS ✅ ✅, safety guards ✅ | One green Windows run does not prove the intermittent flakes are gone; the latency reproduction (§6) does the heavy lifting |
| CI `42756815061` | `54af1ef` (review fixes) | **7/7 green on the first attempt**: python ubuntu **1880 passed**, python windows **1878 passed**, frontend ✅ ✅, M2 cross-OS ✅ ✅, safety guards ✅ | Second consecutive fully green run |
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
- **No bar was observed.**
  - Persisted: `clock_skew_changed` offset −3,409 ms (threshold 1,000 ms) and `sim_observation_changed observed: false`; every `ws_live` bar was processed unobserved.
  - **Inferred** from the code (`observation_bound`) and the log's timing, not persisted: the anchor refusal code `clock_offset_skew`, and the fresh-round retries roughly 17, 33, 62 and 122 s apart (server-time requests in the log).
  - The reference strategy is built at the first observed minute, so it **never started**. This is fail-closed as designed.
- **The session ended abruptly** about 19:07:04 UTC.
  - Last writes: the heartbeat at 19:07:02.99 and the 19:06 paper steps (bars ingested at 19:07:03.5–03.9).
  - There is no `monitoring_stopped`, and the WAL was not checkpointed.
  - Owner: **"pc restarted"**. The app was in light steady state at that moment.
  - No `verify-replay` is possible for this session (no session-end evidence).

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
| 5 | Evaluate a reference strategy | ✅ shown (real) | 25-min session 2026-11-09: warmed up at the first observed minute (748/741), evaluated 144 live observed bars, 0 proposals; recomputed offline from the stored bars (§7). Deterministic: Target A tests |
| 6 | Generate or explicitly reject signals | ⚠️ partial | Deterministic Target A: 3 CONFIRMED signals, 3/3 rejected by `rr_net` < 1.5. Real sessions: no signal occurred (compression + breakout + volume is rare by design) |
| 7 | Enforce risk rules | ✅ tests | Risk fault-injection tests green in CI (`42322235295`); literal M3 gate not accepted |
| 8 | Simulate paper orders safely | ⚠️ partial | Real: benchmark book 3 simulated entries in each of 3 sessions (2026-11-06, 2026-11-09 ×2). Strategy books never traded in a real session. No order endpoint exists (paper only) |
| 9 | Inspect trades, positions, PnL | ✅ shown (benchmark) | Paper page (books, fills, activity) |
| 10 | Restart and recover | ✅ shown (real, Windows) | Recovery of the PC-restart session: downtime gap detected and backfilled, paper restored and resumed, `verify-replay` PASS on 3,498 batches (§7 run 3). Attempt 1 died natively (0xC0000005, non-deterministic, §7) |
| 11 | Reproducible backtest / replay | ⚠️ partial | **Replay, real:** PASS ×3: 1 h on 2026-11-06; 25 min on 2026-11-09, replayed on Linux; recovery, 3,498 batches. **Backtest:** reproducibility test green in CI (synthetic lake). **Pending:** a real-data backtest run twice (owner step 4) |
| 12 | Review test evidence and limitations | ✅ | This report, [MVP_LIMITATIONS](MVP_LIMITATIONS.md) |
| 13 | Stop safely | ✅ shown | Stage B: two clean Stops plus quit. 2026-11-09: console-window close recorded as a clean stop with session-end evidence. Recovery run: clean `app_shutdown` |

## 5. Definition of done (directive §11)
| Item | State |
|---|---|
| Mandatory core functions implemented | ✅ except the formal M4/M5/M6 scope (Target C) |
| Integration scenarios verified | ⚠️ 6, 8 and 11 partial (§4); 1–5, 7, 9, 10, 12, 13 shown |
| Applicable safety gates | ⚠️ M3 literal gates open; paper-only isolation holds (no order code, no keys; safety-guard CI job green) |
| Relevant regression tests executed successfully | ✅ CI `42754283876` on `ceb393c`: 7/7 green (ubuntu 1873, windows 1871 passed). CI `42756815061` on `54af1ef` (review fixes): 7/7 green (ubuntu 1880, windows 1878 passed) |
| Paper isolated from real trading | ✅ by construction (no signed endpoints, no keys; CLAUDE.md rule 2; safety guards in CI) |
| Dashboard reflects system state | ✅ for the parts shown. New: research status and actionable blockers |
| Recovery tested | ✅ tests and **real Windows crash recovery** (§7 run 3). One non-deterministic native crash in an earlier attempt is recorded |
| Reproducible backtest / replay evidence | ✅ replay: 3 real sessions, one replayed across OS. ⚠️ backtest: synthetic reproducibility in CI only; real-data double run pending |
| Startup/shutdown procedure | ✅ [MVP_USER_GUIDE](MVP_USER_GUIDE.md) |
| Limitations transparent | ✅ [MVP_LIMITATIONS](MVP_LIMITATIONS.md) |
| Independent final review, no critical blocker | ✅ run (Fable, fresh context, read-only, on `ceb393c`): **APPROVE WITH NOTES** for the code. Its one blocking item, recording the native recovery crash before declaring a status, is done (§7). Its non-blocking findings are fixed (§8) |

## 6. Open item: paper-runtime CI flakes
- **Failures:**
  - the anchor is None or exactly one minute late (`test_p1_observation_anchor`, `test_d21_fresh_clock_round`, `test_mvp3_reference_paper`);
  - or the state is read before the first poll (`test_mvp2_paper::test_p4_incomplete_backfill...`).
- **Working hypothesis:** the tests wait fixed real-time intervals (`asyncio.sleep(POLL * k)`) against a manual clock. Plain CPU load (6 parallel runs, 2 cores) did not reproduce it.
- **Root cause proven** by a Fable subagent in a separate worktree (branch `fix/paper-runtime-test-determinism`) and re-verified independently on this branch:
  - The tests slept a fixed real time and then asserted, or moved the manual clock, before the consumer's poll had run. When the event loop stalled, the anchor came out later or not at all.
  - An opt-in latency fixture (`TSC_TEST_LATENCY_MS`, off by default) reproduces it on the 4 affected files that run locally (73 tests).

  | Tests | No latency | 40 ms per event-store commit | 80 ms slower clock round |
  |---|---|---|---|
  | Old (`1160a71`) | — | **13 failed / 60 passed**, the same assertions as CI | — |
  | Fixed (`ceb393c`) | **73 passed** | **73 passed** | **73 passed** |

- **No production defect found.**
  - The anchor is fixed with no await between the snapshot, `now` and `observe_from`, so a late poll can only give a later anchor.
  - The observed flag depends only on `open_time >= anchor`.
  - Noted for the Lead: `RUNNING` is published between `_open()` and the first poll, even when that poll then turns `AWAITING_BACKFILL`. Nothing is processed in that window.
- **Fix:** tests only (`a3ba353`, `ceb393c`). The tests now wait for what the consumer did (a completed quiet poll with the anchor gate decided at the current manual time), bounded at 60 s; no assertion was weakened.
- **Residual:** `test_retries_back_off_and_are_capped` still asserts real-time gaps (≤ 0.31 s). It was left as it is: loosening it is a Lead decision.

## 7. Owner evidence runs, 2026-11-09 evening (code `1160a71`, clock synced, `scripts/owner_checks/mvp_evidence.ps1`)

### Run 1, `mvp-evidence-20261109-2201` (22:01 local)
- **Clock pre-check:** worst offset 0.0101 s, then 0.0099 s before the paper step: PASS.
- **Crash-recovery attempt 1** (a copy of `paper-20261109-125923`, the session the PC restart ended):
  - The process died with exit code **`0xC0000005` (access violation)** about 4 s after start, after `supervisor start: creating service` (the live-bar store opening the copied database).
  - `verify-chain`: ok, 85 events. `verify-replay`: INCONCLUSIVE (no session-end evidence).
  - The same copy opens and builds the service without a crash on Linux (Python 3.13.18, SQLite 3.53.1); `PRAGMA integrity_check` returns ok.
- **Paper session `paper-20261109-2201`, 25 min** (it ended when the owner closed the console window at 22:27):
  - **Clean stop:** `monitoring_stopped` and `session_stopped` with reason `console_closed`, and the session-end evidence was written (Windows console-close path).
  - **Observation worked:** no `clock_skew_changed` event. `sim_observation_changed` went `observed: false` (04:02Z, downtime backfill) → **`observed: true` (live) from 04:03Z**. **144 of 150 paper steps observed**, 150 `ws_live` bars observed, release latency about 0.2 s.
  - **Books:** benchmark 3 simulated entries (BTC/ETH/SOL spot, 7 ledger postings including the deposit). Baseline and shadow: deposit only. **0 strategy intents.**
  - **Reference strategy, recomputed offline from the session's own stored bars with the production adapter (`warm_up` + feed):**
    - warmed up for all 6 instruments at the first observed minute (748 ≥ 741 15m bars);
    - **0 proposals** over the 144 live bars, consistent with the journal.
    - The live in-memory status itself is not persisted, so this is a recomputation.
  - **`verify-replay` on Linux** (this container, code `2c7d343`, on a copy of the Windows store): **PASS**. 150 batches, every ledger hash equal to the session-end evidence: the replay matches across OS and across this branch's changes.
- **Reference backtest:** not run, because the window was closed before step 4.

### Run 2, `mvp-evidence-20261109-2216`
- Recovery attempt 2 **refused to start**: port 8765 was held by run 1's paper session (`could not bind the API port`, app exit 3).
- Fail-closed: nothing was written, and the copy's chain is unchanged at 83 events.

### Run 3, `mvp-evidence-20261109-2227` (22:27, `PYTHONFAULTHANDLER=1`): real crash recovery **PASS**
- Recovery attempt 3, a new copy of the same crashed session (identical input to attempt 1): **no native crash**.
- **Downtime:** the app detected the 19:07 → 04:27 UTC downtime gap (cause `downtime`) on all 6 pairs, caught it up, restored the paper session and resumed it.
- **Live run:** 15 min monitored, `verify_live` PASS; `monitoring_stopped` (`app_shutdown`), then a clean exit.
- **`verify-replay`: PASS.** **3,498 batches** replayed, every ledger hash equal to the session-end evidence; 84 observed bars; benchmark 3 entries; strategy books not exercised.
- **`verify-chain`:** ok, 3,608 events.
- **Platform anomaly** (logged and survived):
  - At 04:37:40Z one WebSocket frame dispatch raised `TypeError: expected 2 arguments, got 3` inside the generated frozen-dataclass `__init__` of `StreamKey(market, symbol, kind)`, a correct three-argument call (`marketdata/binance/ws.py:112`).
  - That line runs for every book-ticker message and succeeded thousands of times around it. Correct execution cannot raise there.
  - The error was caught; the stream and the session continued.

### Reading
- **The native crash is non-deterministic:** 1 of 2 runs that got past the port check, on identical input.
- **Together** with the "impossible" `TypeError` and the PC restart of 13:07, this evidence is consistent with the open hardware (WHEA) instability. It is not proven to be that.
- **The software side held up:** it failed closed, recorded clean stops where possible, kept the hash chain intact through every event, and recovered.
- **This also changes the scenario table (§4):**
  - recovery after an unclean end is now shown on Windows;
  - real observed paper trading is shown for the benchmark book;
  - strategy books have still never traded in a real session, because the reference strategy produced no signal.
## 8. Independent final review (Fable, fresh context, read-only, commit `ceb393c`)
**Verdict: APPROVE WITH NOTES (code).**
- **Checked by the reviewer:**
  - no look-ahead, no weakened assertion, no unsafe script command;
  - the Stage B statements match the state database;
  - the Stage C counts match (6 × 11,520 rows, 48 unobserved steps, −3,409 ms, last heartbeat 19:07:02.991);
  - CI `42752436724` counts confirmed;
  - its local subset: 268 passed.
- **Blocking (for the status declaration only):** record the native crash in the recovery step. Done in §7.
- **Non-blocking findings, all fixed after the review:**
  1. **Walk-forward train scores were not comparable within a fold.** Each run was certified with the registry count at its own record time, so selection depended on grid order. Fold scores are now re-certified with one common count (`rescore`, via the runner's own recompute) before `select`. Tests: a fake proves selection follows the rescored values; a synthetic-lake test proves both train runs carry the common count.
  2. **An embargo of 0 was accepted.** It is now at least one minute, refused in `plan_folds` and at the CLI. Runs are also flat at their own window end.
  3. **The max-loss docstring was wrong.** Corrected: it is the largest bound among the walk-forward's runs, development-segment measurement, used only for the expectancy bound.
  4. **VALIDATED now requires a holdout-segment grade A** (an A on another segment does not count). Tests added.
  5. **The evidence script's step exit was only the live code.** It is now the first non-zero of live, replay and chain, so a native crash is never hidden. Faulthandler is on for child processes.
  6. **Report wording:** pending/running items updated; heartbeat timing made precise; inferred and persisted Stage C details separated.
