# 2026-11-03: M3 Part B class 1, recorded minute close in the paper journal

**Status:** local checkpoint `0435116944032b95ab21456f8c669c3d8af357b4` on `task/m3-partb`, base `f1d5079`, not
pushed and not merged. The independent re-review of M3-PB.1 on `0435116` gave PASS / PASS, 0 blocking, so the M3-PB.1
implementation is accepted as a checkpoint. **Full Part B and the M3 gate are not accepted.** Brief:
[M3-PB](../tasks/M3-PB.md).

The sections below are in chronological order. Each describes the state when it was written: "uncommitted", "not
reviewed" and "Not done" in the earlier sections were true at the time.

## Base
- `feat/mvp-4-dashboard` at `f1d5079`, with no tracked changes. The branch was created in the main checkout, because
  the `.venv` editable install points at its `src`.
- The uncommitted E-4(b) work in `C:\Users\felip\Projects\tsc-e4b` was not touched.
- No paper session, journal, dataset or holdout file was opened. No network was used.

## Reproduction, on unmodified code
- `check_restore_equivalence(13073, 124)` gave `ledgers differ after restore at batch 49`.
- The last durable batch was BTC minute 25, released 121.5 s after its close, which is past the 90 s settle deadline,
  with ETH 25 absent. The next write (BTC 26) failed. After restore, `missing_before(minute 26)` listed ETH 25, so the
  service stayed `AWAITING_BACKFILL`.
- **Root cause:** a release by deadline is a decision of the live clock: the minute is closed, and ETH 25 would be
  late. The journal did not record that decision, so restore had to treat the key (25, spot, ETHUSDT), which comes after
  `last_key`, as possibly unprocessed.

## Sampling
A scratch script outside the repository ran the property construction on 2,000 fixed (seed, fail_batch) cases (RNG seed
20261103), on unmodified and on final code.

| Result | Before | After |
|---|---:|---:|
| Ledgers equal | 1,972 | 1,993 |
| `AWAITING_BACKFILL` (class 1) | 27 | 1 |
| RUNNING, shadow differs | 1 | 4 |
| RUNNING, baseline and shadow differ | 0 | 2 |

- **The remaining `AWAITING_BACKFILL`** (130741648 / 8) is a minute that had no bar at all, so no entry exists to carry
  its close. It is pinned as a characterisation.
- **The six RUNNING divergences** were checked case by case. The first differing event is `earliest_exec_ms`, or a
  validity rejection that depends on it, and it follows the release time. That is the class 2 mechanism. Five of them
  were hidden before by the class 1 block.
- **Class 2 is broader than its characterisation.** The characterisation says only the shadow book differs, and that
  holds for seed 465923499 only: 2 cases here also changed baseline.
- **Correction after the causality audit (F1, below):** these divergences come from the harness polling the restored
  service before the crash. They are not a defect of the simulator or of restore. The two bullets above describe what
  was measured under that timeline.
- **Seed 13073 is one of the six.** Its minute 26 is released 1.5 s after its close following the restart, against
  61.5 s in the uninterrupted run, where it waited behind minute 25's deadline.

## Change
`src/tsclaude/sim/paper.py`, as specified in the brief:
- the `minute_closed` mark, written by `poll` and recorded by `step_bar`;
- the replay checks;
- the restored clock position;
- `missing_before` and `resume_after_downtime` respect a recorded close.

No contract, journal module, clock or event-store change.

## Tests (Python 3.13, `.venv`, sequential, one process each, no process death)
| Batch | Result |
|---|---|
| `tests/unit/sim` | 307 passed |
| `tests/unit/sim/test_sim_property_regressions.py` alone | 19 passed |
| `tests/property/sim` | 8 passed, **1 failed**: `test_restore_after_any_journal_failure` on its stored example 13073 / 124, now the class 2 divergence. The property is unchanged |
| `tests/integration/test_mvp2_paper.py`, `test_mvp2_paper_api.py`, `test_mvp21_paper_hardening.py` | 39 passed |
| `tests/integration/test_mvp3_reference_paper.py`, `test_mvp4_paper_api.py`, `tests/unit/test_runtime_misc.py`, `tests/unit/test_sessions.py`, `tests/unit/risk` | 92 passed |
| `ruff check --no-cache src tests scripts` | All checks passed |
| `ruff format --no-cache --check src tests scripts` | 220 files already formatted |
| `mypy src` with a fresh `--cache-dir` | No issues in 116 files |

- **Tool failures with the existing caches:** ruff panicked with `wrong package cache` on `scripts/`, and mypy 2.4.0
  raised an INTERNAL ERROR. Both passed with their caches bypassed. No cache was deleted.
- **One pinned value changed, deliberately:** `PINNED_JOURNAL_HASH`. The new test
  `test_the_close_marks_are_the_only_journal_change_of_part_b` shows that removing the marks gives back the previous
  value. Ledger hashes are unchanged.
- **Characterisation replaced by behaviour tests:** the previous class 1 characterisation (13073 blocks) is now
  `test_seed_13073_the_minute_closed_before_the_crash_is_not_downtime`. Its original assertions are kept verbatim for
  a legacy journal in `test_a_journal_without_close_marks_keeps_the_fail_closed_reading`.

