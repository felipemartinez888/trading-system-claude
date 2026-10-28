# Devlog — 2026-10-27 — M2 owner-import handover: reconciliation (separate remediation track)

- **Status:** documentation only. **No dataset was accepted, promoted, modified or re-imported by Claude.**
  The `data_batches/` and `evidence/imports/` folders are the owner's and stay untracked. The canonical `data/`
  and the 2026 holdout are untouched.
- **Input:** the owner's handover `TSC_CLAUDE_HANDOFF_INTEGRAL_2026-10-27.zip`, an owner + ChatGPT summary of
  supervised imports B01–B12 run with the AP-1 importer (worktree `tsc-import-7be73eb` @ `7be73eb`). It is
  treated as evidence of owner work, not as Claude verification.
- **Method:** read-only checks by Claude:
  - `data_batches/<tag>/imports/*/{manifest,qa_report}.json` and `evidence/imports/<tag>/*`;
  - targeted reads of the cached source ZIPs (`data_batches/<tag>/archive_cache/...`);
  - an inventory of the canonical `data/imports` manifests.
- **Not used:** no import was rerun, and the optional ChatGPT "pre-Claude extractor" was not run (by the
  owner), so its outputs do not exist.
- **Track:** this is the M2 data-acceptance track. It is independent of MVP-4. The policies below must not be
  changed during dashboard work.

## 1. Batch matrix (verified from local files)
**Columns:** QA (AP-1 criteria), `verify`, data + QA-reference sources (manifest), formal status.

| Batch | Rows | QA (AP-1 criteria) | `verify` | Sources (data + QA ref) | Formal status (owner) |
|---|---:|---|---|---|---|
| B01 BTC 2024-01 | 44,640 | all 0, cross-checks ok | ok | 1 + 2 verified | ACCEPTED (owner) |
| B02 BTC 2024 | 527,040 | all 0, ok | ok | 12 + 4 | ACCEPTED (owner) |
| B03 BTC 2022–23 | 1,051,119 | **missing 81, 1 gap, 1 malformed**; zero-volume **71 min / 1 run** | ok | 24 + 4 | PENDING_VERIFICATION |
| B04 BNB 2024 | 527,040 | all 0, ok | ok | 12 + 4 | PENDING_VERIFICATION |
| B05–B09 ETH, SOL, DOGE, XRP, ADA 2024–25 | 1,052,640 each | all 0, ok | ok | 24 + 4 | PENDING_VERIFICATION |
| B10 AVAX 2024–25 | 1,052,640 | cross-checks **fail** (3 × 15m open) | **no `verify.json`** | 24 + 4 | PENDING_VERIFICATION |
| B11 TRX 2024–25 | 1,052,640 | all 0, ok | ok | 24 + 4 | PENDING_VERIFICATION |
| B12 HBAR 2024–25 | 1,052,640 | cross-checks **fail** (2 × 15m open); zero-volume 445 / 134 | ok | 24 + 4 | PENDING_VERIFICATION |

- **Cross-check coverage is sampled.** `importer._cross_checks` compares native 15m and 1h files only for the
  **first and last month** of each batch, so 22 of 24 months per batch are not cross-checked.
- **"ACCEPTED" is the owner's classification.** The repository has no acceptance record or contract for it.
  The AP-1 procedure (`2026-10-27-ap1-owner-procedures.md`) defines only the QA criteria: all counts 0 and
  `cross_checks_ok` true.

## 2. Findings
### B10 / B12: 15m open mismatches. Root cause: a convention in Binance's published files, not an import defect
The QA diffs are `[derived from 1m, native]`. For every one of the 5 cases, the cached ZIPs show:

| Bucket (UTC) | Native 15m open | First 1m (trades, volume) | 2nd 1m open | Other fields |
|---|---|---|---|---|
| AVAX 2025-12-07 03:45 | 13.49 | 13.48 (0, 0), the previous close | 13.49 | H/L/C, volume and trades equal |
| AVAX 2025-12-29 23:15 | 12.41 | 12.42 (0, 0) | 12.41 | equal |
| AVAX 2025-12-30 18:15 | 12.56 | 12.55 (0, 0) | 12.56 | equal |
| HBAR 2024-01-25 18:30 | 0.0711 | 0.0712 (0, 0) | 0.0711 | equal |
| HBAR 2025-12-14 03:45 | 0.124 | 0.12399 (0, 0) | 0.124 | equal |

- **The convention:** Binance's 1m archive fills a minute without trades with O=H=L=C equal to the previous
  close. The native 15m candle opens at the first real trade.
- **What our aggregation does:** `aggregate()`, and the live 1m→15m path, take the filler minute's open.
  Backtest and live are consistent with each other, and there is no look-ahead.
