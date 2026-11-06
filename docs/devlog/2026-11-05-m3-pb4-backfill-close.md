# 2026-11-05 — M3-PB.4: backfill closes journaled (Design F) and the empty-minute residual

Status: **implemented on `task/m3-partb` (uncommitted, on `0ee0e4a`); focused validation passed; independent reviews:
`causality-auditor` PASS (0 blocking, 2 non-blocking), `reviewer` APPROVE WITH NITS (0 blocking, 4 non-blocking).**
Not committed (checkpoint awaits owner approval), not pushed, not merged. M3 Part B and the M3 gate are **not**
accepted. Owner authorization: "TSC — Owner Decision: Approve Design F and Begin M3-PB.4" (2026-11-05). The work was
interrupted by a Windows update restart; the builder's edits survived and were verified, completed and re-hashed by
the Lead ("TSC — Resume Interrupted M3-PB.4 After Windows Update Restart").

## Owner decisions applied
- **OD-1 (empty minute):** journal format kept; no new kind, no `PAPER_VERSION` change. The limitation is **not**
  eliminated: it is characterised (below).
- **OD-2 (strict restart equivalence):** unchanged. No oracle, property or acceptance criterion was modified; the
  known counterexamples are pinned as fail-closed, not as recovered.
- **OD-3 (backfill closes):** fixed with the existing `minute_closed` key (this task).

## Diagnosis (2026-11-05, before the owner decision)
A minute released with no bar for any instrument is released in the same poll as the next minute's bars and leaves no
input entry. If the first write of that poll fails, the durable journal ends before the empty minute, `restore` and
`_contiguous` report it as `'*'` (possible downtime) and the service waits in `AWAITING_BACKFILL`; no backfill can
supply bars that never existed. Swept over every crash point (instant restart): 130741648 fails only at batch 8
(empty minute 4; ledgers diverge: the uninterrupted session filled entries in minute 5) and 50553820 only at batch 63
(`fail_batch` 135 modulo 72 batches; empty minute 34; ledgers equal by chance). Every other crash point is equivalent.
A separate clock batch (new kind) would only move that one crash point to the marker write (the durable prefix at a
crash on the marker is byte-identical to today's), so it does not remove the ambiguity.

## Backfill-close defect (T4) and fix
`resume_after_downtime` processes the backfilled bars (unobserved) and calls `LiveClock.skip_to` past the last one,
closing those minutes; nothing durable recorded that close. After a second crash `restore` reopened the last backfilled
minute: a bar the uninterrupted service had classified late (ETH 8, not yet in the store when the backfill ran, ETH not
exposed) was processed and the strategy was called on it.

Fix (`src/tsclaude/sim/paper.py`, `resume_after_downtime` only, plus docstrings): the last bar of each backfilled
minute is stepped with `minute_closed=True` (same rule as `poll`), so `closed_through_ms` equals the `skip_to` point
and a restore restarts after the backfill. Backfilled bars stay `observed=False` (no entries, no signals); resting
exits still resolve over them; fills and ledgers are unchanged.

## Files (SHA-256, first 16 hex)
- `src/tsclaude/sim/paper.py` **e42cc66a514ba0ed** (builder: `sim-risk-engineer`)
- `tests/unit/sim/test_paper_backfill_close.py` **c78486014f75f8db** (new; builder, completed by the Lead after the
  restart: the in-backfill crash test passes the downtime to `restart_causality` and the close at the restore)
- `tests/unit/sim/test_sim_property_regressions.py` **28e92b0e19292809**: one assertion of
  `test_a_backfilled_bar_of_a_closed_minute_is_late` changed from "no mark on backfilled heads" to the exact list of
  marks (owner-authorized behaviour change; stricter)
- Unchanged: `contracts/`, journal keys and kinds, `PAPER_VERSION` (`paper-v1`), `sim/live.py` 6ed2f744aa589804,
  `runtime/paper_runtime.py` eaa0d275c74a5c7d, `marketdata/service.py` e0e6c923de8b2e0f, risk, configs, E-4(b).

## Evidence (newly executed 2026-11-05)
- Red: the new tests against HEAD production code (disposable copy, import verified): 10 behavioural tests fail plus
  the changed regression assertion; the 7 compatibility and characterisation tests pass (expected both ways).
- Green: new file + changed test 18 passed; focused set (`tests/unit/sim`, MVP-2/2.1/3 paper, P-1 anchor, PB.3
  backlog, MVP-2 paper API) **554 passed**; `ruff check src tests` clean, format clean, `mypy src` no issues (116
  files). The auditor also ran `tests/unit`: 1287 passed, 1 skipped. Full non-live suite not re-run.
- Coverage: marks on each backfilled minute; restore keeps the minutes closed; replay gives equal ledgers; a late bar of
  a finalized recovered minute is rejected; a crash at every backfilled bar and right after the backfill restores to
  the uninterrupted journal, ledgers and strategy calls (no duplicate bar); a journal without the marks keeps the
  previous reading; live-only journals carry no new marks; the two empty-minute counterexamples (exact crash points,
  problems, durable prefix, nothing appended, `AWAITING_BACKFILL`, `'*'` = the empty minute, ledger divergence).

## Compatibility
- Journals written before this change (backfills unmarked) replay, verify and restore with their previous reading.
- New journals: code at `0ee0e4a` already replays `minute_closed` on any bar, so it reads them identically (reviewer,
  by code reading; not run across versions). Code before M3-PB.1 fails closed. No migration.

## Independent reviews (mono-model, on the hashes above)
**`causality-auditor`: PASS, 0 blocking.** No early or extra close (marks end at the `skip_to` point; partial first
minute, filtered bars, fault mid-backfill and the all-or-nothing path checked); downtime is never read as a passed gap
(second-downtime probe still blocks); forged bar after a marked close refused by replay and restore; no interaction
with P-1, D-21, PB.3. Mutants: 13 of 14 killed; M5a (in-memory close after a fault) survives as equivalent.
- NB-1 (pre-existing on HEAD): `resume_after_downtime` checks completeness only before `first_live` but processes and
  skips every bar given; a direct caller passing bars at or after the first live minute skips an exposed instrument's
  minutes. Unreachable from the runtime (`validate_backfill` bounds the range). Follow-up: bound `todo` in the method.
- NB-2: the characterisation pins only `awaiting_missing['*']`.

**`reviewer`: APPROVE WITH NITS, 0 blocking.** Consumers of the key are only in `sim/paper.py`; no API change; the
changed assertion is stricter; tests deterministic (new file about 7 s).
- R-1: docstrings/comments still say "closed by the live clock" (`paper.py` ~833, ~958-961, ~976-977).
- R-2: dead field `Downtime.calls_at_restore`.
- R-3: the live-only test guards the unchanged `poll` rule.
- R-4: the sweep pins exact problem strings and batch indexes (intentionally brittle).

## Still open for M3 Part B
- Empty-minute residual (OD-1): one crash point per empty minute fails the strict instant-restart property in a
  fail-closed way; the property can therefore fail in CI on a random empty minute. Whether the formal criterion should
  distinguish "recoverable" from "insufficient persisted evidence" is an owner decision (no change made).
- The delayed-restart property and full restart equivalence re-run with PB.3/PB.4; CI and a Linux run.
- Non-blocking findings above and those of PB.3.