## Mutants
11 hand mutants of `paper.py` were run against the regression, session and restart test files. The original bytes were
restored and verified by SHA-256.

- **Caught (10):** no mark, a mark on every bar, `missing_before` ignoring the close, the restored clock position, the
  restored processed set, the backfill filter, replay dropping the mark, replay skipping the after-close check, no
  `closed_through_ms`, and `false` written when not closing.
- **Survived (1):** `bool(mark)` instead of `mark is True`. It is equivalent: a non-`true` mark cannot be reproduced,
  so verification refuses it either way, which `test_a_close_mark_with_another_value_refuses_the_restore` checks.

## Not done
- Commits, merges, pushes and CI.
- A broad suite run.
- A Linux run.
- Class 2.
- The empty-minute case.
- Any review.

## Instructions for the independent review
Give each reviewer `docs/tasks/M3-PB.md` and `git diff f1d5079 -- src tests` only, not this devlog.

- **`reviewer`:**
  - correctness of the mark placement when one release spans several minutes, and after a restore with a partly
    processed minute;
  - legacy and forward compatibility as stated;
  - whether rejecting a bar in a closed minute could fault a live path that is legitimate today (`skip_to`,
    `resume_after_downtime`, the runtime backfill in `runtime/paper_runtime.py`);
  - test quality.
- **`causality-auditor`:**
  - the mark records only what the clock knew at release time and never feeds fills, risk or strategy;
  - no event reordering;
  - a closed minute can never let an unobserved minute count as observed;
  - the fail-closed paths: a lost bar of an unclosed minute, real downtime, and the empty minute.
- **Commands** (normal PowerShell, no Administrator rights):

```bash
.venv/Scripts/python.exe -m pytest tests/unit/sim tests/property/sim -q -p no:cacheprovider
```
```bash
.venv/Scripts/python.exe -m pytest tests/integration/test_mvp2_paper.py tests/integration/test_mvp21_paper_hardening.py tests/integration/test_mvp3_reference_paper.py -q -p no:cacheprovider
```

- **Expected:** one failure, the property on 13073 / 124, until class 2 is decided.

## Independent reviews (2026-11-03)
Both ran in fresh contexts on the brief and the diff only. Neither read this devlog, and neither modified any file.

| Review | Verdict | Blocking | Main findings |
|---|---|---|---|
| `reviewer` | CONDITIONAL PASS (class 1 only) | 0 | N-1 stale limitation texts (the condition); N-2 step guard untested; N-4 no multi-minute mid-release crash test; N-5 backfill closes not journaled; N-6 vague message in older code; N-7 wrong comment |
| `causality-auditor` | CONDITIONAL PASS (class 1 only) | 0 | F1 harness timing; F2 two cases exposed only under harness timing; F3 stale texts (same as N-1); N1 forged mark protected only by the hash chain; N2 backfill closes; N3 empty minute |

**Neither verdict covers Part B or M3,** which stay not accepted.

**F1, the harness timing artifact, which is distinct from the class 1 defect:**
- The class 1 defect was in restore: a gap the session had passed was read as downtime, so the service never
  resumed. It is fixed.
- The artifact is in the property harness: `check_restore_equivalence` re-drives the restored service on the original
  timeline, so it polls before the instant of the failed write.
- **Verified here, on the final code:**
  - 13073 / 124: the crash poll was at minute 28 + 1.5 s, and the restored service released BTC and ETH minute 26 at
    minute 27 + 1.5 s.
  - 465923499 / 162: the crash poll was at minute 9 + 1.5 s, and ETH 7 was released at minute 8 + 1.5 s.
  - The auditor's probe test rerun gave `2 failed, 2 passed`: the harness releases before the crash, and with
    monotone time both seeds equal the uninterrupted ledgers.
- **Auditor's sample, 2,000 cases:**
  - harness timeline: 1,988 equal;
  - polling never before the crash: 1,997 equal, 0 RUNNING divergences, 3 empty-minute cases that fail closed.
- The harness is unchanged. Changing it is an owner decision.

## M3-PB.1 (owner-authorized follow-up to the reviews)
- **N-1 / F3:**
  - the `paper_runtime.py` docstring and `LIMITATIONS`, which the API serves;
  - the matching comment in `configs/paper.yaml` (the same served limitation, so corrected for consistency);
  - the P5 docstring in `tests/integration/test_mvp2_paper.py`, which describes real downtime and not class 1.
  - The texts now say: a passed gap is not downtime; a bar missing after the last closed minute that no backfill can
    supply still blocks, including an empty minute, an unclosed last minute and legacy journals; fills in any book,
    the baseline included, can differ after a restart.
- **N-2:** `test_the_session_refuses_a_bar_of_a_minute_already_closed`. It covers the direct `ValueError` with nothing
  changed, and FAULTED through the service with nothing journaled.
- **N-4:** `test_a_crash_inside_a_multi_minute_release_restores_equivalently`. One poll releases minutes 0-5 with BTC 2
  and ETH 3 missing, and a write fails at ETH 2, BTC 3, BTC 4, ETH 4 or ETH 5. The re-delivery reproduces the reference
  batches (canonical), ledgers and marks, and re-delivered durable bars are late. All polls are monotone.
