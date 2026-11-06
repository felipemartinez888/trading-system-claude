# 2026-11-04/05 — M3-PB.3: startup backlog (bounded drain before each poll)

Status: **implemented on `task/m3-partb` (uncommitted, on `bb04dec`); focused validation passed; independent reviews:
`causality-auditor` PASS (0 blocking, 3 non-blocking), `reviewer` APPROVE WITH NITS (0 blocking, 6 non-blocking);
non-blocking A-1 (flaky test) fixed test-only and approved by a scoped review ([A-1](#a-1-flaky-test-fixed-2026-11-05)).**
Not committed (checkpoint awaits owner approval), not pushed, not merged. M3 Part B and the M3 gate are **not**
accepted. Owner authorization: "TSC — Owner Approval: Implement M3-PB.3 Startup Backlog Remediation" (2026-11-04).
Brief and binding design: [M3-PB3-proposal](../tasks/M3-PB3-proposal.md). Work was interrupted twice by Windows
restarts; every builder's work survived and was verified by hash before reuse.

## Root cause (confirmed)
`PaperRuntime._run` awaited one subscription update, offered it to the `LiveClock`, then polled. With updates queued
(the startup wait for the market-data backfill, a downtime backfill, any stall), `now` was already past the first
queued minute's deadline (minute + 60 s + settle 90 s), so `LiveClock.release` released that minute with only its
first bar and the next queued bar of the same minute was classified `late` and never processed (an exposed instrument
missed an exit evaluation). `PaperTradingService.run` had the same pattern.

## What changed (by owner)
- **Data Engineer**: `Subscription.get_nowait()` (`src/tsclaude/marketdata/service.py`): oldest queued update without
  awaiting (FIFO); None when empty or when the close sentinel is next (the sentinel stays queued, so the next `get()`
  still reports the closure); read-only `closed`; never yields, never changes `dropped`. A private `asyncio.Queue`
  subclass (`_init` storage hook, same deque as stock) lets the head be inspected without consuming it.
- **Platform & UI Engineer**: `PaperRuntime._run` offers every update already queued (`_drain`, at most
  `PaperSettings.drain_max`, default 1000, optional `configs/paper.yaml` key) before it snapshots, anchors and polls
  once. At the bound the iteration neither snapshots, anchors nor polls (no minute is released incomplete because of
  the bound), counts a deferral, logs it, yields and continues; overflow is checked after the drain too; counters are
  published under the internal snapshot key `consumer` (not served by `/api/paper/status`). `SubscriptionLike`
  protocols (runtime, supervisor) gain `get_nowait`. The integration fake mirrors the real subscription.
- **Simulation & Risk Engineer**: `PaperTradingService.run` (`src/tsclaude/sim/paper.py`, `run()` only) gets the same
  bounded drain (`drain_max` keyword; no drain if the subscription has no `get_nowait`). The restart harness
  (`tests/unit/sim/paper_scenarios.py`) gains an optional delivery mode (`per_update` legacy, `drained` fixed) with a
  startup wait; the default path is unchanged (equivalence test).
- Not changed: `src/tsclaude/contracts/`, journal serialization, `PAPER_VERSION`, risk thresholds, `LiveClock`
  (`sim/live.py`), the D-21 functions (`_anchor_step`, `_establish_observation`, `_request_round`, resync), `configs/`,
  E-4(b), frontend and API models.

## Evidence
Red on `bb04dec` (builders' runs, logs in the session scratchpad):
- Runtime test A (3 queued minutes, 2 instruments): offered 6, released 5, late 1 (`usdm` BTCUSDT minute 0),
  incomplete 1; 8 of 9 new runtime tests failed.
- `PaperTradingService.run`: 6 of 7 failed; a 3-minute backlog lost 3 BTC bars; a BTC stop at 990 filled a minute
  late at 988.5.
- `get_nowait` tests: 10 failed (no attribute).

Green after the fix (newly executed, Lead, 2026-11-04 23:23):
- Integrated focused set (PB.3 runtime, unit market data, unit sim, paper properties, D-21, P-1, market-data service,
  MVP-2/2.1/3/4 paper and API, API, server lifecycle): **744 passed** (`%TEMP%\tsc-pb3-focused.txt`).
- Test A: offered 6, released 6, late 0, incomplete 0. Test B (real runtime and supervisor, restart with a stop in the
  bar queued behind its minute): the stop fills in that minute at the execution model's price (`resolve_exit` and the
  cost model), no fill on a later bar, ledger reconciled, journal replay gives the same ledger hashes.
- Historical **603669584 / 125**, reproduced exactly (instant restart, BTC before ETH within a minute, startup wait
  88.5 s to about 148 s): `per_update` → ETH minute-24 bar late, baseline stop fills at minute 25, **988.5**, ledgers
  differ; `drained` → stop at minute 24, **990.0**, ledgers equal to the uninterrupted session. The auditor confirmed
  the same with a runtime-faithful model (awaited update, bounded drain, deferral, one snapshot per poll) for
  `drain_max` 1, 2, 3, 5, 1000, gated and ungated. With ETH before BTC (the `reconnect` default) the same restart loses
  BTC's bar instead: equivalent regression pinned (delay 30 s, wait 180 s).
- `ruff check src tests`, `ruff format --check`: clean (two mechanical lint fixes in the new runtime test).
  `mypy src`: no issues in 116 files (a first attempt crashed natively with 0xC0000005; the retry passed; not
  investigated).
- The full non-live suite was **not** re-run (focused coverage of every touched module; owner instruction).

Reviewed bytes (SHA-256, first 16 hex): `marketdata/service.py` e0e6c923de8b2e0f, `runtime/paper_runtime.py`
eaa0d275c74a5c7d, `runtime/supervisor.py` bf123aa06d753316, `sim/paper.py` 24e6e82824a9208a,
`tests/integration/m1t2_fakes.py` 4d1666abb8d1aaf6, `tests/integration/test_p1_observation_anchor.py`
aa19a8d51fa8c965, `tests/integration/test_pb3_startup_backlog.py` 31d6cb46c59bbf55 (superseded by the A-1 fix, 35fe2cf1e2c66715, below),
`tests/unit/marketdata/test_subscription_nowait.py` 2c0a24e8d3954d3d, `tests/unit/sim/paper_scenarios.py`
00d36385bcaa64b8, `tests/unit/sim/test_paper_run_drain.py` 5047d3424e072748,
`tests/unit/sim/test_paper_backlog_delivery.py` c48f9b6e826b8852.

## Independent reviews (mono-model)
**`causality-auditor`: PASS, 0 blocking.** No look-ahead: no await between the drain and reading `now`, so a poll
processes only bars queued before `now`; `LiveClock` release policy unchanged; FIFO preserved; at the bound nothing is
released; closure, stream failure and overflow mid-drain fail closed; D-21 anchor code unchanged and only delayed
(re-checked for freshness). Mutants: 15 of 16 killed (poll despite the bound, off-by-one bounds, no drain, drain after
the poll, sentinel popped or LIFO in the real subscription, no overflow check, `run()` variants, anchor during a
deferral, watchdog); the sentinel-popping mutant of the **fake** survives.

**`reviewer`: APPROVE WITH NITS, 0 blocking.** Protocols satisfied by the real and fake subscriptions; the demo
consumer is unaffected; the queue subclass keeps stock `asyncio.Queue` semantics on Python 3.13 (`QueueFull`,
`maxsize`, FIFO, `task_done`/`join`); no API, contract or frontend change; harness default unchanged. 7 of 7 own
mutants caught.

Non-blocking findings (open):
- **A-1 (auditor) flaky test**: fixed, see [below](#a-1-flaky-test-fixed-2026-11-05).
- **A-2 / R-1 sentinel checks are weak**: the fake's `get_nowait` may pop the sentinel undetected; the real
  subscription's `async for` tests hang (no pytest timeout) instead of failing under that mutant. Fix: assert
  `head_is_close_sentinel()` / queue size after `get_nowait`; wrap async iteration in `asyncio.wait_for`.
- **A-3 / R-6** at exactly `drain_max` the poll waits up to `poll_s` more (conservative, no late bar).
- **R-2** `PaperSettings.load` converts `drain_max` with `int()` before the bool check (`true` → 1, `2.9` → 2); reject
  non-int values in `load`.
- **R-3** a deferral does not publish; no cap on consecutive deferrals (logged; acceptable under the brief).
- **R-4** `PaperTradingService.run(drain_max=True)` is not rejected.
- **R-5** the `ran` event check in the `get_nowait` no-yield test cannot fail (the coroutine checks are the guard).
- Harness note (auditor): the delivery harness idealizes snapshots and the anchor; the runtime-faithful model shows it
  does not change the release outcome.

Operational note: a mutant run of the reviewer (`test_subscription_nowait.py` in its scratch copy, PIDs 23504/23272,
started 2026-11-04 23:38) hung and is still running; its kill was denied by the permission system, so it is left
for the owner to stop. It does not touch the repository.

## A-1: flaky test fixed (2026-11-05)
Owner authorization: "TSC — M3-PB.3: Fix Flaky Test A-1 Before Local Checkpoint". Test-only; no production change.
- **Root cause.** `test_bound_defers_the_poll_and_never_releases_a_minute_incomplete` asserted 4 deferral log lines
  over the whole scenario, including `rig.minute(6)` after the backlog. `rig.minute(6)` pushes both bars in one
  event-loop turn; when the consumer's `get(timeout=POLL)` expires in that same turn it returns None with both bars
  queued, the drain then takes exactly `drain_max` (2) and that iteration legitimately defers once more (the
  conservative exact-bound behaviour, A-3): nothing late or incomplete. Reproduced on the reviewed version: 8 of 30
  file runs failed, always at that assertion; the scoped reviewer measured the fifth deferral (`drained=2`,
  `awaited=False`) in 5 of 40 instrumented runs.
- **Fix** (`tests/integration/test_pb3_startup_backlog.py`, 31d6cb46c59bbf55 → **35fe2cf1e2c66715**): the exact
  count is asserted where it is deterministic (after the backlog, whose updates were all queued before the first
  `get()`): published counters (deferrals 4, drained 8, largest 2) **and** 4 log lines. After minute 6: full stats
  14/14/0/0 (was only `released == 14`), journal order for minutes 0–6, logged deferrals equal the runtime's counter,
  deferrals in {4, 5} and every deferral a full drain of `drain_max` (the scoped reviewer's NB-1, restoring the only
  coverage the first fix had dropped).
- **Validation.** Version 6a73c2ae: 55 bounded file runs, 52 completed all "9 passed", 0 assertion failures, 3 native
  `0xC0000005` crashes (no pytest result; environmental, not investigated). Final version 35fe2cf1: 52 runs, 51 "9
  passed", 0 assertion failures, 1 native crash. Focused set 744 passed; historical 603669584 / 125 tests passed;
  ruff and format clean; production hashes unchanged (service e0e6c923, paper_runtime eaa0d275, supervisor bf123aa0,
  sim/paper 24e6e828).
- **Scoped independent review** (`reviewer`, fresh context, on 31d6cb46 → 6a73c2ae, then confirmed on 35fe2cf1):
  **APPROVE**. Root cause confirmed as a test race, not a production defect; not weakened; mutants (poll despite the
  bound, deferral not logged, counter not incremented, off-by-one bound) all killed; 15 + 10 repeat runs green. The
  earlier PB.3 reviews did not see the 35fe2cf1 hash; the rest of their scope is unchanged.

## Remaining for M3 Part B
- A local checkpoint of this work (owner approval).
- The empty-minute limitation (a minute released with no bar; journal-format decision).
- Closes made by a backfill are not journaled (owner decision 4 in M3-PB).
- The delayed-restart property and full restart equivalence re-evaluated with this fix; CI and a Linux run.
