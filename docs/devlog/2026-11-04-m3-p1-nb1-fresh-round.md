# 2026-11-04 — M3 P-1: NB-1 option B (fresh clock round before the anchor, D-21)

Status: **implemented on `task/m3-partb` (uncommitted, on `b4f8d01`); regression tests and the full non-live suite
pass; independent causality review: PASS (0 blocking, 5 non-blocking). NB-1 formally CLOSED under D-21** (owner
approval "TSC — Owner Approval: Close NB-1 and Create D-21 Local Checkpoint", 2026-11-04), with the owner's explicit
acceptance of the residual clock-continuity risk below for the local, supervised, paper-only MVP, subject to the
fail-closed safeguards. Committed as a local checkpoint on `task/m3-partb` (child of `b4f8d01`; not pushed, not
merged). This does not accept Part B, the M3 gate or any other milestone gate, and does not by itself declare P-1
accepted. Owner authorizations: "TSC — Owner Decision D-21: Approve NB-1 Option B" and the closure approval above
(both 2026-11-04).

## Decision (D-21)
Option B: a fresh server-clock round immediately before the paper runtime fixes its observation anchor, fail
closed when valid fresh evidence is unavailable. The owner conditionally accepts the documented residual risk for
the local, supervised, paper-only MVP, provided this mitigation passes its regression tests and an independent
causality review. Not an unconditional acceptance; not a claim that every clock anomaly is detectable.

## What changed
- **`MarketDataService.resync_clock()`** (`src/tsclaude/marketdata/service.py`): one `ClockMonitor` round now, under a
  new `asyncio.Lock` shared with the periodic round (`_sample_clock`). Returns True only if the round produced
  samples and is the monitor's last round; False before start, after stop, or when every sample failed (the previous
  round stays the last one and keeps ageing; it is never relabelled).
- **Anchor gate** (`src/tsclaude/runtime/paper_runtime.py`, `_anchor_step`), every poll while the run has no anchor:
  - no hook: blocked `clock_resync_unavailable` for the whole run;
  - otherwise one background request at a time (`paper-clock-resync` task; the consumer keeps polling), published as
    `clock_resync_pending` (transient: no problem raised, the last blocked code is kept);
  - failure, exception or timeout (`resync_timeout_s`, 30 s): `clock_resync_failed` with the reason;
  - success: the anchor is evaluated once, from a snapshot taken after the round (`_establish_observation`); all
    P-1 evidence checks still apply, plus the new rule: the snapshot's round must be at most
    `anchor_round_max_age_ms` (5 s) old on **both** clocks (`clock_round_not_fresh`);
  - any outcome without an anchor waits `resync_retry_s` (15 s) doubling per consecutive failure up to
    `resync_retry_max_s` (240 s) before the next request; diagnostics published under `observation.resync`
    (`available`, `pending`, `attempts`, `consecutive_failures`, `last_error`, `retry_in_s`);
  - `stop()` cancels a pending request; every monitoring start resets the gate and requests its own round.
  - While blocked, every bar is processed unobserved (unchanged P-1 behaviour): no entry fills or is created,
    resting SL/TP still resolve.
- **Settings** (`PaperSettings`, optional `configs/paper.yaml` keys, validated): `anchor_round_max_age_ms`,
  `resync_timeout_s`, `resync_retry_s`, `resync_retry_max_s`. Risk thresholds (`risk.yaml`) unchanged.
- **Wiring** (`src/tsclaude/runtime/supervisor.py`): passes the service's `resync_clock` (or None: fail closed).
- **N-4:** the served `LIMITATIONS` text now states the fresh-round rule, the same-clock assumption (no switching
  midway, unverifiable 500 ppm) and the undetected cases with "no fixed bound".
- `_establish_observation(..., bound_only=True)` keeps the bound testable on its own (tests only).
- Not changed: `src/tsclaude/contracts/`, `MarketSnapshot`/`ClockContinuity`, journal format and serialization,
  `PAPER_VERSION`, `observation_bound`, `ClockMonitor`, risk config, frontend, startup backlog, E-4(b).

## Residual risk (NB-1, reduced, not removed)
The round-to-anchor interval shrinks from up to the round's maximum age (15 min; periodic rounds every 5 min) to the
round's own duration plus about one poll (a few seconds; at most 5 s of round age is accepted). Still undetected,
and each can count a minute that had already started as observed, by an amount that grows with the anomaly (no
fixed error bound; closure review N-3: 677 ms for 1 s anomalies, 1,963 ms for 2 s ones):
- a wall step and a roughly compensating monotonic suspend (or the reverse) inside the round-to-anchor interval;
- an anomaly that freezes or moves both clocks together (a VM pause or live migration, a slew shared by both);
- a host clock outside the 500 ppm assumption (not verifiable by the runtime);
- an exchange `serverTime` not stamped while the request was in flight, or different exchange clocks for spot and
  USD-M (unchanged assumptions).
The anchor evidence is still not journaled (journal format unchanged by scope).

## Tests
New or changed (fakes and synthetic data, no network):
- `tests/integration/test_d21_fresh_clock_round.py` (new): pending then anchor from the finished round; REST failure
  (exception) and empty round, one problem, retries, recovery; timeout; no hook; fresh round but the snapshot's round
  6 s old (`clock_round_not_fresh`), wall step back and monotonic suspend after the round (`clock_discontinuity`),
  step inside the round; inclusive 5,000 ms limit on either clock (and the bound alone still accepting it);
  backoff doubling and cap; stop cancels a pending round and the next start requests its own; served limitations.
