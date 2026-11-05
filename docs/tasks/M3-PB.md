# Task brief M3-PB: paper restart equivalence, Part B class 1

- **Status:** local checkpoint `0435116944032b95ab21456f8c669c3d8af357b4` on `task/m3-partb` (base `f1d5079`), not
  pushed and not merged.
  - The first independent reviews of 2026-11-03 gave CONDITIONAL PASS for the class 1 remediation, from both
    `reviewer` and `causality-auditor`.
  - The independent re-review of M3-PB.1 on `0435116` gave **PASS / PASS**, 0 blocking (see
    [Re-review of M3-PB.1](#re-review-of-m3-pb1-on-0435116)). The M3-PB.1 implementation is accepted as a checkpoint.
  - M3-PB.2 (restart-timeline property harness, audit F1) is implemented on top of `da42955`. Its independent review
    gave `reviewer` CONDITIONAL PASS and `causality-auditor` **FAIL** (blocking finding B-1: the delayed-restart model
    was not production's). The B-1 remediation is implemented.
  - The independent re-review accepted the B-1 recovery model (`reviewer` CONDITIONAL PASS, `causality-auditor`
    FAIL). Both found one blocker, RR-B1 / BR-1: the P-1 check missed entry fills in the minute the crash interrupted
    (see [Re-review](#re-review-of-m3-pb2-with-the-b-1-remediation)).
  - The RR-B1 remediation (tests only) is implemented. Its closure review gave **RR-B1 PASS** and **B-1 preservation
    PASS** from both reviewers, with no new blocking finding (see [Closure review](#closure-review-of-rr-b1--br-1)).
    The M3-PB.2 implementation is accepted as a checkpoint and is committed locally on `task/m3-partb` on top of
    `da42955` (not pushed, not merged).
  - Production limitation **P-1** (an entry can fill in a minute the downtime overlaps) was confirmed. The owner
    approved D-19 and D-20 on 2026-11-03. The production fix is implemented, and its independent review gave
    **CONDITIONAL PASS / CONDITIONAL PASS** (0 blocking). It is committed as a local checkpoint pending hardening (not
    pushed, not merged). The P-1 hardening (F-1, F-2, F-3, N-1, N-2) is a local checkpoint; its independent review gave
    **CONDITIONAL PASS / CONDITIONAL PASS** (0 blocking): F-2, F-3, N-1, N-2 closed, F-1 conditionally closed; P-1
    acceptance is conditional on NB-1 (same-clock assumption, sequential step + suspend counterexample) and R-1/NB-2
    (producer continuity tests). Follow-up (uncommitted): R-1/NB-2 tests added (M10/M17, M11, M19 caught); NB-1's
    assumption restated, its residual risk remains real and unmitigated, so P-1 acceptance stays conditional unless
    the owner accepts that risk ([hardening devlog](../devlog/2026-11-03-m3-p1-hardening.md#independent-review-2026-11-03);
    see also [P-1 implementation](#p-1-implementation)).
  - **Full Part B and the M3 gate are not accepted.**
  - Evidence: [devlog](../devlog/2026-11-03-m3-partb-close-marks.md), [M3-PB.2
    devlog](../devlog/2026-11-03-m3-pb2-restart-timeline-harness.md).
- **Authority:** owner authorization of 2026-11-03 for Part B class 1, for the follow-up M3-PB.1, and for the harness
  remediation M3-PB.2.
- **Owner:** Lead (acting as Simulation & Risk builder). Reviewers: `reviewer` and `causality-auditor`, fresh context,
  brief and diff only.

## Goal
`test_restore_after_any_journal_failure` (seed 13073, fail_batch 124) showed that a restored paper service could wait
for a backfill that can never exist: the live clock had closed a minute at its deadline without one instrument, but
the journal could not tell that instrument from one whose bar was not processed yet. Restore must keep the clock's
recorded decision, without weakening fail-closed handling of real downtime.

## Two different things the stored example shows
| | Class 1 defect | Harness timing artifact (audit F1) |
|---|---|---|
| What | Restore treated a gap the session had passed as downtime, so the service stayed `AWAITING_BACKFILL` forever | The property re-drives the restored service on the original timeline, so it polls at wall times **before** the crash, which no real restart can do |
| Where | `sim/paper.py`: restore, `missing_before`, the journal | `tests/unit/sim/paper_scenarios.py` (`check_restore_equivalence`) with `paper_helpers.drive` |
| 13073 / 124 | The service never resumed | The failed write was in the poll at minute 28 + 1.5 s; the restored service released minute 26 at minute 27 + 1.5 s, a minute before the crash. That earlier release moves `earliest_exec_ms` and changes the shadow ledger |
| 465923499 / 162 | (not affected) | The crash poll was at minute 9 + 1.5 s; ETH 7 was released at minute 8 + 1.5 s |
| Status | Fixed by this task | Unchanged. Changing the harness changes what the property means, so it is an owner decision. Pinned as characterisations |

- **Audit sample, 2,000 cases, harness timeline:** 1,988 equal, 9 RUNNING with ledgers different (4 of them in baseline
  too), 3 `AWAITING_BACKFILL`.
- **Same cases, polling never before the crash:** 1,997 equal, 0 RUNNING divergences. The 3 remaining cases are
  minutes released with no bar at all, and they fail closed.
- **Production timing:** a real restart polls at or after the crash, so release, and therefore execution, can only be
  the same or later, never earlier. A real restart delay legitimately changes fills, in any book, the baseline
  included. That is causally valid and never optimistic (audit, class 2 severity: low).
- **Correction to the first version of this brief:** it described the RUNNING divergences as release-latency
  dependence of the simulator, affecting baseline in 2 of 2,000 cases. The mechanism is right, since release time bounds
  execution. The cause of the observed divergences is the harness timeline.

## Scope
- **Code:** `src/tsclaude/sim/paper.py`. M3-PB.1 also corrected the limitation texts in
  `src/tsclaude/runtime/paper_runtime.py` and `configs/paper.yaml`, and one docstring in
  `tests/integration/test_mvp2_paper.py`.
- **Tests:** `tests/unit/sim/test_sim_property_regressions.py`, and `tests/unit/sim/test_paper_session.py` (pinned
  journal hash).
- **No change to:** `contracts/`, `sim/journal.py`, `sim/live.py`, the event store, or the property harness.
- **Out of scope:**
  - the property harness;
  - class 2 timing;
  - a minute released with no bar at all;
  - closes made by a backfill;
  - migration of existing journals;
  - any data, holdout or live run.

## Design
The input entry of the **last bar the live clock released for a minute** carries `minute_closed: true`, in the same
atomic batch as that bar. The key is omitted otherwise, so entries without a close keep their recorded form.

| Where | Rule |
|---|---|
| `PaperTradingService.poll` | The clock releases whole minutes; the last bar of each minute in a release gets the mark |
| `PaperSession.step_bar` | Records the mark and `closed_through_ms`; refuses a bar of a minute already closed |
| `replay` | Passes the recorded mark back (an input fact, like `released_at_ms`); a bar after its recorded close is a `RestoreError`; a mark other than `true` diverges and is refused |
| `restore` | A closed last minute restarts the clock at the next minute with nothing processed, so a re-delivery is late |
| `missing_before` | A key after `last_key` in a closed minute is a passed gap, not downtime |
| `resume_after_downtime` | A backfilled bar of a closed minute is skipped, as it would be late live |

**Alternative considered:** a separate per-minute close batch (a new input kind). It would also cover a minute with no
bar, but it adds a second write that can fail between the bar and its close, a new replay path, and a journal form the
current code cannot read. The embedded mark is atomic with the bar and needs no new kind. The empty-minute case stays
open (see below).

## Compatibility
- **Legacy journals** (no marks) replay and verify unchanged and keep the previous, fail-closed reading. Pinned:
  `test_a_journal_without_close_marks_keeps_the_fail_closed_reading`, and the journal hash with the marks stripped equals
  the previous pinned value.
- **Forward:** code before this change cannot restore a journal written after it (the replayed batch lacks the mark, so
  verification fails closed with `RestoreError`, with the generic message "diverged from the journal"). No migration is
  performed or needed.
- `PAPER_VERSION` stays `paper-v1`: changing it would make every existing journal fail verification.
- **A forged mark** on the last journaled bar of an unclosed minute would pass replay. In production the event-store hash
  chain is the protection (audit N1).

## Acceptance criteria and status
| Criterion | Status |
|---|---|
| Seed 13073 passes the restore property | Under the current harness (up to `da42955`): **not met**. Class 1 is resolved for it (RUNNING, no backfill, same bars in the same order, baseline and benchmark equal), and its shadow ledger differs only because of the harness timing artifact. Escalated, not weakened. With the M3-PB.2 harness (local checkpoint after the closure review): passes, with the journal and ledgers equal to the uninterrupted run |
| Property unchanged and meaningful | Met up to `da42955`: the property and the harness are untouched. M3-PB.2 changes the harness timeline (owner-authorized) and makes the instant-restart check stricter |
| Real downtime still blocks | Met: `test_real_downtime_after_a_closed_minute_still_needs_a_backfill`, `test_real_downtime_after_the_journal_still_needs_a_backfill`, `test_a_minute_without_a_recorded_close_still_waits_for_its_bar` |
| Deterministic restart | Met for class 1; under the harness timeline, release-time dependence is still visible |
| No look-ahead or reordering | Independent causality audit, and its re-audit on `0435116`: no look-ahead, no reordering, the mark never feeds decisions |
| Legacy behaviour tested | Met (above) |
| Focused regressions and static checks | See the devlog |
| No acceptance claimed by the builder | Met |

## Review conditions and M3-PB.1
| Finding | Resolution |
|---|---|
| N-1 / F3: stale limitation texts | Corrected in `paper_runtime.py` (docstring and `LIMITATIONS`), `configs/paper.yaml` and the P5 docstring |
| N-2: step guard untested | `test_the_session_refuses_a_bar_of_a_minute_already_closed` |
| N-4: crash inside a multi-minute release | `test_a_crash_inside_a_multi_minute_release_restores_equivalently`, 5 failure points |
| N-7: wrong comment | Corrected |
| F1: harness timing | Documented here and in the two characterisation docstrings; the harness is unchanged |

## Re-review of M3-PB.1 on `0435116`
Owner-authorized, 2026-11-03. Two fresh, isolated contexts reviewed `git diff f1d5079..0435116`, read-only, with
mutants run only on copies of `src` outside the repository.

| Review | Verdict | Blocking | N-1 | N-2 | N-4 | N-7 | F1 | Class 1 kept | Mutants |
|---|---|---:|---|---|---|---|---|---|---|
| `reviewer` | PASS | 0 | verified | verified | verified | verified | verified | verified | 14 of 14 caught |
| `causality-auditor` | PASS | 0 | verified | verified | verified | verified | verified | verified | 18 of 19 caught, 1 equivalent |

- **All eight checks passed in both reviews:**
  - the tests detect their defects;
  - the assertions are intact;
  - no look-ahead or reordering;
  - legacy compatibility;
  - fail closed;
  - the limitation texts;
  - the property failure is not shown as a pass;
  - no mutation artifacts.
- **The remaining property failure is the harness artifact.** The auditor's probe repeats the property construction
  but never polls the restored service before the crash, without changing the harness:
  - 13073 / 124 and 465923499 / 162 are equal to the uninterrupted run;
  - in a sample of 300 cases, 297 are equal under both timelines, and the 3 others fail only under the harness timeline;
  - the probe was rerun by the Lead with the same result.
- **Non-blocking findings, not fixed** (no source or test change was authorized):
  - `LIMITATIONS` and `configs/paper.yaml` overstate one case in the safe direction: an unclosed last minute blocks
    only for an exposed instrument's missing bar or a whole skipped minute. They can also be read as if seed 13073
    still blocked, which is true only for legacy journals, and they do not name closes made by a backfill;
  - the N-4 test slices bar heads with a batch count, which is correct only while every batch in that scenario is a
    bar batch;
  - the property's failing example comes from the local Hypothesis database (`.hypothesis/`, git-ignored). There is
    no `@example`, so on a clean checkout or in CI the property may pass by chance. The deterministic pin is
    `test_seed_13073_still_differs_by_release_latency_only`;
  - nits: the legacy test restores without `tracker` (the result does not depend on it); the restore-close mutant is
    caught only by the two seed-13073 tests.
- **Fixed by this documentation checkpoint:** the stale "uncommitted" status texts (reviewer NB-1, audit NF-1).

| Scope | Status |
|---|---|
| M3-PB.1 implementation | Accepted as a checkpoint: PASS / PASS, 0 blocking |
| Full Part B | **Not accepted**: the property fails under the current harness; the empty minute and closes made by a backfill are open; no CI |
| M3 gate | **Not met** |

## M3-PB.2: restart-timeline property harness
Owner-authorized 2026-11-03. This is a test-harness change only: `src/` is unchanged, `paper.py` included.

| Where | Change |
|---|---|
| `paper_helpers.drive` | Optional `not_before_ms`; every poll is at `max(scheduled time, not_before_ms)`. The default `None` keeps every other caller's schedule |
| `paper_scenarios.check_restore_equivalence` | Reads the crash instant `t_crash` from the failed batch's `released_at_ms` and re-drives the restored service with `not_before_ms = t_crash + restart_delay_ms` (default delay 0) |
| Instant restart (delay 0) | Ledgers equal, as before, **and** the whole journal canonically equal to the uninterrupted one (bars, order, release times, marks, events); `AWAITING_BACKFILL` and faults are reported |
| Delayed restart (delay > 0) | **Superseded by the [B-1 remediation](#m3-pb2-b-1-remediation).** The first version re-delivered every minute after the journal as live, observed bars, downtime included, which production never does, and its checks could not see the difference |
| `test_paper_properties.py` | `@example(seed=13073, fail_batch=124)` and `@example(seed=465923499, fail_batch=162)` on the restore property (the property's own parameter names); a new property `test_delayed_restart_stays_causal` |

- **Why clamping is safe:**
  - every bar the original run released before `t_crash` is durable, because the batches of earlier polls succeeded;
  - so every bar the restored service processes was released at or after `t_crash` in the original run as well;
  - the live clock releases whole minutes in order, and a minute is due at `t_crash` only if it was due in the
    original crash poll, so release times and marks reproduce.
- **No special case for any seed:** the two examples are inputs to the unchanged property code.

## M3-PB.2: B-1 remediation
Owner-authorized 2026-11-03, after the independent review of M3-PB.2. Tests only: `src/` and the journal contract are
unchanged (`paper.py` SHA-256 still `d48500576af7…`). Uncommitted, not re-reviewed.

- **B-1 (causality-auditor, blocking):**
  - the delayed-restart harness re-delivered the downtime bars as observed `WS_LIVE` bars;
  - so entries resting from before the crash filled on downtime bars, the strategy made signals from them, and the
    backfill path never ran (687 of 687 cases ended RUNNING);
  - `restart_causality` returned `[]` for it. Reproduction: 780949414 / 169 at 322,973 ms, where
    `shadow:SIG…1:entry` filled at the minute-10 open, inside the downtime.
- **Withdrawn claims.** "1,000 causal" and "2,000 causal" (delayed column of the M3-PB.2 samples), and "nothing
  released or executable before the restart", were measured with that model and checker. They are not supported.

| Where | Change |
|---|---|
| `paper_scenarios.reconnect` | Production's restart: a new subscription delivers only bars that close at or after the restart, polled from the restart on. The first gap leaves the service `AWAITING_BACKFILL`; the runtime's own `PaperRuntime._attempt_backfill` (bar store, `validate_backfill`, completeness, `resume_after_downtime`, all unobserved) runs once, as per start in production |
| `paper_scenarios.bar_store` | The live bar store the runtime reads: every bar that existed. Tests pass incomplete or invalid stores |
| `restart_causality` | Adds: release after the bar's close; downtime bars only unobserved, before any live bar; no decision event on an unobserved bar; execution not before `max(restart, ceil_minute(released_at_ms))` nor at or before the decision; a market exit due no earlier than its release minute; entry fills after their submission, never on an unobserved bar, and never on a minute that overlaps `[crash, restart)` (at an open inside it, or at the open or intrabar in the minute the crash interrupted; as corrected by the [RR-B1 remediation](#rr-b1--br-1-remediation)); SL, TP and exit fills not before their position's entry; L-04 |
| `recovery_outcome` (new) | Never FAULTED; only existing bars after the journal; observed exactly when the bar closes at or after the restart; the strategy never sees a downtime bar; `AWAITING_BACKFILL` only with nothing processed, and never when the store is valid and complete; RUNNING processes every live bar and every downtime bar of an exposed instrument |
| Instant restart (delay 0) | Unchanged: `redeliver` and the strict comparison of the canonical journal and the ledgers |

Resting SL/TP and market exits already due resolve over the backfilled path, as SIMULATION §7 specifies. They are not
restricted to the restart.

### Four categories
| Category | Oracle | Status |
|---|---|---|
| Instant restart | Canonical journal and ledgers equal to the uninterrupted run | Holds: 13073 / 124, 465923499 / 162 and every sampled case except the empty minutes. `@example`s kept |
| Delayed restart, complete backfill | Causal, RUNNING, every needed bar processed | The harness checks it; production passes except where P-1 occurs. Examples: 465923499 / 162 / 600,000 (ten candles of downtime), 13073 / 124 / 30,000, 853832589 / 176 / 600,000. 853832589 / 176 / 30,000 resolves a take-profit over the backfill and also shows P-1 |
| Delayed restart, incomplete or invalid backfill | Blocked in `AWAITING_BACKFILL`, nothing processed | Holds: 13073 / 124 / 600,000 and 780949414 / 169 / 322,973 (a bar the feed never had); stores missing a bar, with a duplicate, or with a bar outside the requested range (that bar is also unclosed, but the runtime's not-closed rule is not isolated, reviewer NB-2) |
| Known production limitations | Reported, not fixed | Empty minute (130741648 / 8, 50553820 / 135); closes made by a backfill; **P-1** |

A P-1 report is a confirmed **production** limitation that the harness detects, not a harness defect. RR-B1 was a
harness defect: the check let some P-1 fills pass silently.

### P-1: an entry can fill in a minute the downtime overlaps
- **Mechanism:**
  - the first live bar after a mid-minute restart opened before the restart;
  - the service processes it as observed, because the source alone (`WS_LIVE`) decides that;
  - so an entry resting from before the crash fills on it while the service was down: at its open when the open lies
    in `[crash, restart)`, or at its open or intrabar when the crash and the restart fall in the same minute.
- **Contracts it contradicts:**
  - SIMULATION §7 (no new entries for the downtime), as `sim/book.py` implements it: a resting entry cannot fill on
    an unobserved bar, because "the order was not on any exchange", and it arrives fresh at the first observed open;
  - Lead decision L-02: a minute only partly covered by monitoring is not observed.
- **Production hook not used:** the runtime records `observing_since_ms` (its first poll), but nothing reads it
  (`paper_runtime.py:498, 683-684`; `paper.py:679-680`).
- **Withdrawn interpretation.** An earlier version of this brief said "an open before the crash is not P-1: the service
  saw it". The re-review showed this is wrong (RR-B1 / BR-1). The crashed service never processed that minute: its
  crash poll released the previous one. The minute's fills are journaled with its bar, after its close, so none can be
  durable before the crash. L-02 does not count the minute as observed.
- **Exact reproductions:**

| Case (seed / fail_batch / delay ms) | Result |
|---|---|
| 696831125 / 13 / 180,000 | `baseline:SIG…3:entry` and `shadow:SIG…3:entry` filled at the minute-10 open, the restart is at minute 10 + 1,499 ms |
| 853832589 / 176 / 180,000 | `shadow:SIG…3:entry` |
| 0 / 1 / 58,502 (the property's shrunk example) | `benchmark:benchmark:BTCUSDT:entry`, the restart 1 ms after the open |
| 0 / 1 / 58,501 | `[]`: the restart is at the open itself |
| 32438366 / 129 / 24,837 | `shadow:SIG…7:entry` filled intrabar in the minute the crash interrupted (crash at its open + 1,499 ms, restart 24.8 s later); the check passed it before RR-B1 |
| 891223841 / 151 / 35,763 | `shadow:SIG…12:entry`, likewise intrabar |
| 23511 / 118 / 30,000 | `shadow:SIG…8:entry` at the open of the interrupted minute |
| 853832589 / 176 / 30,000 | `shadow:SIG…3:entry` at the open of the interrupted minute; the take-profit over the backfill is not reported |
| 0 / 2 / 1 | both benchmark entries at the open of the interrupted minute, the restart 1 ms after the crash |

- **Frequency:** before RR-B1 the check found it in 34 of 400 random delayed cases (RNG 31337); the re-review found
  25 of 200 and 20 of 200. Those counts excluded the interrupted minute, so they understate P-1. No rate is claimed
  for the corrected check.
- **Effect on the property:** `test_delayed_restart_stays_causal` fails whenever it draws such a case. It is red at
  random. In the three fresh-database runs on the final harness, two failed, both shrunk to 0 / 2 / 1 (benchmark
  entries in the interrupted minute). It is not filtered, skipped or marked xfail.
- **Withdrawn statistic.** "Red in 5 of 11 fresh-database runs" mixed harness versions (reviewer NB-3), so it is
  withdrawn.
- **Decision needed:** a production change, see open decision 8. P-1 is not accepted and not deferred.

## Re-review of M3-PB.2 with the B-1 remediation
Owner-authorized, 2026-11-03. Two fresh, isolated contexts, read-only, with mutants only on copies outside the
repository. The repository was hash-verified unchanged after both.

| Review | Verdict | Blocking | Non-blocking | B-1 | P-1 |
|---|---|---:|---:|---|---|
| `reviewer` | CONDITIONAL PASS | 1 (BR-1) | 6 + 2 nits | remediation correct | reproduced |
| `causality-auditor` | FAIL | 1 (RR-B1) | 10 | resolved | confirmed, wider than documented |

- **Shared blocker RR-B1 / BR-1.** The P-1 rule judged only whether a fill's bar *opened* in `[crash, restart)`. An entry
  fill in the minute the crash interrupted, when the restart falls in the same minute, passed silently.
  - Reproductions: 32438366 / 129 / 24,837 (stop entry, intrabar) and 891223841 / 151 / 35,763 (limit entry, intrabar).
  - The reviewer showed this came from narrowing the first draft of the rule.
- **Verified by both:**
  - the B-1 checks;
  - the instant-restart oracle;
  - no earlier test weakened;
  - 358 tests in `tests/unit/sim`, 50 paper integration tests;
  - 36 of 37 of the implementer's mutants caught, and the 37th (`polled_from_crash`) really equivalent.
- **Other findings, not remediated** (no authorization):
  - reviewer NB-1: mutants that survive (the first live minute in the live-lost check; the release-time rule; the
    journal compared on heads only; `eligible` without `closed_through_ms`);
  - NB-2: the "unclosed" store is an out-of-range bar;
  - NB-4: completeness is stricter than production's, which is the safe direction;
  - NB-5: the B-1 repro processes nothing under the new model;
  - NB-6 and audit N-7: a share of delayed draws check little or nothing (about 7% restart after the last bar);
  - audit N-1: the startup-backfill wait between subscription and first poll is not modelled;
  - audit N-2 and N-3: `recovery_outcome` misjudges an identity-invalid store and raises on a failing store read;
  - audit N-4: the instant oracle is an idealized determinism oracle, not a reachable production restart;
  - audit N-5: more surviving mutants;
  - audit N-6: `not_before_ms` in `reconnect` is dead code;
  - audit N-8: three definitions of an observed first minute;
  - audit N-9: `decision_latency_ms > 0` is never exercised;
  - audit N-10: the clock base of the P-1 proposal.
- **Evaluation of the P-1 proposal** (`observed = source observed AND open >= observing_since_ms`, not implemented):
  - mostly compliant with L-02 and SIMULATION §7;
  - safe for replay and for legacy journals, since `observed` is journaled per bar;
  - not sufficient as written:
    - it breaks the reference strategy's warm-up continuity at every start not on a minute open (`trade_from_ms`,
      derived from source, not executed);
    - its anchor (market-data start or first paper poll) is undecided;
    - it compares a local clock with exchange time;
    - it labels the first minute of every session as downtime;
    - the instant oracle would test a configuration production never runs.

## RR-B1 / BR-1 remediation
Owner-authorized 2026-11-03. Tests and documentation only; `src/` and the journal contract are unchanged.

- **Contract basis:**
  - L-02: a minute only partly covered by monitoring is not observed.
  - SIMULATION §7 as `book.py:1052-1054` applies it: a resting entry was not on any exchange during the downtime.
  - A bar's fills are journaled in the bar's own batch, at its release after its close (`PaperSession.step_bar`). So
    a fill can be durable before the crash only on a minute that closed before it.
  - **No conflict to escalate:** the whole-candle reading never invalidates a durably recorded pre-crash fill. Within
    the interrupted minute, the journal holds no evidence of pre-crash execution, not even for a fill at the open.
- **Rule:** after the restore, an entry fill on a bar whose minute overlaps `[crash, restart)` is reported:
  - "filled at an open during the downtime" when its open lies in `[crash, restart)`;
  - "filled in the minute the crash interrupted" when `open < crash < open + 60 s`, at its open or intrabar.
  Fills journaled before the restore are not judged.
- **Tests:**
  - the two intrabar reproductions pinned;
  - boundary checks ±1 ms on both bounds and on the end of the interrupted minute;
  - a test that every durable fill is on a minute closed before the crash and is never judged;
  - re-pinned expectations: 23511 / 118 / 30,000, 853832589 / 176 / 30,000 and 0 / 2 / 1 now report P-1. Before, they
    were wrongly treated as valid.
- **Cases kept unchanged:** 696831125 / 13 / 180,000, 853832589 / 176 / 180,000, 0 / 1 / 58,502 and 0 / 1 / 58,501.
- **Unchanged:** the B-1 recovery model, instant-restart equivalence, the journal and ledger comparisons, and the
  `@example`s. Evidence: [devlog](../devlog/2026-11-03-m3-pb2-restart-timeline-harness.md#rr-b1--br-1-remediation).

## Closure review of RR-B1 / BR-1
Owner-authorized 2026-11-03. Two fresh, isolated contexts, read-only, narrowly scoped to RR-B1, with mutants only on
copies outside the repository. The repository was hash-verified unchanged afterwards.

| Review | RR-B1 | B-1 preserved | Checkpoint | New blocking | Own mutants |
|---|---|---|---|---:|---|
| `reviewer` | **PASS** | **PASS** | acceptable | 0 | 13 of 14 caught, 1 equivalent |
| `causality-auditor` | **PASS** | **PASS** | acceptable | 0 | 11 of 11 caught |

- **Verified by both:**
  - the rule reports every post-restore entry fill on a minute overlapping `[crash, restart)`, at the open and
    intrabar, and no longer exempts the minute the crash interrupted;
  - it never judges a fill journaled before the restore;
  - it uses no intrabar timestamp;
  - for `restart >= crash` its two branches together equal "not observed under L-02" for the runs before the crash and
    after the restart (`ObservationIndex`). The auditor's ±1 ms grid of 273 windows had no disagreement, and both
    random samples (150 cases each) had no missed or false report;
  - the durability argument holds in code. Each released bar is one batch with only its own events. Fills come only
    from stepping that bar, and their `bar_open_ms` is its open. A failed append leaves the service FAULTED, so no
    later bar is processed. `resume_after_downtime` steps unobserved bars, and `skip_to` emits nothing;
  - the B-1 model is byte-identical to the re-reviewed version (`reconnect`, `bar_store`, `recovery_outcome`,
    `redeliver`, `paper_helpers.py`);
  - instant-restart equivalence and the canonical journal and ledger comparisons are unchanged, and the `@example`s
    are kept;
  - no assertion was weakened: with the old rule, 8 tests fail.
- **Non-blocking findings, not remediated:**
  - **Clock skew (audit NB-1).** `LiveClock.release` (`sim/live.py:150-152`) releases a complete minute without
    checking `now >= close`. In production, a local clock behind exchange time could make a durable fill sit on a
    minute the local crash instant interrupted. The harness cannot produce this (polls at close + 1.5 s). It is a
    separate production concern to track.
  - **Delay 0 versus 1 ms (audit NB-2).** The instant oracle accepts the interrupted-minute fill that a 1 ms delay
    reports. This is intended: delay 0 models continuous observation. A P-1 fix must not be judged against it.
  - **Production stricter than L-02 (audit NB-3).** The bars of the failed batch closed before the crash, yet production
    re-processes them unobserved from the backfill. No invalid execution results.
  - **Coverage:**
    - exact crash offsets are reachable only by re-judging real journals, because the harness always crashes at
      open + 1,499 ms (reviewer NB-2, audit NB-4);
    - two windows of the boundary test are inverted, although they still discriminate;
    - crashes on non-bar batches are not exercised (audit NB-5).
  - **Documentation (reviewer NB-1, NB-3):**
    - the `restart_causality` row above stated the old rule (corrected here);
    - the property's shrunk example varies between runs (0 / 2 / 1, 0 / 199 / 1).
- **Earlier open items are not reopened and remain open:**
  - P-1 and the gaps in its proposal (warm-up continuity, anchor, clock base, labels, instant oracle);
  - the three definitions of an observed first minute (audit N-8);
  - the other non-blocking findings of the re-review listed above.

| Scope | Status |
|---|---|
| RR-B1 / BR-1 | **PASS** (both reviews) |
| B-1 preservation | **PASS** (both reviews) |
| M3-PB.2 implementation checkpoint | **Acceptable**: the harness and the checks represent production recovery and detect P-1; committed locally as a checkpoint, not pushed, not merged |
| Delayed-restart property | **Not accepted**: red at random on P-1 (failed in all three of the reviewers' fresh-database runs) |
| Empty-minute restoration; closes made by a backfill | Open |
| Cross-platform CI | Pending |
| Full Part B | **Not accepted** |
| M3 gate | **Not met** |

## P-1 implementation
Owner-authorized 2026-11-03 (D-19 L-02 clarification, D-20 harness rule). **Implemented; independent review
CONDITIONAL PASS / CONDITIONAL PASS; local checkpoint pending hardening, not unconditionally accepted.** Evidence: [P-1 devlog](../devlog/2026-11-03-m3-p1-observation-anchor.md).

- **Root cause.** `PaperTradingService._observed` decided observed from the bar source only; the runtime's
  `observing_since_ms` was never read. A resting entry filled on the first live bar after a restart even when that
  minute was only partly observed, and so did the minute the crash interrupted and the minutes queued during the
  startup wait (audit N-1).
- **Correction.**
  - `sim/paper.py`: `observation_anchor_ms` and `clock_uncertainty_ms` (pure functions); a gated service observes a bar
    only if its source qualifies and it opens at or after `observing_from_ms` (`observe_from`, set once); gated
    without an anchor nothing is observed. An ungated service keeps continuous observation (tests, the instant oracle).
  - `runtime/paper_runtime.py`: the service is always gated; the anchor is fixed at the first poll whose clock round is
    trustworthy, before that poll processes anything; otherwise every bar is processed unobserved and the next poll
    tries again. `bracket_reference` is built at the first observed bar (`DeferredReference`) with
    `trade_from_ms = A` and warmed up from the live bar store with every bar closed before A.
  - `quality/clock.py`, `marketdata/models.py`, `marketdata/service.py`: each clock round keeps its largest round
    trip, published in `MarketSnapshot.clock_max_rtt_ms` with the offset's `as_of_ms`.
  - Not changed: `engine.py`, `book.py`, `live_adapter.py`, the `LiveStrategy` protocol, the journal schema,
    `PAPER_VERSION`, `contracts/`.
- **Harness (D-20).** `reconnect` sets the anchor with the same function (offset 0, round trip 0: the harness clock is
  the exchange clock); `recovery_outcome` expects observed iff `open >= A` and reports a service whose anchor
  differs. `reconnect(..., gated=False)` keeps the pre-fix service so that the checks can be shown to still report P-1.
  `restart_causality` is unchanged. The instant oracle (`redeliver`) sets no anchor.
- **Result.** Every pinned P-1 counterexample now gives `[]` (0 / 1 / 58,502; 0 / 2 / 1; 696831125 / 13 / 180,000;
  853832589 / 176 / 180,000 and 30,000; 23511 / 118 / 30,000; 32438366 / 129 / 24,837; 891223841 / 151 / 35,763), and
  the boundary cases stay `[]`. On 400 random delayed restarts (RNG 31337) the pre-fix service fills a P-1 entry in 32
  cases; the fixed one has no problem in any.
- **Mutants.** 24 caught, 2 equivalent, 1 sound survivor (the service publishing the median round trip, also a
  proven bound); details in the devlog.
- **New finding (not P-1, not remediated).** A backlog queued during the startup wait releases its first minute
  before that minute's second bar is offered (one poll per subscription update), so the minute is processed
  incomplete, gated or not. It is now processed unobserved.
- **Independent review (2026-11-03): CONDITIONAL PASS / CONDITIONAL PASS** (`reviewer`, `causality-auditor`, 0
  blocking; both: acceptable as a local checkpoint; M3-PB.2 preserved). Open, all introduced with P-1:
  - **F-1:** clock drift or step between the round and the poll is not bounded or detected; a started minute can
    count as observed (by milliseconds; counterexamples in the devlog).
  - **F-2:** the producer of `clock_max_rtt_ms` is not pinned (publishing 0 survives).
  - **F-3:** a negative round trip stops the runtime as `CONSUMER_DIED`, and the round trip's provenance is unchecked.
  - **N-1:** the Paper page says "No live strategy is selected" while the reference is pending.
  - **N-2:** the observation state and why no anchor exists are not visible in the API or as a problem.
  - Also N-4, N-5 and nits ([devlog](../devlog/2026-11-03-m3-p1-observation-anchor.md#independent-review-2026-11-03)).

  The startup backlog defect predates P-1. It loses a late bar of an exposed instrument and changes exit timing
  (non-blocking for P-1, blocking for Part B).
- **Not accepted:** P-1 unconditionally (pending hardening), the delayed property, full Part B, the M3 gate.

## Open decisions for the owner
1. **Harness timing (F1).** Decided: the harness never polls the restored service before the crash instant
   (M3-PB.2; B-1 and RR-B1 PASS in the independent reviews; committed as a local checkpoint).
2. **Seed 13073 criterion.** It passes under the M3-PB.2 harness; it cannot pass under the harness up to `da42955`.
3. **Empty minute.** A minute released with no bar leaves no entry to mark (seed 130741648, fail_batch 8). Covering it
   needs a new input kind, which is a journal-format decision.
4. **Closes made by a backfill** (`skip_to` after `resume_after_downtime`) are not journaled (reviewer N-5, audit N2):
   after a crash that follows a backfill, a late bar of an instrument the backfill did not include could be processed.
   That is causal, but not equivalent.
5. **Cross-platform CI.** This work has no CI run and no Linux run. The pinned hashes are labelled as recorded on
   Linux and need re-confirming there. Pushing needs owner approval (D-07).
6. **Pinning the property examples.** Done in M3-PB.2: both examples run on every property run, independent of the
   local Hypothesis database, and as deterministic unit tests.
7. **Stricter property and the empty minute.** The instant-restart check now also compares the journal and reports
   `AWAITING_BACKFILL`, so a random empty-minute case fails the property even when the ledgers happen to be equal
   (for example 50553820 / 135, which the previous check passed). This is the open empty-minute limitation, not a harness
   defect; the property can fail on it in CI until item 3 is decided.
8. **P-1.** Decided by the owner on 2026-11-03 (D-19, D-20); implemented, independent review CONDITIONAL PASS /
   CONDITIONAL PASS, local checkpoint pending hardening ([P-1 implementation](#p-1-implementation)). The text below is the state before that decision.
   Production fills a resting entry in a minute the downtime overlaps. Needs a contract-based decision and a
   production change. A design proposal is in the
   [M3-PB.2 devlog](../devlog/2026-11-03-m3-pb2-restart-timeline-harness.md#p-1-production-limitation-open), and the
   re-review lists its gaps above.
   - The anchor (market-data start or first paper poll), the clock base, the reference strategy's warm-up and the
     instant oracle's definition must be decided with it.
   - The delayed property fails on P-1 at random until then. A deterministic `@example` (for example `seed=0,
     fail_batch=2, delay_ms=1`) would make that failure show on every run; this is proposed, not added.
