# 2026-11-05 — OD-2: restart outcomes RECOVERED / SAFE_BLOCK / FAIL (tests only)

Status: **implemented on `task/m3-partb` (uncommitted, on `d1e280a`); tests and docs only, production unchanged;
focused validation passed; independent reviews on the first hashes: `causality-auditor` CONDITIONAL PASS (0 blocking),
`reviewer` REQUEST CHANGES (2 blocking: CI reporting, this devlog missing), both remediated; re-reviews on the final
hashes: `causality-auditor` **PASS** and `reviewer` **APPROVE WITH NITS**, 0 blocking ([Reviews](#reviews)).** Not committed, not pushed. M3 Part B, M3 availability and M3 are **not** accepted.
Owner authorization: "TSC — Owner Approval: Implement OD-2 Recovery Outcome Classification" (2026-11-05), after the
design report "TSC — OD-2: Formal Recovery Classification and M3 Part B Acceptance Design".

## Contract (OD-2)
Every restart case of the harness (seed, crash point, delay) gets exactly one outcome, judged from the harness ground
truth (the uninterrupted session, every bar that existed and every bar that never did), never from the service's own
report:

- **RECOVERED**: the existing requirements pass, unchanged: `check_restore_equivalence` (strict journal and ledger
  equality for an instant restart; `restart_causality` and `recovery_outcome` after downtime), and the oracle finds the
  evidence a resume needs.
- **SAFE_BLOCK**: blocked in `AWAITING_BACKFILL`, the oracle proves that no store could hold the evidence a resume needs
  (E), and S1–S7 hold. Reasons: `EMPTY_MINUTE` (a required minute where no instrument has a bar) or `INSTRUMENT_GAP` (a
  required bar of an exposed instrument that never existed). **Not recovery, not replay equivalence, not
  availability**: the session stays stopped until an operator acts.
- **FAIL**: anything else. This includes a resume without that evidence (even when it happens to reproduce the
  uninterrupted journal), a block although the evidence exists (store data loss, over-blocking), a fault, a divergence.

Gates: the instant-restart property fails on any FAIL and accepts SAFE_BLOCK only with reason `EMPTY_MINUTE` (and
re-asserts `check_restore_equivalence == []` for RECOVERED). The delayed-restart property still asserts
`check_restore_equivalence == []` first (unchanged), then fails on any FAIL. No `xfail`, no broad exceptions.

## Oracle (E) and invariants
`recovery_evidence` (`tests/unit/sim/paper_scenarios.py`) reads only the durable journal (bars, `minute_closed` marks,
book events → exposure via `journal_exposure`) and the ground truth (`r.bars`, `r.missing`, the restart instant). It
never calls `missing_before`, `_contiguous`, `awaiting_missing`, `_attempt_backfill`, `validate_backfill`,
`resume_after_downtime`, nor reads production-filled fields (`r.closed_through_ms`, `r.last_key`, `r.exposed`).
Contract it states: closed minutes stay closed; the first live minute is the first minute from the clock start with a
delivered, unjournaled bar; a resume needs each exposed instrument's bar for every minute before the first live one
after its last processed minute, the last key and the closed minutes (the rest of an unclosed last minute included),
and, when minutes lie between the last journaled minute and the first live one, a bar of some instrument in the minute
before the first live one.

Safety invariants for SAFE_BLOCK: S1 nothing journaled after the durable prefix; S2 no strategy call after the restore;
S3 the session stepped no batch past the prefix; S4 ledgers equal the prefix's replay; S5 the block names missing
minutes, all within the downtime, covering every minute the oracle finds absent; S6 the runtime's one backfill attempt
was made and refused for completeness only; S7 the block is stable over later polls (1 min, 10 min, 1 day). Plus: the
durable prefix equals the uninterrupted journal's prefix, and `restart_causality` passes (no decision or fill on an
unobserved bar).

## Files
| File | Change |
|---|---|
| `tests/unit/sim/paper_scenarios.py` | OD-2 section appended (oracle, classifier, runtime backfill attempt for instant restarts, stability polls, outcome registry); `Counter` import; existing functions byte-unchanged |
| `tests/unit/sim/test_restart_outcomes.py` | new: historical cases, strict recoverable suite, empty vs feed-only, store loss, invariant meta-tests, guess mutants, oracle independence, delayed census |
| `tests/property/sim/test_paper_properties.py` | the two restart properties gate on the outcome, report it, pin the empty-minute examples |
| `tests/conftest.py` | new: end-of-run summary of the outcomes by group, JSON to `$TSC_RESTART_REPORT` |
| `docs/tasks/M3-PB.md`, this devlog, `docs/devlog/metrics.csv`, `docs/ROADMAP.md` | docs |

## Evidence
Final bytes (SHA-256, first 16 hex): `paper_scenarios.py` **b6340d562888feb7**, `test_restart_outcomes.py`
**1c4a1b91842db68b**, `test_paper_properties.py` **2ba1d55967380d3f**, `tests/conftest.py` **ff96317f841bde11**;
production unchanged (`src/tsclaude/sim/paper.py` e42cc66a514ba0ed, `git diff HEAD -- src configs` empty).

Final run (2026-11-05, project `.venv`, Python 3.13, normal privileges):
- Focused set (`tests/unit/sim`, `tests/property/sim`, MVP-2/2.1/3 paper, P-1 anchor, PB.3 backlog, MVP-2 paper API):
  **608 passed**, 1 warning (pre-existing starlette deprecation). `ruff check src tests` and `ruff format --check`
  clean. `mypy src`: production unchanged, not re-run.
- End-of-run summary (`$TSC_RESTART_REPORT`), deterministic groups: delayed census 60 cases, RECOVERED 45,
  **SAFE_BLOCK 15 (25 %, INSTRUMENT_GAP)**, FAIL 0; the instant property's explicit examples include the two
  empty-minute SAFE_BLOCKs. The property groups also count Hypothesis-generated cases, so their counts vary between runs
  (for example the delayed property gave RECOVERED 24 / SAFE_BLOCK 1 in one run, 23 / 2 and 22 / 3 in others); FAIL was
  0 in every run.
- Sweeps (scratch, same harness): instant restarts, every crash point of 20 seeds (130741648, 50553820 and 18 drawn by
  `random.Random(20261105)`): **1534 RECOVERED, 4 SAFE_BLOCK (EMPTY_MINUTE: 130741648/8, 50553820/63, 138207719/69,
  454919826/27), 0 FAIL**; delayed restarts, 174 cases (10 seeds × 6 crash points × 60 s / 180 s / 600 s): **132
  RECOVERED, 42 SAFE_BLOCK (6 EMPTY_MINUTE, 36 INSTRUMENT_GAP), 0 FAIL**.
- Oracle vs production's own determination (auditor's differential, seeds 0–19, every second crash point, delays 0, 1
  ms, 30 s, 60 s, 3 min, 10 min): 4180 equal, 488 without a live minute, 0 different (508 "oracle less strict" before
  the NB-1 fix). Exposure from the journal equals the books at the restore: 345 of 345 restores.
