# 2026-11-09 — Start stability: bounded, resumable, safely stoppable ingestion

**Status: on `origin/feat/ingestion-bounded` (owner-approved push 2026-11-09), CI green on `1dec031` (see "CI and equivalence gate"); not merged.**
Owner approval of the plan (decisions D-A…D-F, see [DECISIONS_PENDING](../DECISIONS_PENDING.md)). Application
architecture only: nothing here concerns the operating system or hardware. Operator guide:
[RUNBOOK_INGESTION](../RUNBOOK_INGESTION.md).

## Problem (confirmed at `1acd38a`)
Every Start launched `md-backfill` over 9 days (up to 30). Its write loop (`for row in rows: store.write(...)`)
had no `await`, committed once per row (SQLite WAL, `synchronous=FULL`) on the event-loop thread, so the loop could
not serve the dashboard or Stop (cancellation lands only at an `await`). No progress, checkpoint or budget existed;
an interrupted first load turned its remainder into gaps. **Not proven:** that this explains a whole-computer freeze.

## What changed
| Commit | Content |
|---|---|
| `fe21a16` | `LiveBarStore.write_many` (one transaction per batch, row-for-row equivalent outcomes), `ingest_checkpoints` written in the same transaction, `StoreClosed`; config (`startup`, `run`, dev profile) with validators |
| `8d18c58` | `BarWriter` (one daemon thread, single flight), `WriterSink` (slow-write report then join, cancel path joins, stuck declared); `Backfiller` rewritten as chunked newest-first with cooperative stop, timeouts, budgets, error limit, checkpoint verdicts |
| `ab54d2e` | `IngestionCoordinator` (catch-up → history per policy, deterministic Stop), service/supervisor wiring, `HistoryIncomplete` fail-closed gate, `TSC_INGEST_PROFILE=dev` |
| `2b5b16a` | read-only `GET /api/ingestion` |
| `c3224ad` | read-only `tsclaude data backfill-plan` |

Production defaults: only the approved history policy changed (`catchup_max_minutes: 1440`, `history: on_demand`);
`lookback_days: 9` and `max_backfill_days: 30` are untouched.

## Stop / writer lifecycle (requirements 2–3)
Cancelling an asyncio task does not stop a thread, so Stop is explicit: finish the page (`stop_grace_s`) → cancel a pending
REST fetch → **join the writer thread** → close sockets/REST → the store may close. A write in flight is never abandoned
or overlapped (single flight; a timed-out write is reported `write_slow` and joined). A writer still running after
`write_join_timeout_s` is declared **stuck**: further writes refuse, `phase: stuck`, `safe_to_close_store: false`, and
`MarketDataService.close()` leaves the connection open instead of closing it under a transaction.

## Readiness (requirement 5)
Paper with a warm-up strategy marks the history as **required**; `wait_backfill` then raises `HistoryIncomplete` unless every
pair's history run is `complete` (partial / paused / failed / cancelled are not). Paper reports UNAVAILABLE with that reason.
The strategy adapter's own contiguous-bar readiness (`warmup_progress`, existing tests) is untouched and still blocks a
partial suffix.

## Evidence (all synthetic, no network, no real history)
- Focused local run: **235 passed** (`tests/unit/marketdata` without the 11 pre-existing pytest-asyncio failures of
  `test_subscription_nowait.py`, plus `test_ingestion_view`, runtime misc, sessions, event store, strategies, contracts).
- New test files: `test_md_writer.py` (7), `test_md_ingest_engine.py` (13), `test_md_ingest_coordinator.py` (8),
  `test_md_service_ingest_glue.py` (5), `test_md_ingest_plan.py` (2), `test_ingestion_view.py` (2); additions to
  `test_md_livebars.py` and `test_md_config.py`. The 8 existing `test_md_backfill.py` tests pass unchanged.
- ruff check / format clean on all touched files; mypy clean on the touched modules (two pre-existing errors in
  `binance/ws.py:90` and `archive/download.py:167` are unrelated).

| Plan criterion | Evidence |
|---|---|
| AC1 commits per batch, not per row | `test_md_livebars` commit counting; `test_commits_are_per_chunk_not_per_row` (3 chunks + 1 checkpoint vs 250) |
| AC2 batch ≡ sequential writes (all outcomes) | `test_md_livebars` equivalence |
| AC3/AC12 loop responsiveness | `test_engine_is_responsive_with_the_writer_thread_and_a_slow_store` (counted yields, 20 ms/transaction store) |
| AC5 cancellation, resume row-identical | `test_stop_between_chunks_leaves_whole_chunks_and_a_resumable_checkpoint` |
| AC6 crash/restart, no false gap | `test_crash_between_chunks_recovers_on_restart_without_gaps_or_duplicates` |
| AC7 partial failure / error limit | `test_a_failed_chunk_is_recorded…`, `test_consecutive_errors_stop_the_pair…` |
| AC8 timeouts and budgets | `test_a_hanging_fetch_times_out…`, `test_time_budget_pauses_the_run…` |
| AC9 checkpoint validation | `test_checkpoint_ahead_of_the_store…`, `test_foreign_or_invalid_checkpoints_are_ignored` |
| AC10 chunk-size equivalence, source-finality | `test_chunk_size_changes_nothing…`; existing backfill tests unchanged |
| AC11 Stop with slow/stuck writer | coordinator tests (slow write joined, stuck reported) + service glue tests (store left open) |
| AC13 no readiness from partial history | glue tests (`HistoryIncomplete`) + existing `test_live_adapter` partial-history cases |

