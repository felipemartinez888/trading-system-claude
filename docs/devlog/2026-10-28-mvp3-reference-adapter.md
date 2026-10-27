# Devlog — 2026-10-28 (local time UTC-6) — MVP-3: live `bracket_reference` adapter (paper only)

- **Branch:** `feat/mvp-3-reference-adapter`, cut from `11fd12e` (the MVP-2.1 commit; verified in the object
  database; the `25a6e41` in an earlier summary was a reporting error and does not exist).
- **Approvals:**
  - M3-A: the brief.
  - M3-B: the implementation.
  - Option 1: the 9-day startup history.
  - M3-C: **one local commit** of MVP-3 plus Option 1. No merge, no push, `main` not moved.
- **Status:** MVP readiness only; no M3 or M4 acceptance is claimed.
- **Hardware:** focused tests only; results are provisional (WHEA under investigation).

## What it does
- **`src/tsclaude/strategies/live_adapter.py`, `ReferenceLiveStrategy`** (the paper engine's
  `LiveStrategy`):
  - It reuses `research.source.StrategySource` unchanged: the same 1m→15m aggregation, the same history reset
    after an incomplete bucket, the same trailing window ending at the decision bar, and the same planning
    costs.
  - Only **CONFIRMED** proposals become intents. Each is one `SignalRecord` with:
    - a deterministic ULID;
    - decision time = created time = the close of the 1m bar completing the 15m bar;
    - `valid_until` identical to the backtest request;
    - `evidence_grade` "X" and no preregistration;
    - the rationale and `market_conditions` labelled REFERENCE / NOT_VALIDATED, written last so no feature
      name can replace them.
  - **WATCH** proposals are counted, never emitted.
  - **Feeding:**
    - Minutes already fed are skipped (`skipped_not_newer`).
    - Bars before `trade_from_ms` only build history.
    - Bars of non-session instruments and non-1m bars are ignored.
  - **Fail closed:**
    - the runtime builds it only via `create()`, which uses default params;
    - any time exit is refused, because the paper engine cannot carry one;
    - a strategy exception follows the engine's existing `strategy_error` path.
- **`runtime/paper_runtime.py`:**
  - `PaperSettings.strategy` is `none` (the default) or `bracket_reference`; anything else is refused.
  - `_build_strategy` sets `trade_from_ms` to the start minute. It warms up from closed live-store bars in
    `[start − (window_bars + 2) × 15m, start)`, after the market-data startup backfill has finished (the MVP-2
    gate).
  - The snapshot carries the `reference` block.
- **`api/paper.py`:**
  - `/api/paper/status` shows the reference metadata (labels, counts, warm-up progress) as SYSTEM values.
  - The reference signals themselves, which hold market prices, are served separately as `reference_signals`,
    labelled SIMULATED / STALE / UNAVAILABLE by the H1 rules.
- **`configs/paper.yaml`:** still `enabled: false` and `strategy: none`; the option and its warm-up limit are
  documented.
- **Unchanged:** the risk engine. Reference intents go through `EntryRequest.from_signal`; the baseline book is
  risk-gated and the shadow book ungated by design.
- **Untouched:** sim/, risk/, contracts/, research/, marketdata/, store/, frontend, the trial registry,
  preregistrations and the holdout guard.

## Known limitations (disclosed in `LIMITATIONS`, status `reference.note` / `warmup_progress`, `paper.yaml`)
- **Warm-up length** (code review B1). **Resolved by owner Option 1**; see "Option 1" below.
  - Default `bracket_reference` needs **741 contiguous 15m bars**, about **7.8 days of unbroken 1m history**
    per instrument, before its first decision. The `ema(50)` warm-up dominates. The MVP-3 brief's estimate of
    "~116 bars / 29 h" was wrong.
  - The startup backfill now provides 9 days (`lookback_days: 9`). With a complete history the strategy is
    ready at paper start. If any minute is missing inside the ~7.8-day read, that instrument is not ready
    until it has 7.8 unbroken days again.
