# M2 approval package after O-2 / O-3

> **Decided 2026-10-28** (see DECISIONS_PENDING D-09 … D-17): only E-0 and E-4(a) were authorized; E-1, E-2
> and E-3 are briefs only (`docs/tasks/M2-E1.md`, `M2-E2.md`, `M2-E3.md`). The ADRs are accepted in principle,
> as recorded in their status lines. The text below is the package as proposed.

- **Base:** `feat/mvp-4-dashboard` @ `ebefbc1`.
- **Owner checks:** O-2 PASS (exactly the 2 known C-1 rows) and O-3 PASS (B10 `verify` ok), 2026-10-28.
- **Unchanged until each package below is approved:** ADR-0013/0014/0015 stay Proposed; no canonical
  replacement, promotion, QA-rule or simulator change; acceptance labels unchanged.

## 1. Confirmed facts this package builds on
| Fact | Evidence |
|---|---|
| Canonical lake (5 symbols, 2022-01 → 2025-12, holdout excluded) has **exactly two** count-level row losses: XRP 2025-04-03 17:24 and SOL 2022-12-21 15:23 | O-2 report `evidence/owner_checks/o2_canonical_rows_20261028T014419Z.*` (240 files) |
| No committed parser reproduces the loss; leading hypothesis: transient fault during the 2026-10-24 import (WHEA period) | `2026-10-28-m2-decision-recommendations.md` §4 |
| Value-level integrity of canonical is proven **only** where an independent re-import matches: BTC 2022–2024; ETH, SOL, DOGE 2024–2025; XRP 2024–2025 except 2025-04 | Part-hash parity, `M2_DATASET_ACCEPTANCE.md` |
| B10 is intact: 24 parts, 1,052,640 rows, 28 sources re-hashed, 0 errors | `evidence/imports/b10_spot_avax_2024_2025/verify.json` |
| All 5 AVAX/HBAR open differences are the leading-zero-trade convention; every other field matches | ADR-0015 evidence table |
| BTC 2022–2025 has **no** zero-trade minute outside the 2023-03-24 outage; AVAX 0.056 %; HBAR 0.64 % | Recommendations §1 |

## 2. Work split
### A. Engineering by Claude's agents (code and tests; each package is its own branch from `ebefbc1`, uncommitted until approved)
| Package | Decisions | Owner agents | Scope | Must not touch |
|---|---|---|---|---|
| **E-0, documentation** | P-6, plus ADR status changes once approved | Lead | Apply the §6 clarification text. Set ADR-0013/0014/0015 to Accepted only for what you approve | Contracts, code |
| **E-1, outage registry and no-fill on zero-trade** | P-1 + P-2 + P-4 (one package, for backtest/paper parity) | sim-risk-engineer (simulator), quant-engineer (runner and reports); causality-auditor review | `configs/exchange_interruptions.yaml` with the P-2 entry and its manifest hash; runner downtime accounting (`missing_minutes` and `missing_warmup_minutes` exclude registered minutes only); no fills on zero-trade candles and inside registered intervals; first-traded-open execution and L-11 position close; the `spans_exchange_downtime` flag counted into the `spans_gap` share; `SIM_ENGINE_VERSION` and `RUNNER_VERSION` bumps | `contracts/` (Lead only), raw data, the `max_missing_share` value, the holdout guard |
| **E-2, QA classification and the narrow exception** | P-3 + P-5 | data-engineer; reviewer | Z1/Z2/M/A classification of zero-volume minutes; the `leading_zero_trade_open` explained difference with every ADR-0015 condition (derived open = filler, fail closed at month boundaries); `cross_check_mismatches_explained` in the QA summary; holdout redaction kept; QA report version bump | Stored bars, derived bars, any tolerance |
| **E-3, importer integrity safeguards** (C-1 prevention) | P-8 | data-engineer; reviewer | Record the git commit and dirty flag in manifest `tool`; fail an import when parsed rows ≠ data lines of a verified archive (fail closed); re-read and re-hash written parts before commit; write an import log under `evidence/` | Existing datasets |
| **E-4, read-only tools for the owner** | P-4 prerequisite; P-8; P-7 | data-engineer | (a) a live zero-trade kline capture script (WebSocket, read-only, 60 min on a thin symbol); (b) a part-hash parity comparison script for the C-1 parity batches; (c) instructions for the isolated QA re-run | Any data write outside the report files |
| **E-5, promotion procedure** (design first) | P-9 | Lead + data-engineer | A logged, non-overwriting procedure that replaces a canonical month only with an independently verified part; keeps the old manifest; records old and new content hashes | Execution, which is the owner's and separately approved |

### B. Data processing by the owner (normal PowerShell, no administrator; Claude prepares scripts and reads results)
| Action | When | Read/write | Effort |
|---|---|---|---|
| **O-4: live zero-trade kline capture** (thin symbol, e.g. HBARUSDT spot, 60 min) | Before E-1 is finalized (P-4 prerequisite) | Read-only market data; one report file | ~1 h unattended |
| **O-5: C-1 parity batches** | After E-3 (so the new safeguards run); with hardware monitoring as in B01–B12 | Isolated imports into new `data_batches/c1_*` folders; never `data/` | BTC 2025 (12 months) + ETH/SOL/DOGE/XRP 2022–2023 (4 × 24); about 108 months, minutes per batch |
| **O-6: part-hash parity comparison** (E-4b script) | After O-5 | Read-only; one report | Seconds |
| **O-7: QA re-run for B03, B10, B12** (isolated re-import from the cached archives) | After E-2 | Writes only to new scratch data folders | Minutes |
| **O-8: holdout parity, sealed path only** | Separate approval (touches the holdout procedure) | Counts and hashes only, no prices | Minutes |
| **O-9: coverage imports** (BNB 2022–23/2025; ADA, AVAX, TRX, HBAR 2022–23; holdout for those five; USD-M bars, mark prices, funding) | After E-3; one batch at a time under the AP-1 procedure | Isolated batches | Hours in total, spread over sessions |