- Historical counterexamples: 130741648/8 and 50553820/135 (durable 63) are SAFE_BLOCK EMPTY_MINUTE with no problem;
  the absent set is both instruments and "any bar" in minute 4 (`1735689840000`) and minute 34 (`1735691640000`). The
  strict check still reports them (not equivalence). 13073/124 and 465923499/162 are RECOVERED with strict equality.

Mutation (in memory, scratch plugins; repo files untouched; target: the OD-2 tests and the two properties):
- Production: guess an unexplained gap empty (M-A, G1) killed, also by the feed-only test
  `test_a_bar_missing_only_from_the_live_feed_is_backfilled`; process after a block (M-B) killed; backfill gap guessed
  empty (M-D, G2) killed; exposed gaps never reported (M-E) killed; unobserved bars stepped observed (M-F) killed;
  resume after an hour (time-based) killed. Restore ignoring `minute_closed` (M-C) survives the OD-2 tests and is
  killed by 5 existing PB.1/PB.4 tests.
- Classifier: any AWAITING safe (O-1), E from production's `awaiting_missing` (O-2), resumes without evidence accepted
  (O-5), exposure from the books (O-6), and dropping each check alone (prefix, S1–S7, E, "resumed without the
  evidence"): all killed.
- Auditor's mutants re-run on the final bytes: A1–A4, K1–K7 killed by the OD-2 tests (K5, vacuous stability polls,
  now killed); A6 killed only by the delayed property's existing strict check, not by an OD-2 test; **A5 (runtime
  without the tail check) and A8 (every instrument exposed) survive** (NB-3 below). In the closure audit, K8 (revert
  the NB-1 oracle fix) and K9 (drop the S5 naming check) survive (NB-A, NB-B below).

