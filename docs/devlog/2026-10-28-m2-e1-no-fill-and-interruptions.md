# M2-E1: no simulated fills on zero-trade candles; exchange-interruption registry

- **Authorization:** owner message "OWNER O-4 RESULTS — PASS / E-1 IMPLEMENTATION AUTHORIZATION" (2026-10-28):
  D-12/P-4 approved for implementation, subject to validation.
- **Brief:** [`docs/tasks/M2-E1.md`](../tasks/M2-E1.md). ADRs: [0013](../adr/0013-exchange-wide-interruptions.md),
  [0014](../adr/0014-zero-volume-candles.md) rule 4 and rule 5.
- **Branch:** `task/m2-e1` in an isolated git worktree, from `c327bbd`.
  Kept uncommitted until the owner reviewed this report; the owner then approved one local commit
  (2026-10-29). No merge, cherry-pick or push.
- **Not touched:**
  - canonical data, `data_batches/`, the 2026 holdout and its guard;
  - acceptance classifications (B01–B12), the QA rules and the E-2 open exception;
  - `contracts/`;
  - no historical backtest was run; every number below comes from synthetic test fixtures.

## Live evidence the semantics rest on (O-4)
O-4 PASS (owner run, `evidence/owner_checks/o4_20261029T010705Z/`; details in the
[O-4 devlog](2026-10-28-o4-owner-capture.md#results-owner-run-2026-10-29t0107z-utc)). The live WebSocket delivers
a closed zero-trade candle (`n = 0`, flat at the previous close, class Z1) for each minute without trades, exactly
like the archive and REST: 70 of 70 REST zero-trade minutes arrived as closed candles, with no mismatch.

So one rule, applied to the published candles, gives the same behaviour in backtest and paper. Live needs no
special handling of zero-trade minutes.

## Execution semantics (sim-v1)
The rule lives in the shared `Book` (`sim/book.py`), so backtest and paper cannot diverge (ADR-0005).

**A candle is *frozen*** when `trades == 0` (Z1 genuine zero-trade, or Z2 outage filler), or when its open
lies inside a registered exchange interruption (backtests only, below). On a frozen candle:

| Situation | Behaviour |
|---|---|
| Pending entry, any kind | Not evaluated: no fill, no trigger. The candle does not count as a bar the order has seen, so at the next traded candle the order is resolved as a **resting** order with the existing rules. |
| Market entry scheduled at that open | Executes at the open of the next candle with trades. |
| Limit entry | Fills at the next traded candle only if that candle reaches the limit, at the limit price as a maker (existing resting-order rule; a gap through the limit gives no price improvement). |
| Stop entry | Triggers only on trades. A gapped next open fills at the worse of open and stop (existing gap-fill rule). |
| Validity | Never extended. If the frozen candle's close reaches `valid_until`, the order expires with reason `valid_until_passed:no_trade` (or `…:exchange_downtime`). An expired order never executes. |
| Open position: stop / take-profit | Not evaluated on a frozen candle (a frozen price cannot touch a level). At the next traded candle a gap through the stop fills at the worse open. |
| Open position: exit signal or time exit due | Waits for the next traded open; the trade is flagged `deferred_no_trade`. |

**What counts as deferred** (review fix B1): an entry is flagged `deferred_no_trade` only when the usual
rules would have filled it on the frozen candle's prices. This is a side-effect-free dry run of the same
`resolve_entry`. A market order always qualifies; a limit or stop that the frozen price never reached does
not. A due exit always qualifies. The book counter `no_trade_deferrals` counts each order once (entry) and
each position once (exit), not every frozen minute it waited.

**Registered exchange interruption** (backtests only; ADR-0013, D-09/D-10):
- **Coverage:** every candle with its open in `[from, to)` is frozen, even one that looks traded.
- **Pending entries** rest through the interval and carry `spans_exchange_downtime` and `spans_gap` (so the
  resulting trade counts in the flagged-share cap, even when the interval is made only of filler candles).
  They execute at the first traded open after it only if `valid_until` has not passed; otherwise they are
  cancelled.
- **Exposed positions:** a position open when the interval starts is flagged `spans_exchange_downtime` and
  `spans_gap`. It is closed at the first traded open after the interval, with exit reason `exchange_downtime`
  (aligned with L-11).
- **Recorded:** `RUN_STARTED` lists the registry ids the simulator used.

**Raw data is never altered:** no candle is removed, synthesized or repriced. The strategies receive exactly the
bars that were published, and an absent minute still breaks history through the existing reset.

## Registry, versions and manifest
- **`configs/exchange_interruptions.yaml`** (schema 1) holds one entry, `binance-spot-2023-03-24`, spot
  `[2023-03-24T11:28Z, 2023-03-24T14:00Z)`.
  - It records the signature evidence: the 5 symbols, identical boundaries, the 71-minute zero-trade run, the
    truncated line 33880 and the 81 absent minutes.
  - It cites 3 external sources and `approved_by: owner (D-10, 2026-10-28)`.
- **`quality/interruptions.py`** validates the registry and fails closed on any problem, raising
  `InterruptionRegistryError`. It checks:
  - schema 1, unique ids, a known market and `exchange-wide` scope;
  - that the signature, external sources and approval are present;
  - ISO-8601 timestamps ending in `Z`, minute-aligned, with `to` after `from`;
  - that no two entries overlap within a market.

  The backtest CLI refuses the run (exit code 5) when the registry file is malformed.
- **Missing file and the library default both mean an empty registry:** registered minutes then count as
  missing again and the run grades X. This is the fail-closed direction.
- **Versions:** `SIM_ENGINE_VERSION = "sim-v1"` and `RUNNER_VERSION = "runner-v2"`.
- **Manifest:** `exchange_interruptions` (`schema`, `source`, `sha256` of the file bytes, `entries`) is a
  required manifest field. Changing the registry changes the manifest hash. `source` is the file name only,
  so the hash does not depend on whether `--config` was typed relative or absolute, and no local directory
  is written into artefacts (review fix N4).

## Runner integrity and reports
- **Missing-minute split:** missing minutes are split per market. Only the part inside a registered interval of
  that market is reported as `exchange_downtime_minutes_in_range` (or `…_in_warmup`), together with
  `exchange_interruptions_in_range`.
  - Every other missing minute still counts in `missing_minutes_in_range`.
  - `exchange_interruptions_in_range` and `exchange_interruptions_in_warmup` list every registered interval
    that intersects `[from, to)` or the warm-up for the run's markets, whether its minutes are absent or
    published as fillers.
  - `max_missing_share` stays 0.0, so the zero tolerance for unregistered gaps is unchanged.
- **Zero-trade share (ADR-0014 rule 5):** `zero_trade_minutes_in_range`, `zero_trade_share` and
  `zero_trade_minutes_by_symbol`. Published zero-trade candles are never counted as missing.
- **Trade statistics:** `deferred_no_trade_count`, `deferred_no_trade_share` and `exchange_downtime_count`. Every
  flagged trade is counted; none is removed.
- **Flagged-share cap:**
  - Trades exposed to a registered interruption also carry `spans_gap`, so they count in the existing 5 %
    flagged-share cap, which gives grade C.
  - **Decided (D-12A, owner, 2026-10-29):** trades flagged only `deferred_no_trade` are reported but **not**
    added to the cap. The original decision point read: they are reported but **not** added to
    that cap. They are realistic execution (the fill waited for a counterparty), not a data defect. If the owner
    prefers, they can be capped like `spans_gap`; that is a one-line change plus a golden update.
- **Paper API** (read-only): each book in the paper snapshot carries `no_trade_deferrals`.
- **UI** (read-only), so no affected trade is presented unqualified:
  - the Paper trades table shows "waited for a traded candle" and "spans exchange downtime";
  - the Backtests page shows the deferred share, the exchange-downtime minutes and the zero-trade share.

## Paper consequences
- **The zero-trade rule applies to paper**, through the shared `Book`. A test drives the real paper path
  (`ScriptedStrategy` → `drive` → `record`) and checks that the fill waits for the traded minute and that verified
  replay reproduces identical ledger hashes.
- **The registry is backtest-only (ADR-0013 §4):** live and paper journals stay deterministic and replayable.
  A real live outage is handled by the existing fail-closed live rules and L-11.
- **Existing paper sessions:** paper restore uses verified replay, which compares the journalled simulator
  events exactly, including `engine_version`. A journal written by sim-v0 therefore no longer replays, and
  restore fails closed: paper shows `UNAVAILABLE` and the owner starts a new session.
  - This follows from the code; it was not exercised against the owner's real journal.
  - RUNNING paper has not yet been checked in the real app (ROADMAP), so the expected impact is small.

## Pinned values updated (each verified as version-only or additive)
| Pin | New value | Why it changed |
|---|---|---|
| `test_paper_session.PINNED_JOURNAL_HASH` | `4bc1e0b8…` | only `engine_version` changed in the journal |
| `test_sim_determinism.PINNED_FINGERPRINT_SEED0` | `93ecca10…` | version plus three zero-valued summary counters in `RUN_COMPLETED`; the other 1 349 events are identical |
| `test_bt_runner.GOLDEN_MANIFEST` | `f77a88bb…` | runner-v2 / sim-v1 and the empty-registry manifest field |
| `test_bt_runner.GOLDEN_METRICS` | `ef8abc59…` | zero-valued new metrics only: removing `exchange_interruptions_in_warmup` gives `0b96116b…`, and removing the three zero-trade keys from that gives the value before E-1's reporting additions, `b17de44f…` |
| `test_bt_runner.GOLDEN_TRADES` | `2dff9ce6…` | two new bool columns `deferred_no_trade` and `spans_exchange_downtime` (all False in the fixture); rewriting the same rows without them gives the previous `39ca0329…` byte for byte, so no trade changed |

## Tests
New tests:
- `tests/unit/sim/test_e1_no_trade_and_outage.py` (17) covers:
  - market, limit and stop entries across zero-trade candles;
  - validity expiry;
  - a stop not hit on a frozen candle;
  - a deferred exit;
  - no new fields without a deferral;
  - an outage close at the first traded open;
  - no fill inside an outage;
  - an entry expiring inside an outage;
  - a stop entry the frozen price never reached is not reported as deferred (B1);
  - a stop crossed by a traded-looking candle inside an interval does not fill;
  - a position opened after an interval is not closed by it;
  - an interval made only of fillers still flags `spans_gap` for held and pending trades (B2);
  - no change without a registry;
  - stepped versus historical parity;
  - the live paper path and replay.
- `tests/unit/backtest/test_bt_exchange_interruptions.py` (20 collected) covers:
  - registered downtime versus X;
  - a partial cover still giving X;
  - another market;
  - an empty registry;
  - the manifest hash;
  - the zero-trade share;
  - warm-up downtime, and an unregistered warm-up gap still giving X;
  - the reported interval ids filtered by market;
  - the fail-closed `RunEnv` default (empty registry);
  - the per-trade flag columns in `trades.parquet`;
  - the repository registry;
  - invalid, overlapping, missing and malformed registries.
- `tests/unit/backtest/test_bt_report.py` (+1) covers flag counts and the cap through `spans_gap`.
- `tests/unit/backtest/test_bt_cli.py` (+1) covers a malformed registry refusing the run (exit 5, no run
  directory).

Results: see the [test evidence](#test-evidence) section.

## Test evidence
All runs are local on the owner's Windows machine (Python 3.13.2, main `.venv`, worktree `src` on
`PYTHONPATH`), on the final code, 2026-10-29. No CI run exists for this branch.

| Check | Result |
|---|---|
| Final regression, chunk A: `tests/unit/sim` | **295 passed** (12.9 s) |
| Final regression, chunk B: `tests/unit/backtest` (includes the golden-hash test and the CLI) | **63 passed** (99 s) |
| Final regression, chunk C: `tests/unit/research`, `strategies`, `risk` | **235 passed** (253 s) |
| Final regression, chunk D: paper integration (`test_mvp2_paper`, `test_mvp21_paper_hardening`, `test_mvp3_reference_paper`, `test_mvp4_paper_api`, `test_mvp2_paper_api`) and `test_o4_zero_trade_capture` | **74 passed** |
| **Final regression total** | **667 passed, 0 failed** (`-m "not live"`) |
| E-1 focused set (the two new files, `test_bt_cli`, `test_bt_report`) | 51 passed |
| Frontend: `PaperPage.test.tsx`, `BacktestsPage.test.tsx`; `tsc --noEmit` | 13 passed; tsc clean |
| `ruff check --no-cache`, `ruff format --check` | clean (211 files) |
| `mypy src` (scratch cache) | no issues (115 files) |

- **The regression ran in four chunks.** Two single-process runs had been cut short when the PC shut down: one
  had no output, the other only partial dots. Neither is counted.
- **One transient abort.** A focused run once ended in a bare interpreter traceback with no pytest summary. The
  immediate rerun of the same command passed 51/51. This fits the open hardware (WHEA) instability; it is
  recorded, not explained.
- **Golden hashes and parity** are in chunks A and B:
  - `test_golden_hashes_identical_across_platforms`, with each pin verified as additive (table above);
  - `test_paper_and_backtest_resolve_zero_trade_candles_identically`;
  - `test_the_live_paper_path_defers_the_same_fill_and_replays_identically`;
  - the pinned paper journal and sim fingerprint.
- **Mutation sensitivity:** each safeguard fails at least one test when removed (scratch copies only). The
  mutants checked:
  - no freeze on `trades == 0`;
  - no freeze inside an interval;
  - validity expiry removed, or validity extended;
  - `split_missing` ignoring the market;
  - registered minutes counted as missing;
  - the CLI ignoring a malformed registry;
  - closing one bar before the interval;
  - no `_hold_position`;
  - no `spans_gap` for held or pending trades;
  - hidden warm-up gaps;
  - evidence fields not required;
  - a deferral counted without a would-fill;
  - the exit scheduled before the interval ends (M11);
  - `ids_overlapping` ignoring the market (M16);
  - the flag columns missing from `trades.parquet`.
- **Defensive only:** not counting `bars_seen` on frozen candles has no effect a test can see, because arrival
  also requires `open == earliest_exec_ms`.

## Reviews
**Independent code review** (reviewer agent, fresh context): verdict *changes required*, with 2 blocking and 8
non-blocking findings. Resolution:

| Finding | Resolution |
|---|---|
| **B1 (blocking)** `deferred_no_trade` overcounted: any resting order was flagged on a frozen candle | Fixed. Flagged only when the dry-run `resolve_entry` would have filled; each order or position counted once. Test: `test_an_entry_the_frozen_price_never_reached_is_not_reported_as_deferred` |
| **B2 (blocking)** outage semantics survived mutants M5 / M6 / M7 | Fixed. Three tests: a stop crossed inside an interval, a position after an interval, a fillers-only interval flags `spans_gap` |
| N1 warm-up split untested | Fixed. Two warm-up tests |
| N2 pending trades in an interval without `spans_gap` | Fixed. Pending entries set `spans_gap` too |
| N3 CLI refusal untested | Fixed. CLI test (exit 5, no run directory) |
| N4 manifest depended on the typed path | Fixed. `source` is the file name only |
| N5 interval ids listed only from missing minutes | Fixed. `ids_overlapping` over `[from, to)` and the warm-up separately |
| N6 deferral counter per minute, not in paper | Fixed. Counted once per order or position; `no_trade_deferrals` in the paper snapshot |
| N7 metrics row missing; §6 wording applied | Metrics row added. The §6 wording is brief deliverable 7, applied in this change, which the owner reviewed before approving the commit (revertible) |
| N8 same-bar time exit reported as `exchange_downtime`; open exposed positions at the end not counted | Accepted as is (cosmetic; the same pattern as `spans_gap`) |

**Causality and safety audit** (causality-auditor agent, on the code after the review fixes): **PASS, no
blocking findings.**
- **What it confirmed:**
  - no look-ahead: the exit happens only at or after the interval end, and the registry never reaches
    strategies;
  - no optimistic fills: waiting orders are resolved as resting orders, gaps fill at the worse price, and
    validity is never extended;
  - integrity fails closed;
  - paper does not use the registry;
  - the holdout is untouched.
- **Non-blocking findings:**
  - **N1, owner decision:** missing bars in the zero-trade part of the 2023 interval (11:28–12:38) would count as
    downtime. This follows from P-2, which admitted those 71 minutes. Options:
    - an `absent_from` boundary per entry;
    - narrowing the waiver to `[12:39, 14:00)`.
  - **N2:** per-trade flags were not in `trades.parquet`. Fixed: explicit bool columns.
  - **N3:** the market filter in `ids_overlapping` was untested. Fixed.
  - **N4, owner decision:** the loader does not require `approved_at` and does not limit interval length.
    Proposal: require `approved_at`, and refuse intervals longer than 24 h without an explicit override.
  - **N5:** M11 and `bars_seen` had no effect a test could see. M11 is fixed (now detected); `bars_seen` is
    documented as defensive.

## Owner decisions of 2026-10-29 and proposed E-1b
The owner approved the E-1 local commit and decided the three open policy points (DECISIONS_PENDING D-09A, D-10A,
D-12A). They are recorded here. Any code beyond the reviewed E-1 diff is **proposed, not implemented**, as the
owner asked.

| Decision | Status in E-1 | Proposed E-1b change (not implemented) | Test requirements |
|---|---|---|---|
| **D-12A:** waits stay outside the 5 % cap, under conditions | Met: the delay is recorded per trade (`deferred_no_trade` in events, `trades.parquet`, reports, UI); the real next traded price is used; validity is never extended; no optimistic fill (causality audit PASS); interruption trades keep `spans_gap`. Independent evaluation is possible from the `trades.parquet` column | `report.breakdowns` gains a `deferred_no_trade` split (n, win rate, expectancy R, net PnL for deferred versus not). Optionally a per-trade `deferred_wait_ms` (first frozen candle to fill) | Hand-checked breakdown on a fixed trade set; a run without deferrals gives an empty deferred bucket; golden metrics update recorded as additive |
| **D-10A:** keep `[11:28, 14:00)`; distinguish observed zero-trade from absent minutes | Partly met: absent minutes are reported as `exchange_downtime_minutes`, observed zero-trade candles in `zero_trade_minutes_*`; raw data untouched. **Gap** (audit N1): a missing bar in 11:28–12:38 would count as downtime | Optional per-entry `absent_from` (for 2023: `2023-03-24T12:39:00Z`). Missing minutes in `[from, absent_from)` count as **missing** (grade X; those candles were published). Missing minutes in `[absent_from, to)` are downtime. The simulator freeze still covers the whole interval. The integrity block gains `exchange_observed_zero_trade_minutes_in_range` | A missing bar at 11:30 gives X with "data gaps"; a missing bar at 13:00 gives downtime; counts split exactly; `absent_from` outside `[from, to)` or unaligned fails closed; the repository registry test updated |
| **D-09A:** approval date required; > 24 h fails closed unless an exceptional override | Not met (audit N4) | Required `approved_at` (ISO date) on every entry; duration > 24 h raises unless `exceptional_override: {approved_by, approved_at, reason, reference}` is present. Add `approved_at: "2026-10-28"` (the D-10 date) to the 2023 entry. The registry file's SHA-256 changes (recorded in manifests); the golden run uses the empty registry and is unaffected | Missing or invalid `approved_at` fails; exactly 24 h passes; 24 h + 1 min fails; the same with a complete override passes; an incomplete override fails |

Scope of E-1b: `quality/interruptions.py`, `research/runner.py`, `research/report.py`,
`configs/exchange_interruptions.yaml` and their tests. No simulator change, no data or holdout access. It needs
the owner's approval before any code is written.

## Open items and next approvals
- **Owner review of this implementation:** then a local commit of `task/m2-e1` (no merge, no push).
- **Owner decisions raised by the review:** decided on 2026-10-29 (D-12A, D-10A, D-09A). Their code is the
  proposed E-1b above, awaiting approval.
- **After integration:** sim-v0 paper journals do not replay, so the owner starts a new paper session.
- **Integration with `task/m2-e3`** (`e5a5e2e`): separate branches; no merge without approval.
- **USD-M archives** follow the same zero-trade convention: still unverified (ADR-0014 "Still to verify").
- **Not authorized:** E-2 (QA open exception), C-1 parity imports (O-5/O-6), D-15 and D-17, and any historical
  backtest.