## 3. Safeguards (every package)
- **Repository and process:**
  - Every package is a branch from `ebefbc1`. Changes stay uncommitted until reviewed.
  - Local commits only with your approval; no merge, push or main change.
  - `contracts/` is untouched (Lead-only rule).
- **Fail closed, no relaxation:**
  - `max_missing_share` stays 0.0; only registered, owner-approved intervals are reported as downtime.
  - No raw candle is changed, removed or synthesized; QA exceptions are listed with their evidence, never hidden.
  - Version bumps (simulator, runner, QA report) make old and new results distinguishable. There are no recorded
    runs locally, so no result is silently regraded.
- **Data and holdout:**
  - The holdout guard and sealing (ADR-0009, L-07) are unchanged.
  - Owner data steps write only to new isolated folders or report files, and refuse to overwrite.
- **Testing and machine constraints:**
  - Focused tests only (WHEA); every engineering package gets a code review, and E-1 also gets a causality audit.

## 4. Expected tests
**E-1** (simulator and runner):
- **Canaries:** an unregistered gap still gives grade X; a registered interval gives no X and reports
  `exchange_downtime_minutes`.
- **Registry hashing:** a registry change changes the manifest hash.
- **Execution rules:**
  - no fill on a zero-trade candle;
  - an entry waits for the next traded candle (gap-fill: the worse of open and level);
  - `valid_until` is never extended;
  - an entry scheduled inside an interval executes at the first traded open or is cancelled;
  - an exposed position closes at the first traded open after the interval, flagged.
- **Flag accounting:** the flag counts into the `spans_gap` share and caps at C above 5 %.
- **Parity and regression:**
  - backtest/paper parity on the same bars (shared simulator);
  - golden trades updated under the new `SIM_ENGINE_VERSION`;
  - existing look-ahead canaries and ledger tests still pass.

**E-2** (QA):
- **The exception:**
  - the five real cases reproduce as explained (fixtures from the cached archives, recorded as test data);
  - mutation tests (flip high, low, close, volume or trades, or set the derived open ≠ filler) must fail the
    exception;
  - month-boundary failure is closed.
- **Classification:** Z1/Z2/M/A unit tests, including "zero-trade after a missing minute → A".
- **Visibility:** `cross_checks_ok` stays false for any unexplained mismatch; the explained count is visible;
  holdout redaction is intact.

**E-3** (importer):
- **Fail closed on lost rows:** a synthetic archive where the parser loses a row fails the import.
- **Manifest provenance:** the manifest records the commit and dirty flag.
- **Part re-read:** a corrupted written part is detected before commit.
- **No regressions:** AP-1 bounded-memory tests and the existing importer tests still pass.

**E-4:** each script refuses to overwrite and to run outside the repository, and does no data writes; dry runs on
known cases (as was done for O-2).

## 5. Impact on M4 research readiness
| Requirement for M4 (RESEARCH_PROTOCOL, D-05 splits) | Status today | Cleared by |
|---|---|---|
| Development-segment backtests over 2023 can be graded (not X from the market-wide halt) | **Blocked** | E-1 (P-1, P-2, P-4) |
| No false gaps in canonical data | **Blocked** for XRP 2025-04 and SOL 2022-12 (2 rows) | O-5 + O-6, then the E-5 repair (P-8, P-9) |
| Canonical values independently verified for the research range | Partial (see §1) | O-5 + O-6 (+ O-8 for the holdout) |
| Data coverage for the frozen Top 10 | 5 of 10 symbols complete for 2022–2025 (+ holdout); BNB, ADA, AVAX, TRX, HBAR incomplete | O-9 + P-7/P-9 |
| Thin-symbol fills realistic | Optimistic today (fills on zero-trade candles) | E-1 (P-4) after O-4 |
| QA exceptions documented and reproducible | Proposed | E-2 + O-7 |
| Futures research (D-02) | No USD-M or funding data | O-9 (USD-M) |
| Reference vs research separation in the UI | Done (MVP-4) | — |
| M2/M3 formal gates (restore property, live replay hash, M1 2-hour run) | Open | Separate tracks (Part B, AP-4 hardware, M1 run) |

**Earliest honest M4 start:**
- **Spot-only exploration on the five complete symbols** after E-1, E-2, E-3, O-4, O-5/O-6 and the C-1 repair.
  Results would carry the existing grade caps, and no holdout use.
- **Full Top 10 research** only after O-9 coverage and P-7/P-9.
- **Formal M4 acceptance** additionally needs preregistrations, walk-forward and holdout evaluation under
  ADR-0009/0012, and the open M2/M3 gate items.

## 6. Recommended approvals now
1. **Local docs commit** of the uncommitted material:
   - this package and the O-2/O-3 package;
   - the decision recommendations with their O-2/O-3 updates;
   - the acceptance-matrix B10 update;
   - `scripts/owner_checks/o2_canonical_row_audit.py`;
   - the metrics rows.
2. **Decisions:**
   - P-1 + P-2 (full interval) + P-4 together;
   - P-3 + P-5;
   - P-6;
   - P-8;
   - P-7 and P-9 stay deferred.
3. **Engineering authorizations,** in this order:
   - E-0;
   - E-4(a), so O-4 can run early;
   - E-3, then O-5/O-6;
   - E-1 and E-2, which can run in parallel (different files);
   - E-5 design.
