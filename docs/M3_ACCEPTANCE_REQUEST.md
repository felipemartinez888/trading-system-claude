# M3 acceptance request (for the owner; NOT approved)

This asks the owner to decide proposal **D-A** ([DECISIONS_PENDING](DECISIONS_PENDING.md)). Nothing here
accepts M3. Source: the independent closure audit ([MVP_CLOSURE_HANDOFF](MVP_CLOSURE_HANDOFF.md) §1, §2).

## The gate, as written (ROADMAP "M3")
*"recorded live session replays to an identical ledger hash; fault-injection tests (stale stream, exception
in risk rule, crash mid-fill) all block or recover correctly."*

## What is demonstrated

| Requirement | Evidence |
|---|---|
| A recorded live session replays to an identical ledger hash | Three real Windows paper sessions pass `verify-replay`: 1 h (2026-11-06, 360 batches), 25 min (2026-11-09, 150 batches), crash recovery after a PC restart (2026-11-09, 3,498 batches). All three also replay identically on Linux, with the store's SHA-256 unchanged before and after |
| Stale stream blocks | `tests/unit/sim/test_paper_session.py:240`; in the real app a clock 3.4 s and 6.9 s off blocked all observation (fail closed) |
| Exception in a risk rule blocks | `tests/unit/sim/test_paper_session.py:223`; `tests/unit/risk/test_risk_rules.py:187,197` |
| Crash mid-fill blocks or recovers | `tests/unit/sim/test_paper_restart.py:168`; a real PC restart mid-session recovered with replay PASS (not timed mid-fill) |
| The tests run green | Audit re-run: 20 gate tests passed. CI 7/7 green on many commits since, latest listed in [MVP_VALIDATION_REPORT](MVP_VALIDATION_REPORT.md) |

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
1. **Accept** with limitations 1–4 restated (recommended).
2. Accept after a longer supervised session (it would not close U1: no real signal is expected).
3. Do not accept now. No engineering work is blocked either way.
