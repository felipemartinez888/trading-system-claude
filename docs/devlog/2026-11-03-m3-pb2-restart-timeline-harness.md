# 2026-11-03: M3-PB.2, restart-timeline property harness (audit F1)

**Status:** implemented on `task/m3-partb` on top of `da42955`.
- The independent review gave `reviewer` CONDITIONAL PASS and `causality-auditor` FAIL (blocking B-1). The
  [B-1 remediation](#b-1-remediation) is implemented.
- Its independent re-review accepted the B-1 recovery model and found one shared blocker, RR-B1 / BR-1 (`reviewer`
  CONDITIONAL PASS, `causality-auditor` FAIL; see the [brief](../tasks/M3-PB.md#re-review-of-m3-pb2-with-the-b-1-remediation)).
  The [RR-B1 remediation](#rr-b1--br-1-remediation) is implemented.
- The [closure review](#closure-review-and-checkpoint) gave RR-B1 **PASS** and B-1 preservation **PASS** from both
  reviewers. M3-PB.2 is committed as a **local implementation checkpoint** (not pushed, not merged).
- **Not accepted:** the delayed-restart property (P-1 is open), full Part B and the M3 gate. Brief:
  [M3-PB](../tasks/M3-PB.md#m3-pb2-restart-timeline-property-harness).

The sections from "Preflight" to "Remaining" describe the first implementation. Its delayed-restart parts (model,
checks, tests, samples and mutants) are **superseded** by the B-1 remediation; its instant-restart parts still hold.

## Preflight
- Repository `trading-system-claude`, branch `task/m3-partb`, HEAD `da429557b9c1f52cf9f2b9c8d8f0b0a4cba0c977`, with no
  tracked changes.
- The reviewed source is unchanged: `src/tsclaude/sim/paper.py` SHA-256 is still `d48500576af7…`, and there is no
  `src` diff against `0435116`.
- The E-4(b) worktree `tsc-e4b` (branch `task/m2-e4b`, 8 uncommitted entries) was not touched.
- No dataset, holdout, paper session, journal on disk or network was used.

## Root cause
- `check_restore_equivalence` re-drove the restored service with `drive` on the original timeline, from the restored
  minute, so its first polls were at wall times before the poll whose write failed.
- The restored clock, with the closed minute known from the journal, then released the next minute at that earlier
  time. Release time bounds execution (`not_before_ms` → `earliest_exec_ms`), so fills moved.
- No real restart can poll before the crash. The divergence was a property of the harness timeline, not of restore or
  the simulator.

## Change (tests only)
| File | Change |
|---|---|
| `tests/unit/sim/paper_helpers.py` | `drive(..., not_before_ms=None)`: every poll at `max(scheduled, not_before_ms)`; `None` keeps the schedule of every other caller |
| `tests/unit/sim/paper_scenarios.py` | `check_restore_equivalence(seed, fail_batch, restart_delay_ms=0)`: `t_crash` from the failed batch's `released_at_ms`; restored polls never before `t_crash + delay`; instant restart compares ledgers **and** the canonical journal and reports `AWAITING_BACKFILL`; a delayed restart is checked by the new `restart_causality` |
| `tests/property/sim/test_paper_properties.py` | `@example(seed=13073, fail_batch=124)`, `@example(seed=465923499, fail_batch=162)`; new property `test_delayed_restart_stays_causal` (delay 1 ms to 10 min) |
| `tests/unit/sim/test_sim_property_regressions.py` | 13 new tests (below); module and characterisation docstrings updated |

- **One existing assertion changed:** the last line of `test_seed_13073_still_differs_by_release_latency_only`. It was
  `check_restore_equivalence(13073, 124) == ["ledgers differ after restore at batch 49"]`, which pinned the property's
  failure under the pre-crash timeline. It is now `== []`.
  - The rest of that test still drives the pre-crash timeline and still asserts the shadow divergence and the 1.5 s
    vs 61.5 s release latencies.
  - `test_release_latency_changes_the_shadow_book_across_a_restart` (465923499) is unchanged apart from its docstring.
- **Invariants examined before choosing the clamp:**
  - Every bar released before `t_crash` is durable: earlier polls' batches succeeded. So every re-processed bar was
    released at or after `t_crash` in the original run as well.
  - The live clock releases whole minutes in order. A minute due at `t_crash` in the restored service was due in the
    original crash poll, and one not due then is not due now. Release times and close marks therefore reproduce, and
    the instant-restart journal comparison confirms it.
  - Re-delivered durable bars stay late, and are never processed twice (journal comparison, `restart_causality`).
  - Polls are nondecreasing: the max with a constant keeps the original increasing schedule.
  - Snapshots and bar offers carry no wall time, so only the polls are clamped.
- **No special case for any seed:** the two examples are only inputs to the unchanged property code.

## New tests (`test_sim_property_regressions.py`)
- `test_the_crash_instant_is_the_poll_of_the_failed_write`:
  - 13073 / 124: batch 49 at minute 28 + 1,499 ms;
  - 465923499 / 162: batch 14 at minute 9 + 1,499 ms.
- `test_an_instant_restart_reproduces_the_uninterrupted_journal`: both examples.
- `test_the_pre_crash_timeline_is_what_broke_equivalence`: both examples.
  - The original timeline diverges and `restart_causality` reports a release before the crash.
  - Held at `t_crash`, the run is causal and the ledgers are equal.
- `test_a_delayed_restart_stays_causal`: 3 cases × delays of 30 s and 10 min.
- `test_the_empty_minute_still_fails_the_instant_restart_property`: 130741648 / 8 keeps failing the strict check (exact
  messages pinned). The delayed check is causal there.
- `test_restart_causality_reports_each_violation`: on a real journal, each rule is detected:
  - a release before the restart;
  - an entry executable before it;
  - a bar processed twice;
  - a bar of a closed minute;
  - an entry filled before its earliest execution (forged).

## Validation (sequential, one process each, `-p no:cacheprovider`)
| Batch | Result |
|---|---|
| `tests/unit/sim/test_sim_property_regressions.py` | 38 passed (25 before) |
| `tests/unit/sim` | 326 passed (313 before) |
| `tests/property/sim`, repository `.hypothesis` database | 10 passed (before: 8 passed, 1 failed on 13073 / 124) |
| `tests/property/sim`, empty database (`HYPOTHESIS_STORAGE_DIRECTORY` in the scratchpad) | 10 passed |
| `test_mvp2_paper.py`, `test_mvp2_paper_api.py`, `test_mvp21_paper_hardening.py` | 39 passed |
| `test_mvp3_reference_paper.py`, `test_mvp4_paper_api.py`, `test_runtime_misc.py`, `test_sessions.py`, `tests/unit/risk` | 92 passed |
| `ruff check --no-cache src tests`, `ruff format --no-cache --check src tests` | All checks passed; 214 files formatted |
| `mypy src` (fresh cache) | No issues in 116 files |

### Mutants
12 harness mutants were run, each on a disposable copy of `tests/` in the scratchpad, against the regression file and
the property file. **12 of 12 were caught.** The repository's harness files were hash-verified unchanged afterwards.

The mutants:
- no clamp;
- the check not passing the restart time;
- the restart a minute early;
- `t_crash` = 0;
- no release check;
- no clock-order check;
- no closed-minute check;
- no entry-exec check;
- no fill check;
- no journal equality;
- no ledger equality;
- no `AWAITING_BACKFILL` report.

### Samples
Fixed-seed random cases. Each case is checked by the harness before the change, and by the new harness at delay 0
and at a random delay of 1 ms to 10 min.

| Sample | Before | After, instant | After, delayed |
|---|---|---|---|
| RNG 20261103, 1,000 cases | 998 equal, 2 differ (122878939 / 45, 800001546 / 15) | 1,000 equal (ledgers and journal) | ~~1,000 causal~~ withdrawn |
| RNG 777, 1,000 cases | 993 equal, 7 differ | 999 equal, 1 `AWAITING_BACKFILL` | ~~1,000 causal~~ withdrawn |

- **Correction (B-1):** the "delayed" column was measured with a model that re-delivered downtime bars as observed
  live bars and with a checker that could not see it. It does not show that delayed restarts are causal.

- **All 9 "before" divergences are equal after.**
- **The one remaining case,** 50553820 / 135, is an empty minute (minute 34). It is `AWAITING_BACKFILL` under both
  timelines. The previous check passed it only because the ledgers happened to be equal, and the stricter check now
  reports it.

## Remaining
- **Production limitations, not harness defects:**
  - **Empty-minute recovery** fails closed. The property can now fail on such a random case in CI.
  - **Closes made by a backfill** are not journaled.
- **Not done:**
  - an independent review of M3-PB.2;
  - a commit;
  - CI and a Linux run.
- **Earlier non-blocking review findings,** unchanged:
  - the `LIMITATIONS` wording;
  - the N-4 test slicing by batch count.

## B-1 remediation
Owner-authorized 2026-11-03, after the independent review. Tests only; `src/` and the journal contract are unchanged
(`paper.py` SHA-256 `d48500576af7…`; every `src` file hash-verified before and after the mutants). Design and the
four categories: [brief](../tasks/M3-PB.md#m3-pb2-b-1-remediation).

### Root cause
- `check_restore_equivalence` with a delay re-delivered every minute after the journal through `drive`, as observed
  `WS_LIVE` bars, downtime included.
- Production never does that. A new subscription delivers only bars that close after the restart; the gap leaves the
  service `AWAITING_BACKFILL`; the runtime backfills once per start, unobserved.
- `restart_causality` checked release times and entry execution only. It could not see fills on downtime bars,
  signals from them, or a recovery that ended in the wrong state.

### Change
| File | Change |
|---|---|
| `tests/unit/sim/paper_scenarios.py` | `Restart` (the restored service and its timestamps); `restore_after_crash`; `bar_store`; `redeliver` (instant, unchanged semantics); `reconnect` (production's restart, with the real `PaperRuntime._attempt_backfill`); `restart_causality` extended (bars, decisions, execution, entry and bracket fills, P-1 window `[down_from_ms, live_from_ms)`); new `recovery_outcome` |
| `tests/unit/sim/test_sim_property_regressions.py` | Delayed tests rewritten on the production model, with exact expected results; adversarial and boundary tests (below); detection tests for every rule |
| `tests/property/sim/test_paper_properties.py` | Unchanged since the first M3-PB.2 version: both properties strict, `@example`s kept |

- **Instant restart:** unchanged. 13073 / 124 and 465923499 / 162 give `[]`; 130741648 / 8 keeps its three messages;
  50553820 / 135 keeps "journal differs" and `AWAITING_BACKFILL`.
- **No case removed, filtered, skipped or marked xfail.** Cases that show P-1 are pinned with their exact messages.

### Adversarial scenarios (all pinned)
| Scenario | Case | Result |
|---|---|---|
| The old live re-delivery | 780949414 / 169 / 322,973 | The checker reports the downtime fill; the new model has no fill in `[crash, restart)` |
| Pending entry, ten minutes of downtime | 696831125 / 13 / 600,000 | No fill on a downtime bar; expiries tagged downtime |
| Downtime overlapping the execution window, P-1 | 696831125 / 13 / 180,000 | Two entry fills at the minute-10 open reported |
| Multi-candle downtime, complete backfill | 465923499 / 162 / 600,000 | RUNNING, causal |
| Bracket over the backfill | 853832589 / 176 / 30,000 | Only a take-profit on unobserved bars; after RR-B1 the check also reports the P-1 entry of the interrupted minute, never the take-profit |
| 30 s and 10 min delays | 13073 / 124, 465923499 / 162, 23511 / 118 | `[]`; 13073 / 124 / 600,000 blocked (a bar the feed never had); after RR-B1, 23511 / 118 / 30,000 reports P-1 |
| Minute and settle boundaries | 465923499 / 162, delays 1, 58,499 to 58,501, 59,999 to 60,001, 88,500, 88,501, 89,999 to 90,001 ms | First live minute 9 up to 58,500 ms, 10 after; all causal |
| P-1 window ±1 ms | 696831125 / 13 / 180,000 | Reported for an open in `[down, live)`, not at the restart. **Superseded by RR-B1:** this version also exempted every fill in the minute the crash interrupted, which was wrong |
| Shrunk property example | 0 / 1 / 58,501 and 58,502 | `[]`, then the P-1 benchmark fill |
| Incomplete or invalid backfill | stores missing a BTC bar, with a duplicate, with a bar outside the requested range (also unclosed; the not-closed rule is not isolated, reviewer NB-2) | `AWAITING_BACKFILL`, nothing processed |
| Empty minute | 130741648 / 8 / 600,000 | `AWAITING_BACKFILL`, nothing processed |

### P-1 (production limitation, open)
An entry resting from before the crash fills on the first live bar when that bar's minute overlaps the downtime: at an
open inside it, or anywhere in the minute the crash interrupted (corrected by RR-B1). The service decides observed from
the bar source only; `observing_since_ms` exists in the runtime but is not used. Exact cases and the contracts
involved are in the [brief](../tasks/M3-PB.md#p-1-an-entry-can-fill-in-a-minute-the-downtime-overlaps). The re-review's
evaluation of the proposal below (gaps: warm-up continuity, anchor, clock base, labels, instant oracle) is in the
[brief](../tasks/M3-PB.md#re-review-of-m3-pb2-with-the-b-1-remediation).

**Design proposal (analysis only, not implemented):**
- **Boundary:** a bar is observed when its source is observed **and** `open_time_ms >= observing_since_ms` of the
  current run (L-02: start ≤ open). A restart exactly at an open observes that minute.
- **Effect:** the partly monitored first bar takes the existing unobserved path. The strategy is not called; resting
  entries cannot fill and may expire as downtime; SL/TP and due exits resolve on it, tagged `resolved_from_backfill`.
- **Service API:** the runtime passes the boundary at its first poll. Without it (direct service use, the instant
  oracle) behaviour is unchanged.
- **Compatibility:** `observed` is already journaled per bar and replay uses the recorded value, so existing journals
  replay unchanged and no contract change is needed.
- **Files:** `src/tsclaude/sim/paper.py`, `src/tsclaude/runtime/paper_runtime.py`, `docs/SIMULATION.md` §7, the
  limitation texts, and the harness's observed criterion (an owner-approved spec change).

### Validation (sequential, `-p no:cacheprovider`, Hypothesis databases in the scratchpad)
| Batch | Result |
|---|---|
| `tests/unit/sim/test_sim_property_regressions.py` | 70 passed |
| `tests/unit/sim` | 358 passed |
| Paper integration (`test_mvp2_paper.py`, `test_mvp2_paper_api.py`, `test_mvp21_paper_hardening.py`, `test_mvp3_reference_paper.py`, `test_mvp4_paper_api.py`) | 50 passed, 1 warning |
| `tests/property/sim`, fresh databases | Red at random on P-1 only (shrunk to 0 / 1 / 58,502). The earlier "5 of 11 runs" mixed harness versions (reviewer NB-3) and is withdrawn |
| `ruff check --no-cache src tests`, `ruff format --no-cache --check src tests` | All checks passed; 214 files formatted |
| `mypy src` (fresh cache) | No issues in 116 files |

The full suite was not run (not authorized).

### Mutants
37 mutants of the harness and the checks, each on a disposable copy of `tests/` in the scratchpad, judged by the
deterministic regression file (green on an unmutated copy; the property file is red on P-1, so it cannot judge).
The repository files were hash-verified unchanged after each batch.
- **36 caught.** Among them:
  - downtime delivered live, no backfill attempt, backfill before the bars closed, restart at the crash, no clamp;
  - each `restart_causality` rule, including the P-1 rule and its old false-positive form (`at < live_from_ms`);
  - each `recovery_outcome` rule;
  - the instant journal and ledger equality, and the `AWAITING_BACKFILL` report.
- **3 first survived and are now caught:** no recovery outcome in the delayed check, no "not a bar after the
  journal" check, no observed mismatch check. One test was added and one extended.
- **1 equivalent:** polls in `reconnect` from the crash instead of the restart. Every live minute closes at or after
  the restart, so its poll at close + 1.5 s is never clamped.

### Remaining (at the end of the B-1 remediation)
- P-1 needs an owner decision and a production change.
- Empty-minute recovery and closes made by a backfill are open.
- No commit, no independent re-review, no CI or Linux run.
- Proposed, not added: `@example(seed=0, fail_batch=1, delay_ms=58502)`, so that the P-1 failure shows on every run.

## RR-B1 / BR-1 remediation
Owner-authorized 2026-11-03, after the independent re-review. Tests and documentation only. `src/` and the journal
contract are unchanged; `src` was hash-verified after the mutants.

### Root cause
- `restart_causality` reported an entry fill after the restore only when the fill bar's open lay in `[crash, restart)`.
- The minute the crash interrupted (`open < crash`) was exempt as a whole, on the premise that the service saw its
  open. That premise is false:
  - the crash poll released the previous minute, so the crashed service never processed this one;
  - a bar's fills are journaled with the bar, after its close, so no fill on that minute can be durable before the
    crash;
  - L-02 does not count the minute as observed.
- The narrowing came from an earlier fix: the first draft of the rule (`open < restart`) reported fills at the open of
  the interrupted minute (0 / 2 / 1, 23511 / 118 / 30,000), and those reports were wrongly taken for false
  positives. They were correct. The re-review found it.

### Correction
- An entry fill after the restore is reported when its bar's minute overlaps `[crash, restart)`:
  - "filled at an open during the downtime" when `crash <= open < restart` (unchanged);
  - "filled in the minute the crash interrupted" when `open < crash < open + 60 s`, at the open or intrabar (new).
- Fills journaled before the restore are not judged.
- No intrabar timestamp is inferred: the rule is whole-candle, as L-02 is.

| Case | Before | After |
|---|---|---|
| 32438366 / 129 / 24,837 (intrabar stop entry) | `[]` | `shadow:SIG…7:entry filled in the minute the crash interrupted` |
| 891223841 / 151 / 35,763 (intrabar limit entry) | `[]` | `shadow:SIG…12:entry …` likewise |
| 23511 / 118 / 30,000 (at the open) | `[]` | `shadow:SIG…8:entry …` |
| 853832589 / 176 / 30,000 (at the open) | `[]` | `shadow:SIG…3:entry …`; the take-profit is not reported |
| 0 / 2 / 1 (at the open, restart 1 ms after the crash) | `[]` | both benchmark entries |
| 696831125 / 13 / 180,000; 853832589 / 176 / 180,000; 0 / 1 / 58,502 | P-1 at an open during the downtime | unchanged |
| 0 / 1 / 58,500 and 58,501; 13073 / 124 and 465923499 / 162 at 30 s and 10 min; 23511 / 118, 853832589 / 176 and 696831125 / 13 at 10 min; the twelve boundary delays of 465923499 / 162 | `[]` | `[]` |

### Tests (`test_sim_property_regressions.py`)
- `test_an_intrabar_entry_fill_in_the_minute_the_crash_interrupted_is_reported`: the two new cases. Each asserts the
  fill (intrabar, observed, in the interrupted minute) and the exact message.
- `test_the_downtime_fill_check_covers_each_minute_the_downtime_overlaps`:
  - replaces the earlier window test, whose assertion "the open came before the crash: it was observed" was the
    defect;
  - ±1 ms checks: crash 1 ms after the open, crash at the bar's last millisecond, the minute closed before the crash,
    crash at the open, restart at the open.
- `test_a_fill_journaled_before_the_crash_is_never_judged_as_downtime`: every durable entry fill is on a minute closed
  before the crash; the check does not judge it; the same fill judged as post-restore is reported.
- **Re-pinned with exact messages:** `test_a_delayed_restart_stays_causal` (23511 / 118 / 30,000, 853832589 / 176 /
  30,000 and 0 / 2 / 1 now expect P-1; 23511 / 118 / 600,000 and 853832589 / 176 / 600,000 still expect `[]`) and
  `test_a_bracket_resolves_over_the_backfill`.
- The property module docstring no longer states the withdrawn delayed scope (reviewer Nit-1).

### Validation (focused, `-p no:cacheprovider`)
| Batch | Result |
|---|---|
| The two new intrabar cases | 2 passed |
| P-1 and its boundaries (delayed parametrization, window, durable, P-1 characterisation, pending entry, bracket) | 18 passed |
| Instant-restart regressions (crash instant, instant journal, pre-crash timeline, empty minutes, 13073, release latency) | 14 passed |
| Other delayed-restart tests (recoveries, old model, minute and settle boundaries, backfill stores, outcome and checker detection) | 23 passed |
| The whole regression file (these groups overlap; this is the total) | 74 passed |
| `tests/unit/sim` (includes the regression file) | 362 passed |
| `ruff check --no-cache src tests`; `ruff format --no-cache --check src tests` | All checks passed; 214 files formatted |
| `mypy src` (fresh cache) | No issues in 116 files |
| `tests/property/sim`, 3 runs, fresh database each, final harness | 2 failed, 1 passed. Each failure is only `test_delayed_restart_stays_causal`, shrunk to 0 / 2 / 1 (P-1 in the interrupted minute). Observations, not a rate |

Paper integration and the full suite were not run (not in the authorized list; `src` is unchanged).

### Mutants (disposable copies of `tests/`, judged by the regression file)
- **11 new mutants of the corrected rule, all caught:**
  - the old open-only window;
  - exempting at-open fills in the interrupted minute;
  - exempting intrabar fills there;
  - the minute 1 ms short or 1 ms long;
  - judging durable fills;
  - no downtime-open window;
  - an exclusive lower bound or an inclusive upper bound;
  - not passing the crash instant;
  - the downtime-observed check (re-run of a mutant whose earlier run ended with a Windows crash code after a test had
    already failed).
- **The 37 earlier mutants, re-run:**
  - 34 caught;
  - `polled_from_crash` still equivalent;
  - `no_p1_rule` and `p1_from_restart_only` no longer apply, because the rule text changed (they are covered by the
    new mutants);
  - `no_downtime_observed` ended with a Windows crash code after a failing test. Re-run in the new set: caught.
- Repository files hash-verified unchanged after both batches.

### Remaining
- P-1: an owner decision and a production change. The re-review's gaps in the proposal must be decided with it.
- Non-blocking re-review findings, not remediated (listed in the brief).
- Empty minute; closes made by a backfill; no CI or Linux run.
- No commit; the RR-B1 remediation needs an independent re-review.

## Closure review and checkpoint
- **Closure review of RR-B1** (owner-authorized, two fresh isolated contexts, read-only):
  - `reviewer`: RR-B1 **PASS**, B-1 preservation **PASS**, checkpoint acceptable, no new blocking finding; 13 of 14
    own mutants caught (1 equivalent).
  - `causality-auditor`: RR-B1 **PASS**, B-1 preservation **PASS**, checkpoint acceptable, no new blocking finding;
    11 of 11 own mutants caught.
  - Both reproduced the pinned cases, 74 tests in the regression file and 362 in `tests/unit/sim`. Their fresh-database
    property runs (three in all) each failed only on the delayed property, on P-1.
  - Their non-blocking findings are recorded in the
    [brief](../tasks/M3-PB.md#closure-review-of-rr-b1--br-1) and not remediated: clock skew in `LiveClock.release`,
    the delay 0 versus 1 ms discontinuity of the instant oracle, coverage of exact crash offsets and non-bar batches,
    and documentation nits.
- **Documentation reconciliation:** the `restart_causality` row of the brief now states the corrected rule; the
  closure verdicts are recorded in the brief, this devlog, the roadmap and `metrics.csv`.
- **Checkpoint:** committed locally on `task/m3-partb` on top of `da42955` as
  `checkpoint(m3): preserve PB.2 harness pending P-1 remediation`. Not pushed, not merged.
- **Not accepted:** the delayed-restart property (P-1), full Part B, the M3 gate.
- **Open:** P-1, empty-minute restoration, closes made by a backfill, cross-platform CI.
