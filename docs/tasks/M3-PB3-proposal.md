# M3-PB.3 — Startup backlog: proposal (diagnosis done, implementation NOT authorized)

Status: **APPROVED by the owner** ("TSC — Owner Approval: Implement M3-PB.3 Startup Backlog Remediation",
2026-11-04), implemented on `task/m3-partb` from `bb04dec` (uncommitted); reviews PASS / APPROVE WITH NITS, 0
blocking ([devlog](../devlog/2026-11-05-m3-pb3-startup-backlog.md)). Blocks M3 Part B; M3 Part B
and the M3 gate are not accepted. The binding design is in [Approved design](#approved-design-binding-for-builders).

## Defect (reproduced 2026-11-04, deterministic)
When updates queue in the paper subscription (the startup wait for the market-data backfill, a downtime backfill, or
any consumer stall), `PaperRuntime._run` (`src/tsclaude/runtime/paper_runtime.py`, loop at `_run`) takes **one**
update, offers it to the `LiveClock`, then calls `service.poll(now)`. `now` is already past the first queued minute's
deadline (`minute + 60 s + settle_ms`, settle 90 s), so `LiveClock.release` (`src/tsclaude/sim/live.py`) releases that
minute with only the first symbol's bar (`incomplete_minutes += 1`). The next queued bar of the same minute is then
`late` (`offer`: `minute < _next`) and is never processed. With a longer backlog every queued minute already past its
deadline is split the same way.

Reproduction (scratch, not in the repository): three minutes queued while paper waits (`GatedFactory` rig of
`tests/integration/test_mvp2_paper.py`), then the gate opens: offered 6, released 5, late 1
(`('usdm', 'BTCUSDT', minute 0)`), incomplete minutes 1. An exposed instrument therefore misses an exit evaluation;
the historical counterexample 603669584 / 125 (ETH stop-loss one minute late, 988.5 instead of 990.0) is the same
mechanism but was **not** re-run on 2026-11-04.

Why the property harness misses it: `reconnect` / `drive` (`tests/unit/sim/paper_scenarios.py`,
`tests/unit/sim/paper_helpers.py`) offer a whole minute and then poll; production offers one update per poll.

`PaperTradingService.run` (`src/tsclaude/sim/paper.py`, async driver) has the same one-update-per-poll pattern
(not the production path today).

## Proposed fix (smallest causally correct)
Drain what is **already queued** before each poll, so a poll decides on every bar that had arrived by then:
1. `Subscription.get_nowait() -> Update | None` (`src/tsclaude/marketdata/service.py`): non-blocking; returns None
   when empty; on the close sentinel puts it back and returns None (so `get()` still reports the close).
   *Not* `get(timeout=0)`: on Python >= 3.12 `asyncio.wait_for(..., 0)` raises `TimeoutError` even when an item is
   queued (the first prototype failed exactly that way).
2. `SubscriptionLike` (runtime protocol, `paper_runtime.py`) gains `get_nowait`; `_run` offers the update it awaited,
   then every update already queued (bounded by the queue size at that moment), then snapshots and polls once.
   The overflow check and the silence watchdog keep their meaning.
3. Optional, same pattern: `PaperTradingService.run` drains the same way (sim owner).

Causality: only bars already delivered before the poll are added to it; release order, the observation anchor
(D-21), settle deadlines and fail-closed behaviour are unchanged. A bar the feed itself delivers after its minute's
deadline is still late (by design). Prototype on a disposable copy (`%TEMP%\pb3proto`): reproduction 6/6 released,
0 late; 122 related tests passed (D-21, P-1, MVP-2/2.1/3/4 paper and API, market-data service, paper restart).

Scope check: no change to `src/tsclaude/contracts/`, journal format or serialization, `PAPER_VERSION`, risk thresholds,
`LiveClock` semantics or E-4(b). `SubscriptionLike` is a runtime protocol, not a contract. Replay is unaffected (it
replays `released`); live sessions after the fix process bars that were previously dropped, which is the intent.
Escalate if the reviewers find that it needs a `PAPER_VERSION` bump.

## Regression tests
1. Runtime integration (`tests/integration/`): backlog queued during the startup wait → every queued bar released, 0
   late, 0 incomplete minutes; red on `bb04dec`, green after.
2. Exposed position: a resting stop-loss whose trigger is in the bar that is lost today fills in that minute at the
   expected price (the 988.5 vs 990.0 shape), and the same scenario after a downtime backfill.
3. Harness fidelity: a `reconnect`/`drive` mode that offers one update per poll in subscription order, so the
   restart properties exercise production's consumption; re-run 603669584 / 125 and the delayed restart property.
4. `get_nowait` unit tests (empty, items, close sentinel kept); the fakes' `FakeSubscription.get_nowait`.
5. Unchanged-behaviour guards: the existing D-21/P-1 anchor tests, replay parity and paper restart suites.

## Ownership and review
- `marketdata/service.py` (`Subscription.get_nowait`): **Data Engineer**.
- `runtime/paper_runtime.py` (`SubscriptionLike`, `_run`): **Platform & UI Engineer**.
- `sim/paper.py` `run` (optional) and harness `tests/unit/sim/`: **Simulation & Risk Engineer**.
- Contracts untouched (Lead not needed for them).
- Reviews: `causality-auditor` (release order, late/incomplete minutes, anchor interaction, mutants) and `reviewer`
  (diff, tests, protocol change), both 0 blocking before a local checkpoint; focused tests + one full non-live run.

## Owner decisions needed
1. Approve the design (drain before poll via `get_nowait`).
2. Include or defer the optional `PaperTradingService.run` change.
3. Confirm that no `PAPER_VERSION` bump is wanted (none proposed).

## Approved design (binding for builders)
**`Subscription.get_nowait() -> Update | None`** (`src/tsclaude/marketdata/service.py`, Data Engineer):
- returns the oldest queued update without awaiting (FIFO, same order as `get()`);
- returns None when the queue is empty (never raises `QueueEmpty`);
- when the next item is the close sentinel: leaves it queued (so the next `get()` still returns None for the
  closure) and returns None; a read-only `closed` property tells "closed" apart from "empty";
- never blocks, never yields to the event loop, never changes `dropped`.
The integration fake `FakeSubscription` (`tests/integration/m1t2_fakes.py`) mirrors exactly these semantics.

**Bounded drain before each poll** (`PaperRuntime._run`, Platform & UI Engineer):
- after an awaited update (or a `get()` timeout), offer every update already queued via `get_nowait`, up to
  `PaperSettings.drain_max` (default 1000, validated > 0, optional `configs/paper.yaml` key) updates per iteration;
- if the limit is reached, the queue may still hold older updates: **do not snapshot, anchor or poll in that
  iteration** (an incomplete minute is never released because of the batch limit); record the deferral, yield
  (`await asyncio.sleep(0)`) and continue the loop (stop, fault, overflow and silence checks still run each
  iteration); with an empty queue the iteration polls as today;
- the overflow (`dropped`) check also runs after the drain; the silence watchdog counts any update received;
- observable: counters (drained updates, largest drain, deferrals) published under a new optional key of the
  runtime's published snapshot and a log line per deferral; no API or contract model change;
- D-21 untouched: the anchor step, its fresh-round request and its codes are unchanged (a deferral only delays the
  step, which is conservative).

**`PaperTradingService.run`** (`src/tsclaude/sim/paper.py`, Simulation & Risk Engineer): evaluate whether it can lose
a bar the same way (only used by `tests/unit/sim/test_live_clock_journal.py` today). If the failure is demonstrable,
apply the same bounded drain (calling `get_nowait` only if the subscription provides it), with a dedicated regression
test; otherwise leave it unchanged and explain why.

**Protected:** `src/tsclaude/contracts/`, journal serialization, `PAPER_VERSION`, risk thresholds, D-21 clock
semantics, `LiveClock` release policy (settle deadline), E-4(b), holdout, research contracts. Stop and escalate if any
of them would have to change.
