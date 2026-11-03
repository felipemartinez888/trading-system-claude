# Task brief M3-PB: paper restart equivalence, Part B class 1

- **Status:** local checkpoint `0435116944032b95ab21456f8c669c3d8af357b4` on `task/m3-partb` (base `f1d5079`), not
  pushed and not merged.
  - The first independent reviews of 2026-11-03 gave CONDITIONAL PASS for the class 1 remediation, from both
    `reviewer` and `causality-auditor`.
  - The independent re-review of M3-PB.1 on `0435116` gave **PASS / PASS**, 0 blocking (see
    [Re-review of M3-PB.1](#re-review-of-m3-pb1-on-0435116)). The M3-PB.1 implementation is accepted as a checkpoint.
  - **Full Part B and the M3 gate are not accepted.**
  - Evidence: [devlog](../devlog/2026-11-03-m3-partb-close-marks.md).
- **Authority:** owner authorization of 2026-11-03 for Part B class 1, and for the follow-up M3-PB.1.
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
| Seed 13073 passes the restore property | **Not met.** Class 1 is resolved for it (RUNNING, no backfill, same bars in the same order, baseline and benchmark equal). Its shadow ledger still differs, because of the harness timing artifact. Escalated, not weakened |
| Property unchanged and meaningful | Met: `tests/property/sim/test_paper_properties.py` and the harness are untouched |
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

## Open decisions for the owner
1. **Harness timing (F1).** Either poll the restored service never before the crash instant, which needs separate
   authorization, or keep the current harness and record 13073/124 as a harness artifact.
2. **Seed 13073 criterion.** It cannot pass under the current harness.
3. **Empty minute.** A minute released with no bar leaves no entry to mark (seed 130741648, fail_batch 8). Covering it
   needs a new input kind, which is a journal-format decision.
4. **Closes made by a backfill** (`skip_to` after `resume_after_downtime`) are not journaled (reviewer N-5, audit N2):
   after a crash that follows a backfill, a late bar of an instrument the backfill did not include could be processed.
   That is causal, but not equivalent.
5. **Cross-platform CI.** This work has no CI run and no Linux run. The pinned hashes are labelled as recorded on
   Linux and need re-confirming there. Pushing needs owner approval (D-07).
6. **Pinning the property examples.** If the harness change is approved, adding `@example(13073, 124)` and
   `@example(465923499, 162)` to the property would make these cases independent of the local Hypothesis database.