- **N-7:** the comment is corrected.
- **F1:** recorded in the brief, here, and in the docstrings of the two class 2 characterisations. No assertion changed.

### Tests (sequential, one process each)
| Batch | Result |
|---|---|
| `tests/unit/sim/test_sim_property_regressions.py` | 25 passed |
| `tests/unit/sim` | 313 passed |
| `tests/property/sim` | 8 passed, 1 failed: `test_restore_after_any_journal_failure` on 13073 / 124 (harness timing, unchanged) |
| `test_mvp2_paper.py`, `test_mvp2_paper_api.py`, `test_mvp21_paper_hardening.py` | 39 passed |
| `test_mvp3_reference_paper.py`, `test_mvp4_paper_api.py`, `test_runtime_misc.py`, `test_sessions.py`, `tests/unit/risk` | 92 passed |
| `ruff check` / `ruff format --check` (`--no-cache`), `mypy src` (fresh cache) | Clean / 220 formatted / no issues in 116 files |
| Mutants on a scratch copy of `src` (loaded first through `PYTHONPATH`), against the two new tests only | 8 of 12 caught; m6, m9 and m11 are caught by other tests in the file (earlier full run); m8 is equivalent |

### Incident during mutation testing
- An earlier mutant script wrote mutants into `src/tsclaude/sim/paper.py` in place. One run failed with
  `OSError: [Errno 22] Invalid argument` on a write, and its restore failed the same way. The file was left with mutant
  m7 (`minute_closed=False` in `replay`).
- It was found by the hash check, and the one line was restored by hand. SHA-256 is `d48500576af7…`, equal to the
  value before the run. `sim/live.py`, `sim/journal.py` and `sim/engine.py` were verified unchanged against the base.
- From then on, mutants run only on a copy outside the repository. The cause of the write error is unknown and was not
  investigated (owner rule on hardware).

## Checkpoint and re-review of M3-PB.1 (2026-11-03)
- **Checkpoint:** `0435116944032b95ab21456f8c669c3d8af357b4`, `checkpoint(m3): preserve Part B class-1 remediation
  pending acceptance`, parent `f1d5079`, 10 files. Local only, not pushed and not merged.
- **Re-review:** owner-authorized. `reviewer` and `causality-auditor` ran in fresh, isolated contexts on
  `git diff f1d5079..0435116`, read-only, with mutants only on copies of `src` outside the repository. Neither modified
  a repository file.

| Review | Verdict | Blocking | Non-blocking | Runs |
|---|---|---:|---:|---|
| `reviewer` | PASS | 0 | 4, plus 2 nits | `tests/unit/sim` 313 passed; property sim 8 passed, 1 failed (13073 / 124); paper integration 36 passed; ruff, format, mypy clean; 14 of 14 mutants caught |
| `causality-auditor` | PASS | 0 | 4 | `tests/unit/sim` 313 passed; property sim 8 passed, 1 failed (13073 / 124); paper integration and API 52 + 25 passed; 18 of 19 mutants caught, 1 equivalent |

- **Results:**
  - N-1, N-2, N-4, N-7, F1 and the class 1 remediation were verified by both reviews.
  - All eight checks passed in both reviews.
  - The mutant counts differ from the M3-PB.1 figure above (8 of 12 by the new tests) because the reviewers used
    their own mutant sets and test selections.
- **F1 probe** (auditor, rerun by the Lead): the property construction with polls never before the crash, harness
  unchanged.
  - 13073 / 124: `result: equal`, RUNNING, the same bar sequence, the same inputs apart from release times. The harness
    gives `ledgers differ after restore at batch 49`.
  - 465923499 / 162: `result: equal`, RUNNING. The harness gives `ledgers differ after restore at batch 14`.
  - 300-case sample: 297 equal under both timelines, 3 failing only under the harness timeline, none in
    `AWAITING_BACKFILL`.
- **Non-blocking findings, not fixed:** listed in the [brief](../tasks/M3-PB.md#re-review-of-m3-pb1-on-0435116).
  - Limitation wording errs in the safe direction.
  - The N-4 test slices by batch count.
  - The property's failing example comes from the git-ignored `.hypothesis/` database, with no `@example`.
  - The stale "uncommitted" texts are corrected by the documentation checkpoint that adds this section.

### Acceptance
| Scope | Status |
|---|---|
| M3-PB.1 implementation | Accepted as a checkpoint: PASS / PASS, 0 blocking |
| Full Part B | **Not accepted** |
| M3 gate | **Not met** |

### Remaining limitations
- The property harness re-drives the restored service before the crash instant (F1), so
  `test_restore_after_any_journal_failure` fails on 13073 / 124. Changing it is an owner decision.
- A minute released with no bar leaves no entry to mark, and fails closed (130741648 / 8).
- Closes made by a backfill are not journaled (N-5 / N2): causal, but not equivalent after a later restart.
- There is no CI or Linux run of this work. Pushing needs owner approval (D-07).
