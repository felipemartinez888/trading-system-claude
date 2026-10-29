# ADR-0013: Exchange-wide interruptions are registered non-trading periods, not missing data

- **Status:** Accepted in principle (owner decisions D-09/P-1 and D-10/P-2, 2026-10-28). Implementation (E-1) is
  **not yet authorized**, and its execution semantics depend on D-12 (P-4). Until it is implemented, the current
  rule applies unchanged: any missing 1m bar in a backtest range gives grade X (`configs/backtest.yaml`
  `max_missing_share: 0.0`).
- **Relates to:**
  - ADR-0004 (canonical 1m bars), ADR-0005 (one simulator), ADR-0009 (holdout guard);
  - RESEARCH_PROTOCOL §6 (grades);
  - ADR-0014 (zero-volume candles).
- **Evidence:** `docs/devlog/2026-10-27-m2-handover-reconciliation.md` and
  `docs/devlog/2026-10-28-m2-policy-design.md`.

## Context
- **The 2023-03-24 signature.** On 2023-03-24 every spot symbol in the canonical lake (BTC, ETH, SOL, DOGE, XRP;
  imported 2026-10-24) and in batch B03 shows **the same** sequence, read from the published monthly 1m archives:

  | UTC | What the archive contains |
  |---|---|
  | 11:28 – 12:38 | 71 consecutive zero-trade minutes (O=H=L=C = previous close, volume 0) |
  | 12:39 | one truncated candle, rejected as malformed, on the same line (33880) of every symbol's file. Its close time is cut short by a different amount per symbol (+41.6 s BTC, +42.6 s DOGE, +43.1 s ETH, +46.9 s SOL, +48.3 s XRP, instead of +59.999 s) |
  | 12:40 – 13:59 | no candles (80 minutes; 81 missing including the rejected 12:39) |
  | 14:00 | trading candles resume |

- **External corroboration.** Binance halted spot trading that day after its matching engine hit a bug on a
  trailing-stop order. The restart was staged (cancel-only until about 13:40 UTC), and trading resumed at 14:00
  UTC. Sources: TechCrunch 2023-03-24, Forklog, FinanceFeeds, Kitco.
- **The problem with the current rule.** Under `max_missing_share: 0.0`, every backtest whose range or warm-up
  contains this date is graded X. That includes much of the D-05 development segment, for every symbol. The
  rule cannot tell an exchange that was closed from a dataset that lost data.
- **Ordinary missing data must stay a failure.** The same audit found:
  - isolated one-minute holes that are **import defects** (the row exists in the verified source file but is
    absent from the canonical lake: XRP 2025-04-03 17:24, SOL 2022-12-21 15:23);
  - one hole that is genuinely absent from the source file (SOL 2023-02-14 16:39).

  None of these is an exchange interruption, and none may be waived by this ADR.
- **What must be avoided:**
  - choosing backtest ranges that avoid the date (cherry-picking);
  - raising the tolerance, which would also hide real data loss;
  - interpolating candles (fabrication);
  - per-run waivers, which are not reproducible.

## Decision (proposed)

### 1. Raw data is never changed
The lake keeps exactly what the exchange published: the zero-trade candles, the absent minutes and the record
of the rejected malformed row. Nothing is filled, removed or re-timed.

### 2. A versioned registry of exchange interruptions
- **File:** a new, versioned file `configs/exchange_interruptions.yaml`. Each entry has:
  - `id`, `market` (spot | usdm), `from`, `to` (UTC, `[from, to)`, minute-aligned);
  - `scope: exchange-wide`;
  - `signature`: the measured evidence (symbols checked, identical boundaries, missing and zero-trade minutes
    per symbol, source file SHA-256 and line numbers);
  - `external_sources` (at least one reputable report of an exchange-wide halt);
  - `approved_by` (owner) and `approved_at`.
- **Reproducibility.** The file's hash is recorded in every run manifest, like `config_hash`, so a run is
  reproducible and a registry change is visible.
- **Admission rule, applied to the whole lake and never to one backtest.** An interval may be registered only
  if all of the following hold:
  1. A deterministic scan finds the same pattern (absent minutes and/or zero-trade minutes with **identical
     start and end minutes**) in **every** symbol of that market available for the period, with at least 5
     symbols. Each must be a separate dataset whose source archives are SHA-256-verified.
     - **Holdout:** the scan reads only timestamps and counts. For sealed holdout months it goes through the
       holdout guard and never reads or outputs prices (ADR-0009, L-07).
  2. The absence is confirmed **at source level**: the rows are missing from (or zero-trade in) the verified
     source archives, not only from the lake.
  3. An external source documents an exchange-wide halt that overlaps the interval.
  4. The owner approves the entry **before** any result that uses it is examined. Entries are never added or
     edited because of a backtest outcome.
