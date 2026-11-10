# M3 acceptance request (for the owner; NOT approved)

This asks the owner to decide proposal **D-A** ([DECISIONS_PENDING](DECISIONS_PENDING.md)). Nothing here
accepts M3. Source: the independent closure audit ([MVP_CLOSURE_HANDOFF](MVP_CLOSURE_HANDOFF.md) §1, §2).

## The gate, as written (ROADMAP "M3")
*"recorded live session replays to an identical ledger hash; fault-injection tests (stale stream, exception
in risk rule, crash mid-fill) all block or recover correctly."*

## What is demonstrated

| Requirement | Evidence |
|---|---|
| A recorded live session replays to an identical ledger hash | Three real Windows paper sessions pass `verify-replay`: 1 h (2026-11-06, 360 batches), 25 min (2026-11-09, 150 batches), crash recovery after a PC restart (2026-11-09, 3,498 batches). All three also replay identically on Linux, with the store's SHA-256 unchanged before and after. **All three were recorded at decision latency 0** (paper config digest `7a68bcfc…`), not under the current configuration (see "Configuration change" below) |
| Stale stream blocks | `tests/unit/sim/test_paper_session.py:240`; in the real app a clock 3.4 s and 6.9 s off blocked all observation (fail closed) |
| Exception in a risk rule blocks | `tests/unit/sim/test_paper_session.py:223`; `tests/unit/risk/test_risk_rules.py:187,197` |
| Crash mid-fill blocks or recovers | `tests/unit/sim/test_paper_restart.py:168`; a real PC restart mid-session recovered with replay PASS (not timed mid-fill) |
| The tests run green | Before task T1 (Target A at latency 0): audit re-run, 20 gate tests passed; CI 7/7 green on many commits, latest listed in [MVP_VALIDATION_REPORT](MVP_VALIDATION_REPORT.md), last at `0955749` (run `42859002211`). After T1–T4: see the CI run recorded for them in [MVP_TASK_QUEUE](MVP_TASK_QUEUE.md) (rows OR-1 to OR-4); the owner's gate subset (§6 step 2 of the readiness review) gives 120 tests at `0955749`, 124 at `59c826a` and 126 from `8f8b2f5` on |
| Strategy to fill (Target A, deterministic) | 13 Target A tests, since task T1 run with the production paper latency (3,000 ms, read through `build_paper_config`), plus a latency-0 case of the next-open rule and a test that fails if Target A and the production latency drift apart (`tests/unit/sim/test_target_a_e2e.py`) |

## Configuration change after the recorded sessions (2026-11-10)
- **What changed.** Paper reads `decision_latency_ms` from `configs/backtest.yaml`
  (`build_paper_config`, `runtime/paper_runtime.py`). The value is part of the session
  configuration (`PaperConfig.describe()`, and so the paper config digest). `1cc2693` changed it
  from 0 to 3,000 ms, at least the measured live p95 of 2,170 ms, so that backtests and Paper use
  the same execution timing.
- **Digests**, recomputed with `session_evidence.config_digest`, Paper enabled with
  `bracket_reference`:
  - configs as at `65c0e2f`, latency 0: `7a68bcfc…`;
  - current configs, 3,000 ms: `676d69b6…`.
- **Read from the sessions themselves (2026-11-10):** the `config_digest` field of each session's
  `data\session_end\*.json` is `7a68bcfcfd52ac5a…` in all three (`paper-bracket-20261106-104529`,
  360 batches; `paper-20261109-2201`, 150; `recovery-20261109-2227`, 3,498; each state RUNNING),
  staged read-only from the owner PC. So the recomputed digest and the recorded one agree.
- **Evidence recorded before the change (the three sessions above).**
  - It is replay evidence for the latency-0 configuration.
  - It passes `verify-replay` with each session's own config folder (last run at `65c0e2f` and
    `2c7d343`; repeating it at the accepted commit is an owner step).
  - Checked against the repository's current `configs/`, it is INCONCLUSIVE ("configuration
    differs"), never a false PASS (operational-readiness review R-01).
- **Evidence under the current configuration.**
  - Target A runs at 3,000 ms (tests only).
  - No real session has been recorded or replayed at 3,000 ms yet. The owner's fresh supervised
    session is the step that produces it.
- **Expected live behaviour.** Entries still fill at the second 1m open after the decision close,
  because a live bar is released after the next minute has opened. This is an expectation backed by
  tests (the timing function, Target A at 3,000 ms), not yet by a real session.
- **Sessions recorded with the older default configs** do not restore after an update. They are
  refused (fail closed), and since task T3 the reason names the changed field
  (`decision_latency_ms: 0 -> 3000`) and the hint gives the recovery: either start with the
  session's own config folder, or press Stop and use **Operator action → Record new paper session**
  (the old session's record is kept). The old session stays checkable with `verify-replay` and its
  own config folder (with `--evidence` once a newer session has stopped in the same folder).

## Residual limitations (stated, not hidden)
1. **No strategy book traded in a real session (U1).** Only the benchmark book traded. The reference strategy
   almost never confirms on spot at current costs, and the live baseline book would reject its geometry
   anyway (net reward/risk below 1.5; regression test `tests/unit/research/test_live_compat.py`). The
   deterministic Target A tests are proposed as the strategy-to-fill evidence, as already decided for
   Target A on 2026-11-06.
2. **Restart equivalence is not proven (U2)**, because no uninterrupted twin exists. It is already accepted
   for the supervised paper-only MVP (D-19, D-20, D-21).
3. **No operator kill-switch reset in the app (D-B, open).** Today's recovery is "record a new paper
   session" ([MVP_USER_GUIDE](MVP_USER_GUIDE.md) §3).
4. **The owner PC's instability** (a restart, one unrepeatable access violation) is outside the software.

## What accepting would and would not mean
- **Would:** M3's literal gate is formally accepted, on this evidence and with these limitations.
- **Would not:** anything about M4 research, any strategy's effectiveness, M5/M6, or live trading (none is
  planned before a separate owner decision).

## Options
The decision must also state which configuration it covers:
- the three latency-0 sessions only (`7a68bcfc…`);
- or also the current configuration (`676d69b6…`), through the fresh session.

1. **Accept** with limitations 1–4 restated (recommended).
2. Accept after a longer supervised session (it would not close U1: no real signal is expected).
3. Do not accept now. No engineering work is blocked either way.