- **Unobserved minutes restart the warm-up** (code review B2; still open, owner decision pending).
  - The paper engine passes only observed (ws_live) bars to strategies.
  - A REST-recovered live minute or a downtime-backfill minute therefore never reaches the adapter. The
    history breaks and the 7.8-day warm-up restarts. The `history_breaks` counter and per-symbol progress
    make it visible.
  - The same minutes exist in the lake, so live can diverge from a backtest of the period.
  - Fixing it needs a decision outside MVP-3's file scope. Options:
    - treat REST_RECOVERED as observed (`PaperTradingService(observed_sources=…)`; a design change of the M3
      engine's observation rule);
    - a history-only feed for unobserved bars (needs a sim/ hook);
    - accept the gaps.
- **Smaller limitations:**
  - A store bar later replaced by its WS version keeps the store version in the adapter's history.
  - `created_at_ms` equals the decision time, not the release time; fills are still protected by `not_before`.
  - Warm-up reads up to about 11.2k bars per instrument synchronously when paper opens, one instrument at a
    time (since Option 1).
  - Conditions are recorded as observed values, not as the strategy's thresholds.
- **Parity scope:** signal generation is identical by construction and tested. Fills can differ from a backtest
  through release latency (by design) and the live risk gate.

## Reviews
**Code review: changes required.**
- **B1 (warm-up versus available history) and B2 (unobserved minutes restart it):** not fixable inside the
  approved scope. Both are now disclosed and tested
  (`test_an_unobserved_minute_restarts_the_reference_warm_up`, `test_warmup_progress_…`); each needs an owner
  decision above.
- **Non-blocking items:** recorded under the limitations above.

**Causality audit: PASS, no blocking findings.** Confirmed:
- no look-ahead, because the window ends at the decision bar;
- warm-up uses only bars closed before the start minute;
- the engine rejects any intent whose decision time differs from its bar close;
- fills are no earlier than the release;
- no WATCH-to-order path;
- the risk gate is intact;
- no research or holdout effect.

**Audit non-blocking items, fixed:**
- **N1:** signal prices moved out of the SYSTEM block.
- **N2:** the labels are written last.
- **N4:** added a mid-bucket parity test.
- **N5:** the skip counter was renamed.

**Audit non-blocking items, recorded:** N3 (`created_at`); and that the parity tests are structural because
both paths use the same `StrategySource`. The future-perturbation canary is the real look-ahead check.

## Tests (provisional; focused; `.venv` executables)
- **`tests/unit/strategies/test_live_adapter.py`:**
  - live intents versus backtest requests field by field (9 CONFIRMED, WATCH counts equal, history break
    reproduced);
  - warm-up overlap;
  - a mid-bucket start;
  - the future-perturbation canary plus prefix equality;
  - decision equals the completing bar's close on a 15m boundary;
  - no intents from warm-up or pre-start bars;
  - WATCH never emitted;
  - labels and deterministic ids;
  - fail-closed params;
  - non-session instruments ignored;
  - no network or order imports;
  - warm-up progress.
- **`tests/integration/test_mvp3_reference_paper.py`:**
  - inside the paper engine the decisions equal the backtest's;
  - every baseline entry is risk-blocked (`no_market_view`) while shadow records the hypothetical;
  - fills come after decisions;
  - default `none` and unknown strategy names fail closed;
  - causal warm-up and REFERENCE status in the runtime;
  - demo WATCH never reaches the books;
  - an unobserved minute restarts the warm-up;
  - signal prices are not served as SYSTEM values.
- **Results:**
  - adapter and strategy tests plus MVP-3 integration: **26 passed**;
  - with MVP-2.1, MVP-2, the API, market API and server lifecycle suites: **166 passed, 0 failed** (17.5 s);
  - `ruff check --no-cache` clean; `ruff format --no-cache --check` 209 files formatted; `mypy` with a fresh
    scratch cache shows no issues in 114 files.

## Option 1: 9-day startup history (owner decision; still uncommitted; M3-C not approved)
**Verified data path** (read before changing anything):
- **Fetch:** `Backfiller.run` (marketdata/backfill.py) fetches every unstored minute in `[end − lookback, end)`
  into the live bar store.
  - Minutes before the first stored bar are an initial load, not gaps. An install that already holds 3 days
    therefore fetches the older 6 days on its next start without registering spurious gaps.
- **Read:** paper waits for `wait_backfill` (bounded by `backfill_wait_s` 900). `_build_strategy` then reads
  `[start − start % 15m − 748 × 15m, start)` through the same SQLite store (`api/server.py` reader).
- **Feed:** `ReferenceLiveStrategy.warm_up` feeds those bars straight to `StrategySource`. This bypasses the
  engine's observed-only filter, so backfilled (`rest_backfill` / `rest_recovered`) bars count toward the
  warm-up.
- **Conclusion:** with `lookback_days: 3` the read started about 4.8 days before the backfilled window, so the
  strategy could never be ready from history. Raising `lookback_days` alone is enough. No marketdata/ or sim/
  change is needed.

**Changes:**
- **`configs/data_quality.yaml`:** `lookback_days` 3 → 9, with a cost comment. `max_backfill_days` stays 30
  (validated ≥ lookback). The code defaults stay at 3 days, so a file without a `backfill` section falls back
  to 3 and the strategy simply stays not ready.
- **`paper_runtime._build_strategy`:** warms up one instrument at a time. This is equivalent because all
  `StrategySource` and adapter state is per (market, symbol). It cuts the warm-up's transient memory from
  ~17 MB × instruments to ~17 MB, measured with tracemalloc at ~1.5 KB per bar for 11.2k bars.
- **Text:** `WARMUP_NOTE`, `LIMITATIONS` and the `paper.yaml` comment updated. The earlier `paper.yaml` text
  wrongly said backfilled minutes restart the warm-up; only unobserved minutes after the start do.

**Resources:**
- **Fetch size:** a first start fetches 12,960 bars per pair, in 13 REST pages of ≤ 1000. The 6-pair universe
  needs ~78 requests and ~78k rows.
- **REST weight:** about 26 per spot pair and 65 per usdm pair, far below the budget. At the unseeded cap of
  2 requests/s, the 78 requests take about 39 s.
- **Upgrade from 3 days:** about 8,640 rows per pair.
- **Past backfill durations** (read-only queries of local state DBs, from the review):
  - the 3-day backfill took 21–22 s (`data_m1_2h`, `data_m1_2h_rerun`) and about 68 s
    (`data_m1_2h_instrumented`);
  - three times that is about 65–205 s, within `backfill_wait_s` 900. A timeout leaves paper UNAVAILABLE
    (fail closed).
- **Not measured here (review N1):** backfill rows are written one by one (`synchronous=FULL`) on the event
  loop, so a first start blocks WS handling and the API for roughly 3× longer per pair than before. A real
  first start has not been measured.

**Remaining limitations:**
- Any minute missing inside the ~7.8-day read leaves that instrument short at the start. The headroom of
  9 − 7.8 days does not absorb missing minutes.
- After the start, an unobserved minute (REST-recovered or downtime-backfilled) still restarts the ~7.8-day
  warm-up. This is review B2, still pending an owner decision.

**Tests** (synthetic, focused; the proposal counts are pipeline checks, not strategy results):
- **Unit, readiness and parity:** `test_nine_days_of_history_make_the_default_strategy_ready_for_its_first_decision`.
  - Uses the default params; every instrument is ready, with no records during warm-up.
  - The first decision falls at the first eligible 15m close.
  - Decision bars, proposals and requests equal the backtest's (2 CONFIRMED, WATCH counts equal).
- **Unit, missing minute:** `test_a_missing_minute_in_the_startup_history_resets_readiness`.
  - A recent missing minute leaves that instrument not ready and undecided; an old one does not matter.
- **Integration, configuration guard:** `test_configured_startup_backfill_covers_the_reference_warm_up_and_paper_stays_off`.
  - The configured lookback covers the read and stays within the stated budget.
  - `paper.yaml` is still `enabled: false` and `strategy: none`.
- **Integration, end to end:** `test_nine_day_startup_backfill_reaches_the_reference_warm_up`.
  - Data path: the real `Backfiller` with the configured lookback, then an in-memory `LiveBarStore`, then the
    server's reader, then the runtime warm-up.
  - One ordered read per instrument, inside the backfilled window: `warmup_bars` equals the stored minutes,
    with no duplicates or out-of-order bars (`skipped_not_newer` 0).
  - The strategy is ready at open, with no signals or sim orders.
  - The builder-owned minute arrives live without a break.
  - A later REST-recovered usdm minute resets usdm only.
- **Mutation check:** with `lookback_days: 3` both integration tests fail, the end-to-end one at
  "read inside the backfilled window".
- **Focused runs:**
  - adapter, strategy, MVP-3, MVP-2.1, MVP-2, the paper API, API core, marketdata config/backfill and the mdi
    service: **131 passed**;
  - market API and server lifecycle: **24 passed**;
  - `ruff check --no-cache` / `ruff format --check` clean; `mypy` (scratch cache) shows no issues in 114 files.
- **Review:** code, config and tests approved. B1 was doc-only (this devlog and the metrics row were stale)
  and is fixed. N2 (the margin wording) is fixed. N1 is disclosed above. N3 (the end-to-end test builds
  `BackfillSettings` itself rather than going through `service._backfill`) is recorded. N4 (code defaults
  3 days) is noted above.

## Open limitations at the M3-C checkpoint (explicitly not resolved)
1. **Startup backfill not measured.** A real nine-day first-start backfill has not been run or timed. No
   historical import or real backfill was executed for this step.
2. **Event-loop blocking.** The startup backfill writes rows one by one (`synchronous=FULL`) on the event loop.
   On a first start this may block WS handling and the API for longer than with 3 days (review N1;
   marketdata/ unchanged).
3. **Missing historical minutes.** A minute missing inside the ~7.8-day warm-up read keeps that instrument
   from becoming ready at paper start.
4. **Unobserved minutes after start.** An unobserved minute (REST-recovered or downtime-backfilled) resets
   about 7.8 days of warm-up for that instrument (review B2). The meaning of observed and unobserved data is
   unchanged and still needs an owner decision.
5. **Not validated.** Reference signals are REFERENCE / NOT VALIDATED (evidence grade "X", no
   preregistration). They are not research results.
6. **Restore equivalence.** The paper-journal restore-equivalence defects remain unresolved: seeds 13073 and
   465923499, and the open restore property classes from M3-FIX-SIMPROP (Part B is not implemented).
7. **No profitability claim.** Historical profitability of `bracket_reference` has not been established. The
   synthetic tests check pipeline behaviour (parity, causality, readiness), not performance.

No M3 acceptance and no real-world strategy performance is claimed.