- **Interval bounds come from the data**, not from the news report:
  - `from` = the first minute of the market-wide zero-trade or absent run;
  - `to` = the first minute in which trading resumes in all symbols.
- **Market scope.** A spot halt does not imply a USD-M halt. Each market needs its own evidence.

### 3. How a backtest treats a registered interval
- **Integrity accounting:**
  - Minutes inside a registered interval are **not** counted as `missing_minutes` or
    `missing_warmup_minutes`.
  - They are reported separately (`exchange_downtime_minutes`, with the registry ids that intersect the range).
  - Every other missing minute keeps the existing rule (grade X at tolerance 0.0).
- **Strategy history:** the bars inside the interval reach strategies exactly as live would receive them.
  - The zero-trade filler candles form ordinary buckets.
  - The absent minutes break the history through the existing incomplete-bucket reset.
  - Nothing is bridged or synthesized, so features never join bars across the absent minutes. Backtest and live
    therefore see the same history.
- **No trading inside the interval:**
  - No entry or exit fills during `[from, to)`, including on its zero-trade candles. A strategy decision
    inside the interval cannot become a fill there.
  - An entry scheduled to execute inside the interval executes at the first traded open after `to` if it is
    still valid. Otherwise it is cancelled; `valid_until` is never extended.
  - **Open positions, aligned with L-11:** an exposed position is closed at the first traded open after `to`
    and flagged `spans_exchange_downtime`. L-11 is the live rule for a minute Binance never published, so live
    and backtest treat a closed market the same way.
- **Flagged trades:** trades flagged `spans_exchange_downtime` count toward the existing flagged-share rule.
  Above `flagged_share_cap` (5 %, evaluated per flag in `research/report.py`), the grade is capped at C.
  Proposed: the flag counts into the existing `spans_gap` share rather than adding a separate share (decision
  P-1).
- **Funding** (USD-M) is applied as published.
- **Reports** list every registered interval in range, together with its evidence id.

### 4. Live and paper (parity depends on ADR-0014)
- **Absent minutes are already handled.** Live and paper treat them with the existing fail-closed rules
  (unobserved and missing minutes, AWAITING_BACKFILL, and the L-11 operator action for a minute Binance never
  publishes).
- **Zero-trade filler candles are not handled today.** They are fresh and observed, so nothing in sim, risk or
  the runtime blocks an entry or fill on them. Paper, which shares the simulator, would fill on them while this
  ADR's backtest refuses.
- **So parity requires ADR-0014 rule 4** (no fills on zero-trade candles), implemented in the shared simulator.
  **P-1 must therefore be approved and implemented together with P-4.** Otherwise this ADR would create a
  backtest/paper difference (ADR-0005).
- **The registry is historical only.** It is never used to waive live data problems.

### 5. Initial entry (for approval)
| id | market | from | to | Minutes | Evidence |
|---|---|---|---|---|---|
| `binance-spot-2023-03-24` | spot | 2023-03-24T11:28Z | 2023-03-24T14:00Z | 152 (71 zero-trade + 81 absent) | 5/5 canonical symbols plus B03 identical; malformed line 33880 in every file; external reports of the halt and 14:00 UTC resumption |

**The 71 zero-trade minutes** (11:28–12:38) are inside the interval because the admission rule takes `from`
mechanically from the market-wide zero-trade or absent run. The data supports this: the run is identical in
every symbol, every candle is flat at the previous close with all volumes 0, and it is the only zero-trade run
of 3 or more minutes in about 4.75 years of these liquid symbols. The owner can still choose to admit only the
absent part, [12:39, 14:00); that is decision P-2.

## Consequences
- **Gains:** development-segment backtests over 2023 become gradable without hiding real data loss. The waiver
  is objective, versioned and visible in every manifest and report.
- **Implementation it needs** (not authorized by this proposal):
  - the registry loader and its manifest hash (research);
  - the integrity accounting in `research/runner.py`;
  - the no-fill window and the frozen-position resolution in the shared simulator (sim, with a version bump and
    golden tests);
  - QA and report fields;
  - a deterministic candidate scanner;
  - tests: a canary that a non-registered gap still gives X, and a registry change changing the manifest hash.
- **Trials and results:** existing runs (none locally today) keep their grades. Runs recorded before the
  registry exists cannot be regraded silently: a recompute must record the registry hash.

## Alternatives rejected
- **A non-zero missing-data tolerance:** it would also accept import defects such as the XRP and SOL rows above.
- **Excluding dates from ranges per run:** cherry-picking, and not reproducible.
- **Interpolating or forward-filling candles:** fabricated data.
- **Deleting the zero-trade candles:** destroys published data. ADR-0014 classifies them instead.
