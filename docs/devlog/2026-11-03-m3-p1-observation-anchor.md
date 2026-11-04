# 2026-11-03 — M3 Part B: P-1 production fix (observation anchor, deferred reference warm-up)

**Status:** implemented on `task/m3-partb` on top of `e87fe59`. The independent review gave **CONDITIONAL PASS /
CONDITIONAL PASS** (`reviewer`, `causality-auditor`, 0 blocking; [below](#independent-review-2026-11-03)), and the
implementation is committed as a local checkpoint pending hardening (not pushed, not merged). **P-1 is not
unconditionally accepted**: F-1, F-2, F-3, N-1 and N-2 were open at the checkpoint; their hardening is a local
checkpoint with independent review CONDITIONAL PASS / CONDITIONAL PASS ([hardening devlog](2026-11-03-m3-p1-hardening.md)). The delayed-restart property, full Part B and the M3
gate are not accepted. Brief:
[M3-PB](../tasks/M3-PB.md#p-1-implementation). Owner decisions: D-19 (L-02 clarification), D-20 (harness rule).

## Root cause
- `PaperTradingService._observed` decided observed from the bar source only (`WS_LIVE`).
- `PaperRuntime.observing_since_ms` was set at the first poll, then only published, never read.
- So three kinds of minutes were processed observed although L-02 does not count them:
  - the partly observed first minute of a run;
  - the minute a crash interrupted;
  - the minutes queued during the market-data startup wait (audit N-1).
- On those minutes a resting entry could fill at the open or intrabar, in every book (strategy, benchmark, baseline).

## Correction
| File | Change |
|---|---|
| `src/tsclaude/sim/paper.py` | `clock_uncertainty_ms(max_rtt)` = `(max_rtt + 6) // 2`; `observation_anchor_ms(local, offset, max_rtt)` = the first minute open at or after `local + offset + uncertainty`. `PaperTradingService(observation_gated=...)`, `observe_from(A)` (a minute open, set once). Gated: observed iff the source qualifies and `open >= A`; no anchor, nothing observed. Ungated: continuous observation (tests, the instant oracle). `status()["observation"]` |
| `src/tsclaude/runtime/paper_runtime.py` | Always opens the service gated. `_establish_observation` before each poll until it succeeds: no anchor without a snapshot, with an offset that is not LIVE, a round trip from another round, a round from the local future or older than `clock_offset_max_age_ms`, or `abs(offset) > clock_skew_max_ms`. `DeferredReference`: `bracket_reference` built at the first observed bar, `trade_from_ms = A`, warmed up from the live bar store with every bar closed before A. `observation` published. Docstring and LIMITATIONS updated |
| `src/tsclaude/quality/clock.py` | `ClockRound.max_rtt_ms` (largest round trip of the round) |
| `src/tsclaude/marketdata/models.py`, `service.py` | `MarketSnapshot.clock_max_rtt_ms` (`Sourced[int]`, same `as_of_ms` as the offset; default UNAVAILABLE, so a producer that does not report it blocks observation) |

Not changed: `engine.py`, `book.py`, `live_adapter.py`, the `LiveStrategy` protocol, the journal schema,
`PAPER_VERSION`, `contracts/`, the API and the frontend.

## Clock bound (validated before relying on it)
- **Measurement contract** (`ClockMonitor`): each sample's server reading is taken between the local send and receive
  instants; the three readings are floored milliseconds; the round's offset is `round(median(server - midpoint))`.
- **Per sample:** the true offset exceeds the estimate by less than `rtt / 2 + 1`. This uses causality only, with no
  symmetry assumption about the network.
- **Round:**
  - The median of the estimates is bounded by half the **largest** round trip. Rounding adds 0.5 ms and the poll's
    own reading adds less than 1 ms, so `exchange_at_poll < local + offset + max_rtt / 2 + 2.5`.
  - The median round trip is also a valid bound: an order-statistics argument, for odd and even sample counts.
    The maximum was chosen because its proof is simpler and it is more conservative. It costs at most one minute,
    and only when a poll falls that close before a minute open.
- **Sampled check:** 20,000 random rounds (1 to 5 samples, round trips up to 400 ms, any stamp position) and polls up
  to 900 s after the round.
  - The implemented bound had 0 violations, and so did the median variant.
  - Weaker bounds were rejected: 7,550 violations without the resolution term, 21 with a margin 1 ms short, 15,171
    with no uncertainty.
- **Residual assumptions (not measurable here):**
  - the offset does not drift or step between the round and the poll (the round may be up to
    `clock_offset_max_age_ms` = 900 s old; quartz drift of 50 ppm over 900 s is about 45 ms);
  - Binance REST `serverTime` and kline open times use the same exchange clock.
- No arbitrary margin, service or dependency was added.

## Warm-up continuity
- The strategy is now built at the first observed bar, and the minutes before the anchor reach it through the store,
  as warm-up history. The market-data bar builder stores a closed bar before publishing it (`bars.py`
  `_store` → `on_bar`), so the minute before A is in the store when bar A is released.
- `test_a_deferred_reference_decides_as_an_uninterrupted_one_from_the_anchor` (30 unobserved live minutes before the
  anchor):
  - the deferred adapter's CONFIRMED decisions equal an uninterrupted `StrategySource` trading from A;
  - it has the same history breaks and is ready;
  - an adapter built at the start instead (the old behaviour) breaks its history and decides less.
- `test_nine_day_startup_backfill_reaches_the_reference_warm_up` (the real `Backfiller` and `LiveBarStore`): the
  builder's minute and the partly observed start minute arrive live and unobserved, and still reach the warm-up
  without a break. `trade_from_ms = A`, `warmup_bars = 2 × (A − begin) / 1 min`, `skipped_not_newer = 0`,
  `history_breaks = 0`, ready.
- `test_bracket_reference_is_warmed_up_causally_and_labelled_in_status`:
  - 242 warm-up bars (minutes 0 to 120 for both instruments; minute 121 already stored and not used);
  - two warm-up-phase breaks from the toy history's incomplete first 15m bucket, the same as a warm-up at minute 120
    gives (checked with a probe), and none in the live phase.

## Harness (D-20)
- `reconnect` sets the anchor with `observation_anchor_ms(restart, 0, 0)` (the harness clock is the exchange clock).
- `recovery_outcome`:
  - expects observed iff `open >= A`;
  - reports a service whose anchor differs;
  - checks the strategy against every bar before A.
- `reconnect(..., gated=False)` keeps the pre-fix service. It is used only to show that the checks still report P-1.
- `restart_causality`, the instant oracle (`redeliver`, no anchor) and the canonical journal and ledger comparisons
  are unchanged.

## P-1 counterexamples, before → after
| Case | Before (pre-fix service) | After |
|---|---|---|
| 696831125 / 13 / 180,000 | baseline and shadow SIG3 entries at the minute-10 open | `[]`; both expire `valid_until_passed:downtime` on minute 10 (unobserved) |
| 853832589 / 176 / 180,000 | shadow SIG3 entry at an open during the downtime | `[]` |
| 0 / 1 / 58,502 | benchmark BTC entry at an open during the downtime | `[]` |
| 0 / 2 / 1 | benchmark BTC and ETH entries in the interrupted minute | `[]` |
| 23511 / 118 / 30,000 | shadow SIG8 entry in the interrupted minute | `[]` |
| 853832589 / 176 / 30,000 | shadow SIG3 entry in the interrupted minute (TP over the backfill not reported) | `[]`; the take-profit still resolves over the backfill |
| 32438366 / 129 / 24,837 | shadow SIG7 entry intrabar in the interrupted minute | `[]` |
| 891223841 / 151 / 35,763 | shadow SIG12 entry intrabar in the interrupted minute | `[]` |
| 0 / 1 / 58,500 and 58,501; 13073 / 124 and 465923499 / 162 at 30 s and 10 min; 23511 / 118, 853832589 / 176 and 696831125 / 13 at 10 min | `[]` | `[]` |
| 400 random delayed restarts (RNG 31337) | 32 with a P-1 entry fill | 0 with any problem |

## Validation (focused, `-p no:cacheprovider`, sequential batches)
| Batch | Result |
|---|---|
| `tests/unit/sim/test_sim_property_regressions.py` | 74 passed |
| `tests/unit/sim/test_paper_observation.py` (new) | 20 passed |
| `tests/unit/sim` (includes both) | 382 passed |
| Paper integration (`test_mvp2_paper`, `test_mvp21_paper_hardening`, `test_mvp3_reference_paper`, `test_mvp2_paper_api`, `test_mvp4_paper_api`, `test_p1_observation_anchor` new) | 69 passed |
| `tests/unit/marketdata`, `tests/integration/marketdata/test_mdi_service.py`, `test_m1t2_market_api.py`, `tests/unit/strategies/test_live_adapter.py` | 154 passed |
| `tests/unit/risk` | 70 passed |
| `tests/property/sim`, 3 runs, fresh Hypothesis database each | 10 passed in each run (P-1 `@example`s included); the empty-minute limitation was not hit by these random draws, so it is still open, not resolved |
| `ruff check`, `ruff format --check` (src, tests) | clean |
| `mypy src` (fresh cache) | no issues in 116 files |

The full suite, the frontend and CI were not run.

## Mutants (disposable copies of src/, tests/, configs/ under the scratchpad)
Oracle: the 7 focused test files (154 passed on the unmutated copy). Every run printed the `tsclaude` it imported
(the copy). After the runs, the repository's non-docs diff hashed to the value recorded before them.

| Result | Mutants |
|---|---|
| Caught (24) | uncertainty without the resolution term, 1 ms short; anchor floored instead of ceiled, without uncertainty, without the offset; observed strictly after the anchor; gated without an anchor observes; gate ignored; `observe_from` does not gate, can be reset; runtime ungated, never anchors, no age check, accepts a round from the local future, no skew check, accepts a round trip from another round; reference built at the start; deferred `trade_from_ms` a minute early; deferred adapter rebuilt on every bar; clock round keeps the median round trip; harness `reconnect` ungated, outcome judged by close against the raw restart, anchor from the raw restart; harness outcome without its anchor check (caught after a test was added, below) |
| Equivalent (2) | observed judged by `close_time_ms >= A` (A and every open are minute-aligned and close = open + 59,999); runtime establishing the anchor after the poll instead of before (a gated service without an anchor already processes everything unobserved, and every bar the poll releases opens before the anchor) |
| Survives, not a safety defect (1) | service publishing the median round trip instead of the largest: the median is also a proven and sampled sound bound (above), and the service rig's manual clock gives every sample a 0 ms round trip, so median = maximum |

The harness check "observing from X, not from the anchor A" initially survived, because no test gave
`recovery_outcome` a service with another anchor. `test_recovery_outcome_reports_each_violation` now does; the
mutant is caught, and the regression file is still 74 passed.

## Changed expectations (intended, not weakened)
- The P-1 characterisation pins now expect `[]` or no fill, and each one also asserts the pre-fix service's report
  (`gated=False`).
- Recoveries: live bars are observed exactly from A, and the strategy never sees a bar before A.
- `test_p4_complete_backfill_from_live_bars_resumes`:
  - The rig's first poll is not deterministic (it can come before or after the first delivered minute), so the test
    asserts the exact flags of each case.
  - Minute 0 is never observed, minute 1 only if S1 polled before it opened, minute 2 always. S2's live minute 6 is
    observed iff it opens at or after S2's recorded anchor.
- Reference tests:
  - the strategy is a `DeferredReference` until the first observed minute, and the reference block is published
    from then on;
  - the benchmark buys at the first observed bar by design, so it is excluded from the "warm-up never trades" check.

## Findings and limitations
- **New, not P-1, not remediated:**
  - The runtime polls once per subscription update. A backlog queued during the startup wait therefore releases its
    first minute before that minute's second bar is offered; the second bar is then classified late, and the minute
    is processed incomplete. The same happens without the gate (probe on the service: rows `[0, 1, 1, 2, 2]`, 1 late,
    1 incomplete minute, gated and ungated).
  - Before P-1 that incomplete minute was processed observed; now it is unobserved.
- **Residual clock assumptions:** drift or a clock step between the round and the poll; one exchange clock. See
  above.
- **Behaviour change:**
  - every run starts unobserved until A (one or two minutes); an entry whose validity passes in that window expires
    as `downtime`;
  - the reference block in the status is `null` until the first observed minute.
- **Unchanged and still open:** the empty minute (130741648 / 8, 50553820 / 135); closes made by a backfill not
  journaled; no cross-platform CI; `LiveClock.release` clock skew (NB-1, now argued harmless for P-1 because all
  boundaries are in exchange time); an intra-session WebSocket reconnect is covered only by the source rule.

## Independent review (2026-11-03)
Owner-authorized, read-only, two isolated contexts. The repository was unchanged before and after (the diff hash and
21 file hashes are identical).

| Reviewer | P-1 | M3-PB.2 preservation | Checkpoint |
|---|---|---|---|
| `reviewer` (correctness, compatibility) | CONDITIONAL PASS, 0 blocking | PASS | acceptable |
| `causality-auditor` (timing, observation) | CONDITIONAL PASS, 0 blocking | preserved | acceptable |

### Confirmed
- Root cause: observation decided by source only.
- The correction:
  - one production construction path, always gated;
  - the anchor is set before the poll;
  - nothing at or after A can be processed unobserved;
  - no fill or signal on unobserved bars; SL/TP unchanged.
- The 8 pinned P-1 counterexamples give `[]`, with no entry fill before A and no decision on an unobserved bar. The
  pre-fix model still reports each of them.
- Random checks: 250 (reviewer) and 150 (auditor) delayed restarts showed no P-1 violation. The only problem found
  was the pre-existing empty minute.
- 240 restarts around a minute open (−4 to +1 ms) showed no problem.
- Clock bound: the auditor derived it independently. `exchange_at_poll < local + offset + max_rtt / 2 + 2.5` holds
  under these assumptions:
  - (a) constant offset from the round to the poll;
  - (b) `serverTime` stamped inside the send/receive window;
  - (c) sub-millisecond local clock resolution (100 ns here);
  - (d) one exchange clock for spot and USD-M;
  - (e) RTT ≥ 0.

  The median round trip is also a sound bound.
- Warm-up is causal and continuous. Store before publish was confirmed, the range is end-exclusive, and `warm_up`
  drops bars that close at or after `trade_from`.
- Journal and replay are compatible: `observed` is replayed from the record and nothing new is journaled.
  `PAPER_VERSION` and the protected modules are unchanged.

### Open findings (none blocking; all introduced with P-1 unless noted)
- **F-1 (significant).** Clock drift or an OS clock step between the round and the poll is neither bounded nor
  detected, so a minute that already started can count as observed. That contradicts the letter of D-19.
  - Drift: round 839 s old, local clock 50 ppm slow, U = 23 ms. The poll at 08:00:59.977Z gives A = 08:01:00.000Z,
    while exchange time is 08:01:00.018Z.
  - Step: a 300 ms backward step leaves minute A started 200 ms before the poll.
  - The effect is milliseconds, but untested.
  - Candidate remedies: a drift allowance ρ × age plus step detection (100 ppm × age passes all 154 focused tests),
    or documentation of the assumption as an owner decision.
- **F-2.** The producer of `clock_max_rtt_ms` is not pinned: a service publishing 0 (unsafe) survives the focused
  tests, because the service rig's round trips are all 0.
- **F-3 / N-3.** A round whose samples all have a negative round trip makes `clock_uncertainty_ms` raise, and the
  runtime stops as `CONSUMER_DIED` (fail closed, but it halts paper). Further gaps:
  - a round with some negative samples is accepted;
  - the round trip's provenance is not checked (a DEMO round trip of 0 would give an anchor).
- **N-1.** While `reference` is `null` (1 to 2 minutes at each start, or unbounded with an untrustworthy clock), the
  Paper page shows "No live strategy is selected" (`frontend/src/pages/PaperPage.tsx`). That is false.
- **N-2.** `/api/paper/status` does not expose `observation`, and no problem is raised when no anchor can be set. A
  stale or skewed clock therefore means RUNNING but never observing, with no visible reason.
- **N-4.** A failed deferred build is retried on every observed bar (a full warm-up read each time). A build that
  succeeds late in a minute can break the reference history.
- **N-5.** Test gaps:
  - the LIVE-provenance check of the offset;
  - `start()` resetting `observation`;
  - the harness strategy check for the interrupted minute.
- **Nits.**
  - Nine-day warm-up test: its decision check is vacuous (0 decisions); the warm-up count, no-break and readiness
    checks are not.
  - P4 relaxation: it is not tied to S1's anchor.
  - The anchor evidence is not journaled.
  - "2.5 ms" in SIMULATION §7: the code adds 3 ms for an even round trip.
- **Startup backlog (predates P-1; non-blocking for P-1, blocking for Part B).**
  - Confirmed on a HEAD copy. A late bar of the backlog's first minute is never processed and cannot be backfilled.
  - In 51 of 117 restored sessions an exposed instrument loses a bar.
  - This changes exit timing. In 603669584 / 125 a baseline ETH stop-loss fills a minute late, at 988.5 instead of
    990.0. The same happens at HEAD.
  - P-1 prevents entries and signals on that minute, but not the lost exit evaluation.
- **Still open (unchanged):**
  - the empty-minute recovery limitation (130741648 / 8, 50553820 / 135; the reviewers found 686072667 / 82 / 2 and
    923710 / 2 / 30,000 too, all identical at HEAD);
  - closes made by a backfill are not journaled;
  - no full-suite, CI or cross-platform run.

### Evidence (scratch, outside the repository)
`reviewer`:
- focused tests: 382 unit sim, 94 regression and observation, 69 paper integration, 118 market data, 85 risk and
  live adapter, and 10 property tests, all passed;
- ruff and mypy clean;
- 10 of 12 own mutants caught.

`causality-auditor`:
- 39 and 99 focused tests passed;
- 10 property tests passed;
- 5 own tests: 4 fail on current code (F-1 drift, F-1 step, F-3 twice), 1 passes (the producer pin, which fails on
  the 0 and median mutants);
- mutants: M3 to M8 caught; M1 (publishes 0), M2 (median) and M9 (drift allowance) survive.

The lead re-ran the auditor's 5 tests: 4 failed, 1 passed, as reported.
