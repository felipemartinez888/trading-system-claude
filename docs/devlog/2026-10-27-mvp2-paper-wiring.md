# Devlog — 2026-10-27 (local time UTC-6) — MVP-2: paper backend wiring

- **Branch:** `feat/mvp-2-paper-wiring`, cut from `integrate/mvp-1` @ `7be73eb`.
- **Approvals:** M2-A (brief), M2-B (implement, uncommitted) and M2-D (`configs/paper.yaml`, off by default).
  M2-C (local commit) is **not** approved, so nothing is committed or pushed and `main` is unchanged.
- **Status:** this is MVP readiness work only. It claims no M3 acceptance, and the restore-property failure
  stays open.
- **Hardware:** results are provisional while the WHEA processor errors are under investigation. Only
  focused tests were run.

## What it does
- **Paper OFF by default** (`configs/paper.yaml`, `enabled: false`). A missing file means off. An invalid
  file never enables paper: it shows `UNAVAILABLE` with the reason.
- **Supervisor wiring** (`runtime/supervisor.py`):
  - When enabled, paper gets its **own** market-data subscription at every monitoring start. The demo
    detector never feeds the paper books.
  - It is stopped before market data stops.
  - A paper failure never stops monitoring.
- **Lifecycle** (`runtime/paper_runtime.py`):
  1. Wait for the market-data startup backfill (`wait_backfill`, bounded by `backfill_wait_s` = 900 s).
  2. Open or restore the active session from the hash-chained journal (`PaperTradingService.open`).
  3. Seed the risk tracker's lookback from the live bar store. Those bars are observed only, never
     processed by the books.
  4. Consume updates: `on_update` → `on_snapshot` → `poll(wall clock)`.
  5. Make at most one validated, all-or-nothing downtime backfill attempt per start from the live bar store
     (instrument, 1m timeframe, aligned and closed timestamps, range, uniqueness, ordering, and
     completeness via `missing_before`). Missing or ambiguous data stays `AWAITING_BACKFILL`.
- **States and operator visibility.** Each state is shown verbatim in `GET /api/paper/status` and as
  `paper` in `/api/status` and its push. Every failure stops paper decisions immediately and is reported as
  a runtime problem.
  | State | Meaning |
  |---|---|
  | `DISABLED` | paper is off |
  | `IDLE` | monitoring is stopped |
  | `STARTING` | waiting for the market-data backfill |
  | `RUNNING` | paper is deciding |
  | `AWAITING_BACKFILL` | downtime after the journal is unresolved |
  | `FAULTED` | a step or journal write failed |
  | `UNAVAILABLE` | the session cannot be opened or restored, or the backfill wait timed out |
  | `STOPPED_OVERFLOW` | any update dropped by the paper subscription |
  | `SUBSCRIPTION_LOST` | market data stopped under it, or no update for `silence_s` = 180 s |
  | `CONSUMER_DIED` | the consumer raised an exception |
- **Read endpoints:**
  - `GET /api/paper/status` is labelled `SYSTEM`.
  - `GET /api/paper/books` (equity, cash, unrealized P&L, positions, halted, ledger reconciliation, ledger
    hashes) and `GET /api/paper/fills` are `SIMULATED` while paper is RUNNING, otherwise `STALE` (last
    known values with their publication time) or `UNAVAILABLE`.
  - API threads read only an immutable snapshot published on the event loop.
- **Operator action** `POST /api/paper/session/new`:
  - requires the per-launch token, a note of 1–500 characters and a `request_id`;
  - is idempotent (the same id returns the original record; the same id with another note is refused);
  - is refused while paper is STARTING or RUNNING, and when paper is disabled;
  - picks a new `paper-<UTC stamp>` id that has no journal entries;
  - records the request in the append-only `data/paper_sessions.jsonl` (fsync per record); the new session
    opens at the next monitoring start, and old sessions stay read-only in the hash chain.
- **Strategy:** MVP-2 runs with **no strategy**, so only the benchmark book trades (buy-and-hold, simulated).
  WATCH signals are never converted into CONFIRMED signals or orders. Nothing here can reach an exchange
  order endpoint, sign a request or use an API key.

