# Devlog — 2026-10-27 (local time UTC-6) — sim property-test defects

Branch `fix/sim-property-defects`, cut from `49823ae` (separate from `fix/m2-bounded-importer` / `7d615a4`).
Committed locally as a PARTIAL fix (owner approval G-1); not pushed. Targeted tests only (the machine is under hardware investigation: WHEA parity errors and
native crashes), so every pass below is **provisional** until hardware stability is established.

## Scope
Two Hypothesis property tests fail on `49823ae` (they reproduce on a pristine `git archive` of that commit):
`test_live_clock_releases_historical_order` (seed 118309, settle_ms 111026) and
`test_restore_after_any_journal_failure` (seed 23511, fail_batch 118).

## Defect 1 — `LiveClock` fixed its position too early (`src/tsclaude/sim/live.py`)
- Root cause: `release()` set `self._next = min(self._buffer)` on the first call even when that minute was
  neither complete nor due. With bars of adjacent minutes overlapping in flight (settle window longer than
  a minute) the first arrival can be a minute-1 bar; minute 0's bars, still inside their own settle window,
  then arrived behind `_next` and were classified **late** (dropped), so the released sequence differed from
  the historical order.
- Fix: until the first release the minute is evaluated as `min(self._buffer)` on each call; `_next` is only
  advanced by an actual release (existing line `self._next = minute + MS_PER_MINUTE`). Nothing else changes:
  release still requires the minute to be complete or due, bars inside a minute are sorted by `order_key`,
  the defensive `_last_key` check is untouched, `start_ms` / `processed` / `skip_to` restarts (which set
  `_next`) are unaffected.
- Residual, not fixable without changing documented behaviour: with no declared `start_ms`, a clock cannot
  tell "session begins at minute 1" from "minute 1 completed before any minute-0 bar arrived". The existing
  unit test `test_live_clock_releases_complete_minutes_in_global_order` requires the first complete minute to
  release at once, so a stricter initial rule would break it. 3 of 6,000 random (seed, settle) cases still
  fail the property (≈0.05%); with `start_ms=minute(0)` they pass (pinned in the new tests). A production
  wiring should pass the session's start minute. `PaperTradingService` currently defaults `start_ms=None`.

## Defect 2 — restore treated an in-session feed gap as downtime (`src/tsclaude/sim/paper.py`)
- Root cause: `missing_before()` (used by `_contiguous()` after a restore and by `resume_after_downtime`)
  listed every minute since an exposed instrument's last processed bar as missing. In seed 23511 BTC's
  minute-21 bar never arrived while the session ran (a feed gap the books handled with a `BAR_GAP`), ETH's
  minute-21 bar was the last journalled batch, and the next write failed. After the restart BTC 21 was
  reported as unobserved downtime: the service sat in `AWAITING_BACKFILL`, and no backfill could ever fill it
  because the engine never accepts a key at or before `last_key` again.
- Fix: gap minutes whose delivery key `(minute, str(market), symbol)` is `<= session.sim.last_key` are not
  missing: the session had already advanced past them. Minutes after the journal's last key (real downtime)
  are still reported; the `"*"` continuity entry is unchanged; the clock's late/duplicate rules are unchanged.
- Measured on the same 400 random (seed, fail_batch) cases: baseline 12 failures, with the fix 4.

## Restore property: NOT fully fixed (two further classes, found by both reviewers and by sampling)
1. **Crash in the middle of a minute with a never-delivered symbol** (seed 13073, fail_batch 124): last
   journalled bar is spot BTCUSDT minute 25; ETHUSDT minute 25 never arrived. Its key is *after* `last_key`,
   and the journal cannot distinguish "not yet processed / lost in the restart" from "never existed".
   The service fails closed (waits for a backfill, processes nothing), which is correct for real downtime but
   leaves the service stuck if the exchange simply has no bar. Resolving it needs a recorded per-minute
   close in the journal (a journal-format design decision, outside this scope).
2. **Release-latency dependence** (seed 465923499, fail_batch 162): the shadow book never fills before the
   wall time a bar was released; the restart changes that time, so the shadow ledger differs while the
   baseline and benchmark ledgers are identical. A limit of the property's equivalence claim.
Both are pinned as characterisation tests (not xfail; the property test itself is unchanged and still fails
on these seeds). `test_restore_after_any_journal_failure` is therefore **not green**.

## Files
- `src/tsclaude/sim/live.py` (+5/-3), `src/tsclaude/sim/paper.py` (+7)
- `tests/unit/sim/paper_scenarios.py`: optional `start_ms` argument on `check_live_clock_order`
  (default `None`, behaviour unchanged)
- `tests/unit/sim/test_sim_property_regressions.py` (new, 9 tests)

## Tests (targeted; `.venv` executables only)
- New file: 9 passed. On the baseline `live.py` 2 of them fail; on the baseline `paper.py` 3 fail.
- `pytest tests/unit/sim tests/property/sim -q`: **286 passed, 1 failed** (12 s). The failure is
  `test_restore_after_any_journal_failure` on the stored example seed=13073, fail_batch=124 (class 1 above).
- `ruff check src tests scripts`, `ruff format --check src tests scripts`: clean;
  `mypy src/tsclaude/sim`: no issues. No full suite run (hardware restriction).

## Reviews (independent; both read-only)
- sim-risk-engineer and causality-auditor agreed: no look-ahead, no reordering, fail-closed preserved,
  real downtime still blocks (tested). Both reproduced the property failure of class 1 and called the
  "restore equivalence fixed" claim false; the claim is therefore not made.
- Non-blocking: `release()` walks empty minutes one by one (O(gap); pre-existing, now reachable from a stray
  old bar while `_next is None`); production wiring should pass `start_ms`; a position whose feed died before
  the crash resumes with stop/TP evaluated on its next bar (same as an uninterrupted run); the audit did not
  verify that the freshly built `MarketStateTracker` blocks entries after `open()` (not changed here).