- `tests/integration/marketdata/test_mdi_service.py`: `resync_clock` through the real service and `ClockMonitor`:
  False before start and after stop, a fresh 0 ms round after 10 min, all samples failing keeps the old round ageing,
  every post-start round serialized by the lock.
- Existing tests adapted to the new gate (intent unchanged): the rigs retry after one poll; observations sampled
  after the transient pending state; the bound-only cases marked `bound_only=True`; the expected observation includes
  `resync`; the fake market data gained a controllable `resync_clock`.

Results: see [Validation](#validation).

## Validation
All on the reviewed bytes (SHA-256 prefixes below), Windows 11, Python 3.13.2, pytest 9.1.1, the project `.venv`
interpreter (`uv run` fails on this machine), mypy with a fresh cache in `%TEMP%` (the repository's `.mypy_cache` is
corrupt). Logs are scratch files in `%TEMP%`, not in the repository.
- Focused, replay-parity and restart set (14 files: the D-21, P-1, market-data service, unit market data, observation
  clock, paper observation, sim property regressions, paper restart, MVP-2/2.1/3 paper, paper API, API and server
  lifecycle tests): **409 passed** (31.75 s, `tsc-d21-focused.txt`, written after the last source edit).
- `test_d21_fresh_clock_round.py` + `test_p1_observation_anchor.py` five times in a row: 45 passed each run.
- Full non-live suite (`pytest tests -m "not live"`): the first run stopped at about 14 % with the known native
  Windows crash `0xC0000005` inside `tests/property/research/test_research_stats_properties.py` (outside this
  change; not investigated, by instruction); the single authorized retry gave **1513 passed, 1 skipped (symlinks not
  permitted), 2 deselected (live), exit 0** (617 s, `tsc-d21-full-retry.txt`).
- `ruff check src tests`, `ruff format --check`: clean. `mypy src`: no issues in 116 files.
- Scope: `git diff b4f8d01 --stat` touches 14 tracked files (6 docs, 3 source, 5 tests; plus the new test and this
  devlog, untracked); nothing under
  `src/tsclaude/contracts/`, `src/tsclaude/sim/`, risk config, `configs/`, `frontend/` or `scripts/`; no
  `PAPER_VERSION`, journal or startup-backlog change; `refs/preserve/e4b-2026-11-04` still `07f3023`.

Reviewed bytes (SHA-256, first 16 hex): `service.py` e09cf44f5ede5d86, `paper_runtime.py` e3398e987ff1161a,
`supervisor.py` 651cfc0f1b73c93c, `test_d21_fresh_clock_round.py` 99f84d4594c1cc91, `m1t2_fakes.py` 40b66b4babe0d1ba,
`test_mdi_service.py` f2adc0f4916d3abd, `test_p1_observation_anchor.py` 19328673fd512994, `test_mvp2_paper.py`
1509edbb43ddcaf9, `test_mvp2_paper_api.py` 560c83709448b080. Re-verified unchanged after the 2026-11-04 Windows
restart.

## Independent review
`causality-auditor`, isolated context, read-only, on `git diff b4f8d01` plus the new test at the hashes above;
mutants and attack tests only in disposable copies outside the repository. A first run was interrupted (session end,
then a Windows restart) before any verdict; its mutation results were reused only after the second, scoped run
confirmed that the copies matched the reviewed hashes.

**Verdict: PASS, 0 blocking.** Causality: the anchor is evaluated only after the requested round finished with
exactly `True`, from a snapshot taken after it, with the round at most 5,000 ms old on both clocks; `observation_bound`
is unchanged and the gate only adds restrictions. Every failure mode (no hook, False, exception, timeout, cancelled,
pending, not fresh, discontinuity) fails closed with its own code; retries are bounded; rounds are serialized by the
lock; stop cancels and every start requests its own round. Documentation states the residual risk without claiming
elimination or a fixed bound.

Mutants (30): 26 killed by the existing tests; m5 (anchor from the poll's snapshot) and m15 (missing
`round_age_ms` accepted) are equivalent today; m12 (a cancelled round counted as success) and m8c (`resync_clock` True
while not running) survive as test gaps. Attack tests: 3 passed; the VM-pause attack (3 s pause freezing both
clocks, anchor 2,500 ms inside a started minute) fails **as expected**: it demonstrates the documented residual, not a
defect.

Non-blocking findings (open, not remediated in this task):
- **F1** the cancelled-round path is untested (the fake cannot raise `CancelledError`): add the auditor's test k12.
- **F2** the `not self._running` guard of `resync_clock` is not discriminated: add test k8c.
- **F3** the `round_age_ms is None` branch is unreachable today (defensive); optional test k15.
- **F4** re-taking the snapshot after the round is equivalent today but protects against a future `await` before the
  anchor step: keep it, add a comment.
- **F5** `start()` after a halt without `stop()` drops a pending request without cancelling it (bounded by the 30 s
  timeout, no causal impact): cancel it in `_reset_resync`.

NB-1 eligible for closure under D-21: **yes** (auditor and Lead). **Closed** by the owner on 2026-11-04; the residual
risk stays as stated above, explicitly accepted for the local, supervised, paper-only MVP.

Tracking of the non-blocking findings (kept open; the reviewed implementation was not changed for them):
- F1 and F2: future targeted regression tests (k12: a hook raising `CancelledError`; k8c: `resync_clock` with
  `_running` False must not sample).
- F5: bounded lifecycle cleanup (cancel a dropped pending request in `_reset_resync`).
- F3 and F4: no action required (defensive branch; keep the snapshot re-take, optional comment).
