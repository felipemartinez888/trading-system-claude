# 2026-11-06 — First real recorded-session replay (benchmark-only smoke test)

Session: `C:\Users\felip\tsc_runs\paper-20261106-090120\data` (30 min, `strategy: none`, paper only, fresh data
dir, 6 pairs). Checked read-only on the owner's data; the verifier copies the store and never opens the original.

## Result
`tsclaude verify-replay` → PASS, exit 0: 180 journal batches, 180 replayed, baseline / shadow / benchmark ledger
hashes equal the session-end evidence the live service wrote at the clean stop (`state: RUNNING`). Journal hash
chain verified. Re-run on the final verifier bytes (read-only) gives the same PASS.

## What it does NOT prove
- **No trading was exercised.** All 180 bars were `observed: false`; every ledger holds only its starting deposit
  (identical hash in the three books). Only the recording / evidence / replay chain is validated.
- Most likely cause (consistent with the code, not directly persisted): the machine clock was 6886 ms off Binance's
  (event-store event `clock_skew_changed`, offset -6886, threshold 1000, log line "clock skew threshold crossed"
  at 15:47Z; the only persisted observation reason is `downtime_backfill`). D-21 / `observation_bound` refuses an observation anchor when the offset exceeds `clock_skew_max_ms`,
  so every bar was processed unobserved: no entry may fill or be created. The fail-closed guard worked as designed
  and no look-ahead or temporal guarantee was weakened. Not a code defect; the fix is to sync the Windows clock
  (< 1 s) before recording.
- Strategy execution, fills, exits and restart equivalence remain unverified in a real session. The evidence is
  written by the same process that ran the session (reproducibility, not honesty of the run).

## Websocket error
One `websocket frame dispatch failed` (usdm-public bookTicker, 16:10:13Z): `TypeError: expected 2 arguments, got 3`
inside `StreamKey(...)` in `key_for`. Not reproducible: `key_for` ran 51.5 M times on a BookTickerUpdate with no
error, `StreamKey` takes 3 fields, and the 16 ws manager tests pass. The source reads correctly for that
call, so the cause is **unresolved**: an isolated observation, not attributed to hardware or to any other cause
without evidence. One book-ticker frame was lost; the read loop continued and bars are unaffected. No change made.
If it recurs, record the exact Python build, frame and surrounding log before touching code.

## Verifier change (this session)
A PASS keeps its strict meaning (hashes equal, journal chain valid, every batch reproduced) and now also reports,
computed from the REPLAYED ledgers (never from the evidence file): `observed_bars` (journaled `observed: true`
bars), `simulated_fills` per book (ledger memos `entry:` / `exit:`; the `capital` deposit is not a fill),
`trading_exercised` (observed bars and at least one simulated entry in any book) and `strategy_books_exercised`
(at least one entry in baseline or shadow). A benchmark-only PASS says so: it does not show that `bracket_reference`
executed. This session reports `observed_bars: 0`, zero entries and exits in all books, `trading_exercised: false`,
`strategy_books_exercised: false`. Verdicts and exit codes are unchanged.

## Still required for M3 acceptance
A real session with observed bars and simulated strategy fills (`strategy_books_exercised: true`) that replays PASS; the
fault-injection gate evidence; independent review of that session; CI / Linux run (push needs D-07).
