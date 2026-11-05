# 2026-11-03 — M3 P-1 focused hardening (F-1, F-2, F-3, N-1, N-2)

Status: **implemented on `task/m3-partb` (on `91e9f4e`); independent review CONDITIONAL PASS / CONDITIONAL PASS
(0 blocking, [below](#independent-review-2026-11-03)); committed as a local checkpoint (not pushed, not merged).**
P-1 is **not** unconditionally accepted; Part B and the M3 gate are not accepted. Follow-up: R-1/NB-2 test
coverage added and NB-1's assumption restated, committed as local checkpoint `b4f8d01` (2026-11-04; not pushed, not
merged); its independent closure review gave **PASS** (R-1/NB-2 CLOSED, NB-1 wording CLOSED, 0 blocking;
[below](#closure-review-2026-11-04)). NB-1's compound-clock risk remains; the owner chose mitigation option B with a
conditional acceptance of the residual risk (D-21), implemented in the
[NB-1 devlog](2026-11-04-m3-p1-nb1-fresh-round.md) ([follow-up](#follow-up-nb-1-and-r-1nb-2-2026-11-03)); its
independent causality review gave PASS (0 blocking) and the owner **closed NB-1** under D-21 on 2026-11-04 (residual
risk explicitly accepted for the supervised paper-only MVP). Owner authorization: "P-1 Focused Hardening
Authorization" (2026-11-03). Protected code untouched: engine, book, live adapter, journal schema, `PAPER_VERSION`,
`src/tsclaude/contracts/`. Startup backlog, empty minute and backfill closes were not worked on.

## The clock model (F-1)
Notation: true time is exchange time; W is the wall clock, M the local monotonic clock; every reading is floored to
1 ms; r is the rate allowance (`CLOCK_DRIFT_PPM` = 500 ppm, `drift_allowance_ms(n) = ceil(n r / (1 - r))`).

- **Producer.** `ClockMonitor` now reads M next to W at every send and receive and at the round end
  (`ClockSample.mono_*`, `ClockRound.min_rtt_ms`, `mono_at_ms`, `mono_span_ms`, `step_ms`). `step_ms` is the spread of
  `W - M` over every reading of the round. `MarketDataService` publishes `MarketSnapshot.clock_continuity`
  (`Sourced[ClockContinuity]`, LIVE, `as_of_ms` = the round's `at_ms`): samples, min round trip, round span, step, and
  the monotonic and wall time elapsed since the round, measured at the snapshot. Partial monotonic readings are no
  evidence (`UNAVAILABLE`). The production factory passes `MonotonicClock` (`CLOCK_MONOTONIC_RAW` where available,
  else `time.monotonic`: QueryPerformanceCounter on Windows, not adjustable).
- **Bound** (`observation_bound`, `sim/paper.py`, derivation in its docstring). With E = max(monotonic elapsed, wall
  elapsed): `t(P) < w_R + E + offset + (max_rtt + 6) // 2 + 1 + ceil(1.5 S) + drift_allowance(span + E + 2)`; the
  anchor is the first minute open at or above it. The `+1` covers the elapsed reading's resolution, `ceil(1.5 S)` the
  wall/monotonic spread inside the round, the drift term both the round and the time since it.
- **Assumptions (and limits).** Each `serverTime` stamp is taken while its request is in flight; M runs within r of
  exchange time during the round; after the round **one and the same clock (M or W) runs within r over the whole
  interval from the round end to the poll** (M may pause in a suspend, or W may be stepped or slewed, but not one after
  the other; the clock that satisfies it cannot switch midway; see NB-1 below); one exchange clock for spot and
  USD-M. 500 ppm is an external assumption about the host's clocks: the runtime cannot verify it. 500 ppm is 5x the usual ±100 ppm crystal
  tolerance; it does **not** cover a CLOCK_MONOTONIC slewed by NTP faster than 500 ppm (only used where
  CLOCK_MONOTONIC_RAW is missing), a monotonic clock that is not monotonic, or simultaneous step of W and pause of M.
  A drift allowance is not protection against steps: steps are detected (below), not absorbed.

## Exact blocking conditions (no anchor; retried at every poll)
Checked in this order; the first that applies is the reason (`observation.code`):

| Code | Condition |
|---|---|
| `no_snapshot` | no market-data snapshot |
| `clock_offset_unknown` | offset not LIVE or missing |
| `clock_rtt_unknown` | round trip not LIVE (also DEMO/STALE) or missing |
| `clock_rtt_other_round` | round trip `as_of_ms` differs from the offset's |
| `clock_continuity_unknown` | continuity not LIVE or missing (e.g. no monotonic clock) |
| `clock_continuity_other_round` | continuity `as_of_ms` differs from the offset's |
| `clock_evidence_inconsistent` | samples < 1, min RTT > max RTT, negative span, step or monotonic elapsed |
| `clock_rtt_negative` | any sample's round trip negative (W stepped back during the round); never clamped |
| `clock_step_in_round` | `step_ms > continuity_tolerance_ms(span)` = 2 + 2·drift(span) |
| `clock_round_future` | wall elapsed < 0 |
| `clock_discontinuity` | \|wall elapsed − monotonic elapsed\| > `continuity_tolerance_ms(monotonic elapsed)` (step, suspend, fast slew) |
| `clock_round_stale` | max(elapsed) > `clock_offset_max_age_ms` |
| `clock_offset_skew` | \|offset\| > `clock_skew_max_ms` |

While blocked: paper stays `RUNNING`, every bar is processed unobserved as before (no entry fill, no signal; resting
SL/TP resolve; exit code unchanged), the reference strategy is not built, and one problem "Paper observation blocked" is raised
per **change of code** (not per poll). **Recovery:** the first poll with valid evidence (a new valid clock round for
round-level faults) sets the anchor; nothing before it counts.

## Operator-visible states (N-1, N-2)
`/api/paper/status` gains `observation` (null before the first poll; else `state` `active`|`blocked`,
`attempted_at_ms`, `observing_from_ms`, `code`, `problem`, and when active `offset_ms`, `max_rtt_ms`,
`uncertainty_ms`, `drift_ms`, `step_ms`, `allowance_ms`, `round_age_ms`). Existing keys unchanged. Dashboard:
"Observation" row in the runtime card; the strategy card distinguishes *not selected* (`strategy: none`, old text),
*selected, paper not running*, *waiting for the first market-data poll*, *observation blocked: reason*, and *waiting
for its first observed minute (from anchor)*. A backend without the field still renders.

## Evidence (focused, local, Windows, `-p no:cacheprovider`)
| Batch | Result |
|---|---|
| `tests/unit/sim/test_observation_clock.py` (new: allowances, ±1 ms boundary, drift by age, in-round step, invalid evidence, auditor counterexamples, monitor evidence, 6-scenario true-timeline simulation × 3,000) | 37 passed |
| `tests/unit/sim` (incl. `test_sim_property_regressions` 74, the 8 P-1 counterexamples, instant oracle, M3-PB.2/RR-B1) | 419 passed |
| Paper integration (`test_mvp2_paper`, `test_mvp21_paper_hardening`, `test_mvp3_reference_paper`, `test_mvp2_paper_api`, `test_mvp4_paper_api`, `test_p1_observation_anchor`) | 84 passed |
| Market data, risk, strategies (live adapter), runtime misc, contracts (`-m "not live"`) | 262 passed |
| `tests/property/sim`, 3 runs, fresh Hypothesis database each | 10 passed each |
| `ruff check`, `ruff format --check` (src, tests); `mypy src` | clean; 116 files, no issues |
| Frontend `tsc --noEmit`, `vitest run`, `vite build` (outDir in scratch) | clean; 99 passed; built |
| Python mutants on a scratch copy (oracle: 4 focused files, 83 passed unmutated) | 20 of 20 caught |
| Frontend mutants on a scratch copy | 4 of 4 caught |

Simulation: in the drift, independent-rates and step-back scenarios the pre-hardening bound misses some accepted
cases; the new bound has 0 violations in all 6 scenarios. Mutants caught include: drift, step or reading allowance
removed, E = monotonic only, each check removed, RTT provenance/round checks removed, service publishing RTT 0,
running maximum over rounds, negative RTT clamped to 0, continuity stamped at "now", problem on every poll, API key
dropped, production factory without monotonic clock. The first in-round-step mutant survived the first run and was
killed by a new deterministic boundary test. One unmutated baseline run of the mutant oracle exited with a native
access violation (0xC0000005) before any output; it did not reproduce in 3 later runs. pnpm is not installed on this
machine: the package scripts were run through `frontend/node_modules/.bin`. Not run: full suite, CI, Linux.

## Remaining findings
- Assumption-bound, not proven against an adversarial OS: see the limits above.
- Open, out of scope: the startup-backlog defect, the empty minute, closes made by a backfill, CI/Linux runs.
- Reviewer N-4 (retry cost each poll and late build) is unchanged; the per-poll check is O(1).

## Independent review (2026-11-03)
Read-only, isolated contexts, on the uncommitted tree (diff and file hashes identical before, during and after the
review; other worktrees untouched). The lead reproduced both material findings on its own disposable copy.

| Item | `reviewer` | `causality-auditor` | Consolidated |
|---|---|---|---|
| F-1 clock drift / steps | no objection | CONDITIONALLY CLOSED | **CONDITIONALLY CLOSED** (same-clock assumption) |
| F-2 RTT producer | CLOSED | CLOSED | **CLOSED** |
| F-3 invalid clock evidence | CLOSED | CLOSED | **CLOSED** |
| N-1 frontend deferred strategy | CLOSED | — | **CLOSED** |
| N-2 observation visibility | CLOSED | — | **CLOSED** |
| Verdict | CONDITIONAL PASS, checkpoint YES | CONDITIONAL PASS, P-1 acceptance CONDITIONAL, checkpoint YES | |

The auditor derived the bound independently from the measured readings: every term is needed and sufficient under the
assumptions; own true-timeline simulation 0 violations (minimum margin 2.35 to 3.8 ms), 39 of 39 boundary and invalid
evidence probes pass, the bound is monotone and never raises. Findings (all non-blocking):

- **NB-1 (condition for P-1 acceptance).** The proof needs the **same** clock within 500 ppm over the whole interval
  from the round to the poll. A sequential anomaly evades detection: the wall clock is stepped back by X, then a
  suspend stops the monotonic clock for Y close to X. Reproduced through `ClockMonitor.sample_round` ->
  `_clock_continuity` -> `_observation_verdict`: X = Y = 1,000 ms at about 600 s gives no code and an anchor in a minute
  that started 300 ms before the poll (also X = 1,000 / Y = 1,500 at about 850 s, and X = Y = 2,000 at about 30 s:
  600 ms); X = 1,000 / Y = 2,500 is detected. Also undetected: a VM pause or live migration that freezes both clocks,
  a slew shared by both. To do: correct the assumption wording (docstring and docs) and record compound anomalies as
  residual risk; optional mitigation: a fresh clock round at paper start.
- **R-1 / NB-2 (condition for P-1 acceptance).** No test pins the producer's continuity and step fields
  (`service.py` `_clock_continuity`: `wall_elapsed_ms`, `elapsed_ms`, `round_step_ms`). Mutants publishing
  `wall_elapsed_ms` from the monotonic clock or `round_step_ms = 0` pass every repo test (lead re-run: 103 passed each)
  and disable step and suspend detection in production; a service-level test with the wall clock moving against the
  monotonic clock (a step back and a suspend after the round, a step inside it) kills them.
- **R-2.** The D-20 delayed-restart harness (`tests/unit/sim/paper_scenarios.py:253,474`) still uses
  `observation_anchor_ms(restart, 0, 0)`, an idealized zero margin; production adds at least 2 ms.
- **R-3 / NB-4.** `snapshot()` reads the wall clock before building the symbols and the monotonic clock after: the
  skew (p99 about 0.26 ms, max 1.8 ms measured) only pushes towards blocking (availability, not safety).
- **R-4.** The anchor is computed at the snapshot instant, not at the runtime's `now` (sub-millisecond, safe); D-19
  says "first poll".
- **NB-3.** The stale-age policy boundary is untested (`age` from the monotonic clock only survives; safe because the
  bound still uses the larger elapsed time).
- **NB-5.** The spot `serverTime` is used for USD-M bars too (documented assumption).
- **NB-6.** A reconnect or suspend after the anchor within a session is covered only by the source rule (known).
- UI nits: the deferred card can keep "waiting for its first observed minute" after the anchor if the build keeps
  failing; the TS type omits `drift_ms`, `step_ms`, `round_age_ms`.
- Native crash 0xC0000005: about 1 in 40 pytest processes, not reproducible on demand (12 faulthandler re-runs clean),
  no causal link to this change (pure Python); consistent with the open WHEA hardware investigation, no stack captured.
- Reviewer own mutants: 15 caught, 3 meaningful survivors (R-1), 1 equivalent; frontend 4 caught, 1 equivalent.
  Auditor mutants: 12 unsafe killed by repo tests, 1 killed only by `test_mdi_service`, 1 unsafe survivor (R-1), 5 safe
  killed, 1 safe survivor (NB-3).

Still open beyond P-1: the startup backlog, the empty minute, closes made by a backfill, full CI and a Linux run.

## Follow-up: NB-1 and R-1/NB-2 (2026-11-03)
Owner authorization "Final P-1 Conditional Review Follow-up". Written on `42d6b0a`; committed as local checkpoint
`b4f8d01` on 2026-11-04 (not pushed, not merged). No runtime, formula, API,
frontend, journal, risk, strategy or contract change: the only source change is the `observation_bound` docstring.

- **R-1/NB-2, coverage added.** `test_the_producer_publishes_wall_and_monotonic_evidence_from_their_own_clocks`
  (`tests/integration/marketdata/test_mdi_service.py`, 6 cases) runs the real `MarketDataService` and `ClockMonitor`
  with separate fake wall and monotonic clocks and checks the published `elapsed_ms`, `wall_elapsed_ms` and
  `round_step_ms`, the offset (12) and round trip, LIVE provenance and the same round for all three, the code the
  runtime's `_observation_verdict` gives, and recovery on the next valid round. Cases: valid clocks (wall 1 ms ahead
  of monotonic over 120 s: anchor, `round_age_ms` = the larger elapsed), wall step back 300 ms after the round
  (`clock_discontinuity`), 59 s monotonic suspend after the round (`clock_discontinuity`), wall step +500 ms inside
  the round (`clock_step_in_round`), monotonic clock 5 ms back (`clock_evidence_inconsistent`), wall clock 10 ms behind
  the round (`clock_round_future`). Mutants on a disposable copy (oracle: the 5 focused files, 109 passed unmutated):
  `wall_elapsed_ms` from the monotonic clock (M10/M17) 5 failed, `elapsed_ms` from the wall clock (M11) 5 failed,
  `round_step_ms` = 0 (M19) 1 failed, monitor step not measured 3 failed, monitor min round trip from the maximum
  2 failed: 5 of 5 caught. Before this test the first three passed every repo test (103 passed).
- **NB-1, assumption restated** (docstring, this devlog, SIMULATION §7, D-19): the bound holds only if one and the
  same clock, wall or monotonic, stays within 500 ppm over the whole interval from the clock round to the poll; the
  clock that satisfies it cannot switch midway.
- **NB-1, residual limitation (real, not fixed).** A wall-clock step back followed by a roughly compensating
  monotonic suspend cancels in the discontinuity check: re-reproduced on the current code through
  `ClockMonitor.sample_round` -> `_clock_continuity` -> `_observation_verdict` (X = Y = 1,000 ms at about 600 s: no
  code, anchor in a minute that started 300 ms before the poll; X = 1,000 / Y = 1,500 at about 850 s: 300 ms;
  X = Y = 2,000 at about 30 s: 600 ms; X = 1,000 / Y = 2,500: detected). Also not detectable: other sequential or
  compound anomalies whose net wall/monotonic movement stays within the tolerance, anomalies of both clocks at once (a
  slew shared by both), a VM pause or live migration that freezes both clocks, a host clock outside 500 ppm (an
  external assumption). What the runtime can detect still fails closed: a net wall/monotonic movement beyond
  `continuity_tolerance_ms` during or since the round, a negative round trip, inconsistent, missing, non-LIVE or
  other-round evidence, a stale round, an offset beyond the skew limit. No new runtime mitigation was implemented.
- **Design note (not implemented; the owner did not authorize it): a fresh clock round at paper start.** It would
  bound the round-to-poll interval to the round's own duration (about a second) instead of up to
  `clock_offset_max_age_ms` (15 min), so a step and a matching suspend would both have to fall inside that second:
  a large reduction of NB-1 exposure, not its removal (the cancellation does not depend on the round's age; X = Y =
  2,000 at 30 s is still undetected). It would add an operational dependency: paper start (anchor establishment)
  would need the REST time endpoint reachable at that moment, adding latency, request weight and a new fail-closed
  path (no anchor while that request fails), and it would change startup sequencing.
- **Not changed:** the runtime `LIMITATIONS` text served by `/api/paper/status` ("assumes the local monotonic clock
  runs within 500 ppm…") is left as is because API output changes were not authorized; it does not state the
  same-clock condition.
- **Validation:** `test_mdi_service`, `tests/unit/marketdata` (incl. freshness/clock) 127 passed;
  `test_observation_clock`, `test_paper_observation`, `test_sim_property_regressions`, `test_p1_observation_anchor`,
  `test_mvp2_paper_api` 168 passed; ruff, format (touched files), mypy `src` (116 files) clean.
- **Status:** P-1 technical acceptance remains **conditional** unless a separate owner decision explicitly accepts
  the NB-1 residual risk. Part B and the M3 gate are not accepted.

## Closure review (2026-11-04)
Owner authorization "Execute Approved Preservation and P-1 Review" (Steps 1-5). Before the review the follow-up was
preserved outside git (`C:\Users\felip\TSC_preservation\2026-11-04\`, SHA-256 manifest, 0 mismatches), re-validated
(`test_mdi_service` + `tests/unit/marketdata` 127 passed; the five observation files 168 passed; ruff, format and
mypy `src` (116 files) clean; mypy ran with a fresh cache because the local `.mypy_cache` is corrupt, and the project
`.venv` interpreter was used because `uv run` fails on this machine) and committed as local checkpoint `b4f8d01`
(parent `42d6b0a`, exactly the 8 reviewed files).

Independent `causality-auditor`, isolated context, read-only, on `git diff 42d6b0a b4f8d01`; mutants only in a
disposable `git archive` copy with `tsclaude` imported from the copy (verified). **Verdict: PASS, 0 blocking.**
- R-1/NB-2 **CLOSED**: the producer test drives the real `MarketDataService` -> `ClockMonitor` -> `clock_continuity` ->
  `_observation_verdict` path; assertions are not vacuous. 20 mutants: 19 killed by the focused suite (oracle 119
  passed), 15 by the new test alone; M10/M17, M11 and M19 (the previous survivors) killed.
- NB-1 wording **CLOSED**: the same-clock assumption and the residual cases are stated consistently; the auditor
  re-derived the bound under it. The AST of `paper.py` is equal ignoring docstrings: no implementation change.
- NB-1 residual risk **still open** (re-reproduced on `b4f8d01`).
- Non-blocking: **N-1** the new test does not pin the anchor value (unit tests do); **N-2** mutant X9 (receive
  readings dropped from the round's step spread) survives every test, a `ClockMonitor` internal, not an R-1 field;
  **N-3** the anchor overshoot under the NB-1 counterexample has no fixed cap, about the step size minus the
  allowance over all phases (677 ms for X = Y = 1 s, 1,963 ms for X = Y = 2 s); the "300 to 600 ms" quoted above are
  instances; **N-4** the served `LIMITATIONS` text lacked the same-clock condition and NB-1; **N-5** status texts still
  said "uncommitted".
- N-3, N-4 and N-5 are addressed with D-21 ([NB-1 devlog](2026-11-04-m3-p1-nb1-fresh-round.md)); N-1 and N-2 stay
  open (minor).
- **Status:** R-1/NB-2 closed. P-1 acceptance stays conditional on D-21's conditions (the mitigation's regression
  tests and its own independent causality review). Part B and the M3 gate are not accepted.
- **Update (2026-11-04):** D-21's conditions are met (full non-live suite 1513 passed; independent causality review
  PASS, 0 blocking) and the owner **closed NB-1** ([NB-1 devlog](2026-11-04-m3-p1-nb1-fresh-round.md)). Part B and
  the M3 gate are still not accepted.
