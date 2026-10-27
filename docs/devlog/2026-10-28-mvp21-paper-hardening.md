# Devlog — 2026-10-28 (local time UTC-6) — MVP-2.1: paper runtime hardening

- **Branch:** `feat/mvp-2-1-paper-hardening`, cut from `6628a2e`.
- **Approvals:** M21-A (brief) and M21-B (implementation, conditional). M21-C (commit) is **not** approved, so
  nothing is committed or pushed and `main` is unchanged.
- **Status:** MVP readiness only. It claims no M3 acceptance, and restart equivalence (seeds 13073 and
  465923499, Part B) stays open.
- **Hardware:** focused tests only; results are provisional (WHEA under investigation).

## What changed (`src/tsclaude/runtime/paper_runtime.py`, `src/tsclaude/api/paper.py`)
- **H1, freshness.** Books and fills are labelled `SIMULATED` only when all of these hold:
  - paper is RUNNING;
  - the live clock released a bar in this run (`live_bar_since_start`);
  - the last processed bar closed no more than `settle_ms + 120 s` ago.
  
  Otherwise they are `STALE`, with `as_of_ms` = the close of the last processed bar (never the publication
  time). With no processed bar at all they are `UNAVAILABLE`. Restored books therefore stay STALE until the
  first live bar after a start.
- **H2, atomic publication.** The snapshot and its time are one tuple, replaced in a single assignment.
- **H3, state kept after a stop.** `previous_run` = {state, reason, awaiting_missing, stopped_at_ms}.
  - AWAITING_BACKFILL becomes IDLE, with the reason kept.
  - Failure states (FAULTED, UNAVAILABLE, STOPPED_OVERFLOW, SUBSCRIPTION_LOST, CONSUMER_DIED) are never erased
    by a stop.
  - A second stop does not overwrite `previous_run`.
- **H4, session audit log `paper_sessions.jsonl`.** The log is append-only, bytes are never rewritten or
  removed, and it fails closed. The active session must be proven:
  - **Parses cleanly:** the active session is the last session record. A complete final record that only
    lacks its newline (Case A) is repaired: a newline plus a `{"kind":"repair"}` record are appended. A
    whitespace-only tail is repaired the same way.
  - **Unparseable final fragment (Case B):** paper stays **`UNAVAILABLE`**. Nothing is inferred, because the
    fragment may be a session switch that was used and later damaged. Recovery needs the explicit operator
    quarantine below.
  - **Invalid complete line:** accepted only if the very next line is a matching operator quarantine record.
    Otherwise `UNAVAILABLE`.
  - **Journal-ownership proof, required before any use or repair:**
    - the newest `paper_step` in the event store belongs to a session the log knows;
    - no superseded session has a `paper_step` after the switch that superseded it. A session that is active
      again later is only constrained up to its last switch-away.
    - New records carry `store_seq` (the event-store head at the switch) and are checked **by journal
      sequence**. MVP-2 records without it fall back to the weaker comparison of bar-close time and wall time.
  - **Read errors:** `OSError`, invalid UTF-8, absurd numbers or nesting never crash the app. Paper shows
    `UNAVAILABLE` and the POST returns 423.
  - **New-session requests:**
    - are refused (409) while the paper consumer is alive, which includes AWAITING_BACKFILL, because the old
      session can still resume;
    - never write a repair record when they are refused;
    - remain idempotent by `request_id`.
- **H5, subscription loss.** It is detected deterministically.
  - `MarketDataService.stop()` clears `running` before it closes subscriptions
    (`marketdata/service.py:361-370`). So `get() → None` together with `market_running() == False` means
    `SUBSCRIPTION_LOST` within one poll.
  - The fake-only `closed` attribute check was removed.
  - A close by anything else while the service keeps running has no production caller. It is caught by the
    180 s watchdog.
  - Tests use the **real** `marketdata.service.Subscription` class with an owner stub. `marketdata/` is
    unchanged.