- **The QA check is right to flag it.** Its rule is exact equality.
- **Proposal (needs approval; not implemented):** classify "leading zero-trade minute(s), and the native open
  equals the first traded minute's open" as an **explained difference** in `marketdata/archive/qa.py`, with
  tests. It is reported separately and never silently passed. A filler minute could in principle also move
  the derived high or low, and the rule must cover that.

### B12: zero-volume minutes
The sampled runs are 3–5 minutes long (thin trading). This is not evidence of corruption. The bars stay as
published.

### B03: 2023-03-24 interruption is market-wide
- **The same hole in every canonical symbol.** The canonical `data/` lake (BTC, ETH, SOL, DOGE, XRP spot 1m,
  2022-01 → 2026-09, imported 2026-10-24) misses **exactly the same 81 minutes** (12:39 → 14:00 UTC) in all five
  symbols.
  - Their total missing counts differ because of separate 1-minute gaps: XRP 82 (2025-04-03 17:24); SOL 83
    (2022-12-21 15:23 and 2023-02-14 16:39). These gaps were found by the MVP-4 audit.
  - Under the current tolerance, those SOL minutes also grade X any SOL run whose range contains them, regardless
    of the halt.
- **The full sequence in B03:**
  - a **71-minute zero-volume run** (11:28–12:39 UTC);
  - one truncated candle at 12:39 (close time +41.6 s), correctly rejected as malformed;
  - then 81 minutes absent until 14:00.

  That is consistent with about 2.5 h without trading. An exchange-wide halt is the working hypothesis, to be
  confirmed from a reliable source. The handover counted only the 81 minutes.
- **Research consequence:**
  - `configs/backtest.yaml` sets `max_missing_share: 0.0`, so any backtest range that contains 2023-03-24 is
    graded **X** (RESEARCH_PROTOCOL §6). That covers every D-05 development-segment (2022–2024) run whose range
    includes that date, on any symbol.
  - M4 needs a policy decision: either an ADR for documented exchange-wide halts (known non-trading period,
    history reset, no fills, disclosed in reports) or a justified tolerance.
  - Choosing ranges that avoid the date would be cherry-picking and is not acceptable.

### B11: provenance
- The importer cache is per batch (`data_batches/<tag>/archive_cache/...`); the wrapper looked under
  `repo/data/archive_cache`.
- Claude's independent re-hash: **28/28 sources match the manifest SHA-256** (24 monthly 1m files plus 4 QA
  references).
- Resolved.

### Coverage against the frozen Top 10 (`universe_snapshot_2025-01-01.yaml`) and the D-05 splits
- **Canonical `data/`:** BTC, ETH, SOL, DOGE, XRP from 2022-01 to 2026-09, with the holdout parts sealed.
- **`data_batches`:** adds BNB 2024 and ADA, AVAX, TRX, HBAR 2024–25. For the other five symbols it only
  duplicates data already in `data/`.
- **Still missing:**
  - BNB 2022–23 and 2025;
  - ADA, AVAX, TRX, HBAR 2022–23;
  - the 2026 holdout (sealed path) for those five;
  - all USD-M 1m bars, mark prices and funding (D-02).

## 3. What the imports change
| Level | Status |
|---|---|
| Implementation (importer, QA, lake) | Done (M2 code complete; AP-1 bounded importer) |
| Successful local execution | Yes, 12 batches |
| Dataset integrity (verify, checksums) | Yes, except B10 (no verify run) |
| Data-quality acceptance | **Owner decision.** B03, B10 and B12 need the policies above; B04–B09 and B11 meet the AP-1 QA criteria |
| Backtesting readiness | Partial. The D-05 development segment is incomplete for 5 of the Top 10, and 2023 runs grade X until the halt policy exists. No backtest run exists locally |
| Strategy effectiveness | **Not established.** No graded research run exists |

## 4. Remediation track (by impact; nothing here is authorized yet)
1. **Not blocking MVP-4.** The dashboard reads whatever runs and paper state exist.
2. **Blocks M4 grading:** the policy for the 2023-03-24 halt (ADR) and the zero-volume filler-minute policy.
3. **Blocks the formal acceptance of B10 and B12:** the QA explained-difference rule (marketdata/ change plus
   tests, needs approval).
   - Option: owner-run full-month cross-checks, with no re-import.
4. **Data coverage before M4 research:** owner imports of the missing symbol-years into canonical `data/`,
   the holdout through the sealed path, and USD-M plus funding. Promoting `data_batches/` needs owner approval.
5. **Housekeeping:**
   - the `docs/ROADMAP.md` live status is stale;
   - `frontend/package-lock.json` is untracked and no pnpm lockfile is tracked.