## Known limitations (also served in `/api/paper/status` `limitations` and in `configs/paper.yaml`)
- **Restart equivalence is not guaranteed, and not every divergence is visible.**
  - Seed 13073 (a bar after the journal's last key that never arrives) keeps paper `AWAITING_BACKFILL`
    until the operator records a new session.
  - Seed 465923499 (release-time dependence) lets the shadow book differ from an uninterrupted run while
    paper shows `RUNNING` / `SIMULATED`.
  - A journal that cannot be restored shows `UNAVAILABLE`.
  - The per-minute journal-close design (Part B) is not implemented.
- **Each monitoring start restores the active session again**, also after a failure in the previous run
  (unattended starts included). Only the operator creates a new session.
- **Instrument filters** come from `configs/backtest.yaml` and are unverified placeholders.

## Reviews (one code review, one causality and paper-safety audit; both read-only)
**Code review: changes required (2 blocking), now fixed.**
- **B1. The single backfill attempt and the tracker seeding raced the market-data service's background
  startup backfill.** After a long downtime paper would read a half-filled bar store and stay
  `AWAITING_BACKFILL` for the whole run.
  - **Fix:** paper waits for `wait_backfill` (bounded, then `UNAVAILABLE`) before it opens, seeds or
    backfills. Updates arriving meanwhile stay queued.
  - **Also:** any drop on the fresh per-run subscription now counts as overflow. Previously drops before
    the session opened were subtracted (also audit N1).
  - **Tests:** `test_paper_waits_for_the_market_data_backfill_before_opening` (it fails when the wait is
    disabled, checked), `test_market_data_backfill_that_never_finishes_leaves_paper_unavailable` and
    `test_drop_while_waiting_for_the_backfill_stops_paper`.
- **B2. The limitation text claimed every ambiguous restart stays AWAITING/FAULTED** (rule 5).
  - **Fix:** the text in the module, `LIMITATIONS` (API) and `configs/paper.yaml` now states the silent
    shadow divergence (seed 465923499), `UNAVAILABLE` on restore errors, and the restore at every start
    (audit N5).

**Causality and safety audit: no blocking findings.** Confirmed:
- no look-ahead, because risk views are always cut off at decision time;
- preloaded and backfilled bars never reach the strategy, and backfill is unobserved and ordered;
- no WATCH-to-order path with `strategy=None`;
- no exchange or order path;
- holdout and research data untouched.

**Residual non-blocking findings (not fixed in MVP-2; owner decision):**
- **Closed-subscription branch:** the "subscription closed" check never fires with the real `Subscription`
  (no `closed` attribute). Closes are caught by `market_running()` or the silence watchdog;
  `test_p6_lost_subscription…` exercises the fake-only path.
- **Watchdog wording:** the 180 s watchdog can trip during a long outage with no events at all; it shows as
  `SUBSCRIPTION_LOST` until the next monitoring start.
- **Audit log robustness:**
  - A torn or malformed line in `paper_sessions.jsonl` makes paper `UNAVAILABLE` (fail closed) and needs a
    manual fix.
  - An `OSError` while reading it during construction is not caught.
  - The file is outside the hash chain; losing it reopens the original session.
- **Provenance after a restore:** until the first live bar after a restore, paper shows `RUNNING` and books
  are labelled `SIMULATED` with the publication time, although values are last-known. `as_of_ms` should be
  the last processed close (audit N3).
- **Event-loop load:** opening runs the full chain verify and replay on the event loop, and the snapshot is
  rebuilt every poll (scans events, reconciles ledgers, re-reads the audit file). The cost grows with
  session length.
- **Smaller items:**
  - `published()` reads the snapshot and its time non-atomically.
  - `backfill_from_live_bars` accepts any truthy string.
  - `stop()` replaces `AWAITING_BACKFILL` with `IDLE`, losing the reason.
- **Test gaps:** no tests for a cancelled start with paper enabled, the stop-timeout cancel path, or a torn
  audit line. `test_p2` reads `configs/paper.yaml` relative to the working directory.

## Open MVP-demo readiness items (accepted for the local checkpoint only; NOT production-ready)
The owner recorded these at M2-C as open. They do not block the local commit, but they are not
accepted as production-ready behaviour and must be resolved or re-decided before the first demo is
called ready:
1. **Freshness label after a restore.** Restored books can hold stale, last-known values while paper
   shows `RUNNING` and labels them `SIMULATED` with the publication time, until the first live bar is
   processed. `as_of_ms` should be the last processed close, and the label `STALE` until then.
2. **Session audit log.**
   - A torn or malformed line in `paper_sessions.jsonl` makes paper `UNAVAILABLE` and has no in-app
     recovery.
   - A read error while the file is first loaded is not caught.
   - The file is outside the hash chain, so losing it silently reopens the original session.
3. **Real subscription closure is not directly validated.** The "closed" branch only works with the
   test fake. Production relies on `market_running()` and the 180 s silence watchdog, and no test
   exercises the real `Subscription`.
4. **Snapshot and state visibility.**
   - `published()` reads the snapshot and its timestamp non-atomically.
   - `stop()` replaces `AWAITING_BACKFILL` with `IDLE`, so the reason is no longer visible after
     monitoring stops.
5. **Paper restart equivalence is unresolved.** Seeds 13073 and 465923499 remain open; Part B (the
   per-minute journal close) is not implemented.

## Test results (provisional; focused only; `.venv` executables)
- New MVP-2 tests: `tests/integration/test_mvp2_paper.py` and `tests/integration/test_mvp2_paper_api.py`.
  They cover P1–P9, backfill validation, the new-session audit and the three backfill-gate tests:
  **20 passed**.
- MVP-2 plus the directly affected suites: `test_api.py`, `test_api_core.py` (the ADR-0010 contract,
  including `StatusResponse.paper`), `test_m1t2_market_api.py`, `test_m2t4_backtests_api.py`,
  `test_m1t2_verify_live.py`, `test_server_lifecycle.py`, `tests/unit/test_runtime_misc.py` and
  `tests/unit/test_sessions.py`: **152 passed, 0 failed** (15.3 s).
- **Lint and types:**
  - `ruff check --no-cache src tests scripts`: all checks passed.
  - `ruff format --no-cache --check`: 205 files already formatted.
  - `mypy` with a fresh scratch cache: no issues in 113 source files.
  - The repository caches `.ruff_cache` and `.mypy_cache` (corrupted by the power-off) were not used and
    not deleted.
- **Mutation check:** passing `ready=None` from the supervisor makes the backfill-gate test fail.
- **Not run:** the full suite, the memory stress module and imports.