## Known limits (from the reviews; open, non-blocking)
- The oracle restates the contract; it is independent of production's code, not of the contract (a rule wrong in both
  would not be seen). Compared with production's own determination over 20 seeds × every second crash point × 6 delays,
  it was never stricter (auditor NB-2).
- In the harness every instrument is exposed (the benchmark book), so the exposed-only filter and production's
  "minute before the first live minute" check (`paper_runtime.py` tail check) never change an outcome; a runtime
  mutant dropping that check survives every test (auditor A5/A8, NB-3). Follow-up: a scenario without the benchmark book.
- S4 compares with the same `replay` the restore uses (auditor NB-5; mitigated by strict equality on RECOVERED and
  `check_replay_parity`).
- The instant-restart backfill attempt runs after the whole re-delivery, not right after the blocking poll as
  production does; same outcome, since the read window ends at the first live minute (auditor NB-8).
- The safe-block rate after downtime is high in the harness (synthetic 3 % per-instrument gap rate, every instrument
  exposed): 15 of 60 census cases. Real frequency on Binance is not measured.
- A SAFE_BLOCK stops the session (no bar processed, resting SL/TP not resolved) until an operator restarts monitoring
  or starts a new session. No runbook exists yet. M3 availability and autonomous recovery are open product requirements.
- NB-A (closure audit): no test pins the NB-1 oracle fix (mutant K8 survives); in the harness the added items always
  exist, so its evidence is the differential sweep only. Follow-up: assert `required` contains the unclosed last
  minute's remaining exposed bar (for example seed 0, crash batch 30, delay 1 ms).
- NB-B (closure audit): the S5 naming check is redundant with the range check for a one-minute gap (mutant K9 survives)
  and compares minutes, not instruments. Follow-up: a multi-minute-gap tamper and a per-instrument comparison.
- R-9 / R-10 (re-review): the end-of-run summary counts executions (Hypothesis database replays and shrinking included),
  and would undercount under pytest-xdist (not used in CI). R-6 / R-7 (duck-typed runtime in the harness, duplicated
  restore runs) unchanged.

## Reviews
- First review (hashes `8bb32b88` / `48956ffa` / `d8ceb3bb`): `causality-auditor` CONDITIONAL PASS, 0 blocking, NB-1 to
  NB-8; `reviewer` REQUEST CHANGES, B-1 (CI does not report the outcomes separately), B-2 (this devlog and the metrics
  missing), R-1 to R-7.
- Remediation (tests and docs only): B-1 (`tests/conftest.py` summary and JSON), B-2 / NB-6, NB-1, NB-4, NB-7 / R-1, R-2,
  R-3, R-4, R-5. NB-2, NB-3, NB-5, NB-8 documented above.
- Re-review on the final hashes (`b6340d56` / `1c4a1b91` / `2ba1d559` / `ff96317f`): `causality-auditor` **PASS**, 0
  blocking (NB-1, NB-4, NB-6, NB-7 closed; NB-A, NB-B, NB-C new and non-blocking; no look-ahead); `reviewer` **APPROVE
  WITH NITS**, 0 blocking (B-1, B-2, R-1, R-2, R-5 closed; R-8 to R-11 nits). The reviewer also ran the full non-live
  suite on these bytes: **1622 passed, 1 skipped, 2 deselected** (628 s, exit 0). NB-C (this devlog's mutant and
  property-count wording) and R-8 (ROADMAP wording) fixed in the docs afterwards; test and harness bytes unchanged.