### Manual quarantine (operator recovery for an invalid or incomplete audit line)
NORMAL terminal, **no administrator rights**. Run it only after inspecting the fragment and deciding which
session must be active. `<n>` is the 1-based number of the bad line.
```powershell
$log = 'data\paper_sessions.jsonl'
$lines = [IO.File]::ReadAllBytes($log)
# 1. compute the SHA-256 of line <n>'s exact bytes (without the line feed, including any CR)
$text = [Text.Encoding]::UTF8.GetString($lines) -split "`n"
$bad = [Text.Encoding]::UTF8.GetBytes($text[<n> - 1])
$sha = -join ([Security.Cryptography.SHA256]::Create().ComputeHash($bad) | % { $_.ToString('x2') })
# 2. append (never edit) a quarantine record naming that line and the session you choose
$now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$q = @{ v=1; kind='quarantine'; line=<n>; sha256=$sha; active_session_id='<session id>'; ts_ms=$now; note='<why>' } | ConvertTo-Json -Compress
[IO.File]::AppendAllText($log, "`n" + $q + "`n")
```
- **Where the quarantine must go:** directly after the bad line.
- **What this does not cover:** a corrupt line in the **middle** of the log (with valid records after it)
  cannot be quarantined in place without editing the file, which the append-only rule forbids. That case stays
  `UNAVAILABLE` and needs a separate owner decision (residual R1).

## Reviews (one code review, one causality and paper-safety audit)
**Code review: changes required, now fixed.**
- **B1:** a quarantine that kept the same session made the next restart after any processed bar
  `UNAVAILABLE` for good.
  - **Fix:** ownership is checked per session up to its last switch-away; quarantine records require `ts_ms`.
  - **Tests:** the recovered session processes a bar and restarts RUNNING; a quarantine without `ts_ms` is
    refused.
- **Also fixed (non-blocking):**
  - **N1:** `ValueError`/`RecursionError` in a log line crashed app startup.
  - **N3:** a whitespace tail blocked new sessions for good.
  - **N5:** a second stop erased `previous_run`.
  - **N7:** with no processed bar the label is now UNAVAILABLE, not STALE with a wall-time stamp.

**Audit: 2 blocking, now fixed.**
- **B1:** a new session could be recorded while the old one was AWAITING_BACKFILL and could still resume.
  - **Fix:** refused while the consumer is alive.
  - **Test:** `test_new_session_is_refused_while_the_consumer_is_alive_awaiting_backfill`.
  - MVP-2's `test_p5` now stops monitoring before recording the new session.
- **B2:** the same quarantine defect as review B1, fixed above.
- **Also fixed (non-blocking):**
  - **N3:** ownership is now checked by journal sequence for new records
    (`test_ownership_uses_the_store_sequence_recorded_with_the_switch`).
  - **N4:** a refused request never writes the log.

**Incident during development (no data affected):**
- **What happened:** two of my patch scripts passed an invalid `newline` argument to `open()`. Python truncated
  the target file before raising the error.
- **Files hit:** `paper_runtime.py` once and `test_mvp21_paper_hardening.py` once. The audit observed the
  0-byte runtime file; it was not a hardware fault.
- **Recovery:**
  - the runtime was rebuilt from the committed `6628a2e` plus the saved patch scripts;
  - the test file was restored from a backup taken before the run;
  - all tests passed again afterwards.
- **Prevention:** patch scripts now take a backup first and refuse to write implausibly small content.
- **Not affected:** `data/`, `evidence/`, other branches or commits.

## Test results (provisional; focused only; `.venv` executables)
- `tests/integration/test_mvp21_paper_hardening.py` (H1–H5 plus review and audit regressions) and
  `tests/integration/test_mvp2_paper.py`: **36 passed**.
- With `test_mvp2_paper_api.py`, `test_api_core.py`, `test_api.py`, `test_m1t2_market_api.py`,
  `test_server_lifecycle.py` and `test_m1t2_verify_live.py`: **154 passed, 0 failed** (17.8 s).
- **`test_p7_status_and_values_are_labelled`** (owner-approved update of `test_mvp2_paper_api.py`) now asserts
  the H1 semantics through the real API and runtime:
  - `UNAVAILABLE` while no bar is processed (RUNNING included);
  - `SIMULATED` after a ws_live minute is pushed through the running app's event loop and processed, stamped
    with that bar's close;
  - `STALE` after monitoring stops, still stamped with the bar close and asserted different from the
    publication time.
- **Lint and types:** `ruff check --no-cache` clean; `ruff format --no-cache --check` 206 files formatted;
  `mypy` with a fresh scratch cache shows no issues in 113 files.

## Residual risks (not fixed in MVP-2.1)
- **R1:** a corrupt line in the middle of the audit log cannot be quarantined without editing the file. It
  fails closed and needs an owner decision.
- **R2:** MVP-2 audit records without `store_seq` still use the weaker check, comparing bar close (exchange
  time) with wall time.
- **R3:** freshness is judged per session, not per symbol. If one symbol's feed dies, its marks go stale while
  the session label can stay SIMULATED until the session-wide freshness window lapses.
- **R4:** a foreign close of the subscription while market data runs is caught only by the 180 s watchdog.
  Close detection relies on `MarketDataService.stop()` clearing `running` first, which the tests pin.
- **R5:** the audit log is read on the event loop at every publication. The log is small.
- **R6:** restart equivalence (Part B) remains open.