## Deviations from the approved plan (disclosed)
- No `asyncio.Queue` and no `write_batch_rows` / `queue_chunks`: one REST page = one transaction = one chunk in flight, and
  the awaited write is the backpressure. Same bound (≤ 1000 rows in memory), fewer moving parts.
- Semantic change: downtime older than the catch-up is **registered as a gap** at Start and loaded by the history run (within
  `lookback_days`), instead of being fetched at Start. This is the approved on-demand policy (D-A).
- No loop-lag probe; responsiveness is shown by the counted-yield test instead of a runtime probe.
- No `POST` to trigger history: D-D asked for a read-only API. History is triggered by the paper requirement or `background`.
- D-E (M2 lake) and D-F (indicator cache) were not touched.

## Not verified here (environment)
`fastapi`, `httpx`, `websockets`, `pytest-asyncio`, `hypothesis` are not installed and there is no network, so these could
**not** run locally: `tests/integration/marketdata/test_mdi_service.py` (service end to end, one `BACKFILL_COMPLETED` per
pair per run), `test_mvp3_reference_paper.py` (config guard, 9-day end to end), the new API test in `test_m1t2_market_api.py`,
and the property tests. They need a CI run. The glue tests cover the same service logic without the network stack.

## Remaining risks
1. Unproven link between the old write loop and the whole-computer freeze; the manual validation in the runbook is the check.
2. The live bar builder still writes one closed bar per minute on the event-loop thread and may wait briefly for the store
   lock during a ≤1000-row commit.
3. Budgets and pauses are starting points, not measurements on the owner's machine.
4. A stuck writer leaves the connection open until process exit (by design); the daemon thread cannot block interpreter exit.
5. Production dashboards do not yet render `/api/ingestion` (API only).

## CI and equivalence gate (2026-11-09, after the owner-approved push)
`ci.yml` triggers on `main`, `task/**` and pull requests, so a push to `feat/**` starts nothing; CI was started with
`workflow_dispatch` on the branch (no workflow file changed).

| Run | Commit | Result |
|---|---|---|
| 42659715119 attempt 1 | `954c7c6` | Ubuntu: 1 failed, 1797 passed (`test_p1_observation_anchor::test_minutes_queued_during_the_startup_wait…`, known timing flake); Windows green |
| same, attempt 2 (rerun of the failed job) | `954c7c6` | all jobs green, Ubuntu 1798 passed |
| 42664528270 attempt 1 / 2 | `1dec031` | Windows red: `test_mvp2_paper::test_p4_incomplete_backfill…` / three clock-round tests; Ubuntu green |
| same, attempt 3 | `1dec031` | **all 7 jobs green**: Ubuntu 1809 passed, Windows 1807 passed, mypy strict, ruff, m2 cross-os, frontend, safety guards |

- **Integration tests unavailable locally now ran green**: `test_mdi_service`, `test_mvp3_reference_paper` (config guard and 9-day end to end),
  `test_m1t2_market_api` (incl. the new `/api/ingestion` test), paper/restart/replay suites.
- **Pre-existing flakes (not introduced here):** the failures above are real-time `asyncio.sleep` timing tests on a loaded runner, none of which
  runs any changed code (they use `FakeMarketData`; the supervisor hook is skipped because the fake has no `require_history`). The baseline `1acd38a`
  shows the same class: run 42332811215 attempt 1 (Windows: `test_p4_incomplete_backfill…`, `test_d21_fresh_clock_round…`,
  `test_paper_waits_for_the_market_data_backfill…`) and run 42669526845 today (Windows: `test_mvp3_reference_paper::test_bracket_reference_is_warmed_up…`),
  each green on rerun. The deterministic-hardening of these tests stays on the existing backlog (not expanded here).
- **mypy:** the extra local error was `untyped-decorator` for the new route because `fastapi` was not installed in the local sandbox; strict mypy with
  the real dependencies is green on both OS in CI. No code change.
- **Quant Equivalence Gate** (`tests/unit/marketdata/test_quant_equivalence_gate.py`, 9 tests, plus frozen copies `legacy_*_1acd38a.py` of the previous
  `backfill.py` / `livebars.py`): six scenarios (fresh store with holes; downtime after stored bars, partly observed; source-finality conflicts; invalid
  REST row; last stored bar older than `max_backfill`; idempotent second run) give identical stored rows (every column and the observed-session tag),
  counters, gap/conflict events and `BACKFILL_COMPLETED` payloads (new keys additive only); the real `BracketReference` live adapter fed from either store
  gives identical intents (9 confirmed signals), warm-up progress, history breaks and stats; an interrupted or bounded load is never ready and emits no
  intent, and the completed history matches. A mutation (dropping the "not returned by REST" gap registration) fails four gate tests.
  Quantitative layers are untouched: `git diff 1acd38a..HEAD` over `sim/ risk/ signals/ strategies/ research/ features/ backtest/ contracts/ quality/` and
  `configs/{risk,paper,universe}.yaml` is empty, so indicators, thresholds, risk decisions and paper behavior are byte-identical code on identical bars.
- **Stop with an active transaction** (`test_md_ingest_coordinator.py`): Stop waits (>= 0.25 s) for an open BEGIN IMMEDIATE transaction to commit before the
  writer is joined and the connection closes; rows and checkpoint land together; a transaction that fails before COMMIT rolls back whole and the next run
  recovers.
