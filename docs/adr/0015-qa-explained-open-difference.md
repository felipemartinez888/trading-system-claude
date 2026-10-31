# ADR-0015: A narrow, evidence-bound QA exception for native-versus-derived open differences

- **Status:** Accepted in principle (owner decision D-13/P-5, 2026-10-28). Safeguards approved as the governing
  policy and implementation authorized by the owner on 2026-10-30. Implemented in E-2, committed and
  integrated locally
  (`e973f86`; not pushed, not in `main`), with its targeted regression and independent review passed
  (final verdict APPROVE WITH NOTES; [E-2 devlog](../devlog/2026-10-30-m2-e2-zero-trade-qa.md)).
  Some of its mutants ran on a prior code revision. O-7 has not run, M2 is not formally accepted, and no
  statistical strategy validation is implied.
  The existing reports of B10 and B12 are unchanged and keep `cross_checks_ok = false`; they would change only through the O-7 re-runs, which
  are not authorized.
- **Relates to:** ADR-0004 (derived timeframes and the consistency check), ADR-0014 (zero-volume candles).

## Context
The import QA compares 15m and 1h bars derived from our 1m bars with Binance's native 15m and 1h archives. This
is done for the first and last month of each import. Five 15m buckets differ, and **only in `open`**:

**Reading the table:**
- *1m line* is the CSV line number in the 1m archive: first minute / second minute of the bucket.
- *15m line* is the CSV line number in the 15m archive.
- The 1m files and their SHA-256 values are the manifest sources of datasets `d10de5e2a6c1…` (B10) and
  `3d67799fbd1c…` (B12).

| Bucket (UTC) | Derived open | Native open | First 1m candle | 2nd 1m open | 1m line | 15m line |
|---|---|---|---|---|---|---|
| AVAX 2025-12-07 03:45 | 13.48 | 13.49 | 13.48 flat, 0 trades, 0 volume (= previous close) | 13.49 | 8866 / 8867 | 592 |
| AVAX 2025-12-29 23:15 | 12.42 | 12.41 | 12.42 flat, 0 / 0 | 12.41 | 41716 / 41717 | 2782 |
| AVAX 2025-12-30 18:15 | 12.55 | 12.56 | 12.55 flat, 0 / 0 | 12.56 | 42856 / 42857 | 2858 |
| HBAR 2024-01-25 18:30 | 0.0712 | 0.0711 | 0.0712 flat, 0 / 0 | 0.0711 | 35671 / 35672 | 2379 |
| HBAR 2025-12-14 03:45 | 0.12399 | 0.124 | 0.12399 flat, 0 / 0 | 0.124 | 18946 / 18947 | 1264 |

In every case, high, low, close, volume, quote volume, taker volumes and trade count are **equal**, as checked
against the cached archives.

Source files (SHA-256):

| File | SHA-256 |
|---|---|
| `AVAXUSDT-1m-2025-12.zip` | `6464c58b341850ccc7212f23d82b5604c74944c916c5258936c4f42a1736c1df` |
| `AVAXUSDT-15m-2025-12.zip` | `1a777574924bf8f055f7318cb39fea6459d6f039b29d161435c7a6973e1b2b36` |
| `HBARUSDT-1m-2024-01.zip` | `f75724733830d1521895a655b5e682280a3ebead1fb0540c2a1269c099852b3d` |
| `HBARUSDT-15m-2024-01.zip` | `843f84b2e6faa5fcf5b1618cd34c1f1efcc37dd6dae54cb3050bf8a57a123735` |
| `HBARUSDT-1m-2025-12.zip` | `3d2ffff772cfb4c8544c59d5865880784398b21a8196a64163d588af3965813c` |
| `HBARUSDT-15m-2025-12.zip` | `639d2e01a719186e12b3fb4b8013004520a2c4adb419376fdf2fd34b548154dc` |

**Cause.** Binance's native 15m candle opens at the first trade of the 15 minutes. The first 1m candle of these
buckets had no trade, so Binance published it flat at the previous close (ADR-0014, class Z1). Our derived open
takes that filler price. Both files are internally consistent; the conventions differ.

## Decision (proposed)
1. **One explained-difference reason, `leading_zero_trade_open`, and nothing broader.** A cross-check mismatch
   is classified as explained only if **all** of the following hold for that bucket:
   - the **only** differing field is `open`;
   - `high`, `low` and `close` are exactly equal;
   - volume, quote volume and taker volumes are equal within the existing tolerances;
   - `trades` is exactly equal;
   - the bucket's leading `k ≥ 1` 1m candles are all class Z1 (`trades = 0`, all volumes 0, O=H=L=C = the close
     of the candle before the bucket);
   - **the derived open equals the open of the bucket's first 1m candle**, which is the Z1 filler and therefore
     equals the close of the candle before the bucket. This catches an aggregation that takes the open from the
     wrong minute;
   - the candle before the bucket is available. At a month boundary it may lie outside the bars the check
     receives; if it is unavailable, the exception **fails closed** and the mismatch stays unexplained;
   - the native open **equals** the open of the first 1m candle in the bucket with `trades > 0`;
   - the derived bucket is complete (all 15 minutes present), as today.
2. **Anything else stays a failure.** That includes a filler price outside the traded range (it would also
   change high or low), any other field differing, and a missing or incomplete bucket.
3. **Nothing is hidden.**
   - Explained mismatches stay listed in the QA report with full evidence: bucket, `k`, filler price, first
     traded open, source file names, SHA-256 values and line numbers.
   - The QA summary gains `cross_check_mismatches_explained` next to the existing counts.
   - The evidence includes prices, so it keeps the existing holdout redaction of QA outputs (L-07(c);
     `qa.py` `redact_holdout`).
   - `cross_checks_ok` keeps its meaning: true only if there are **no unexplained** mismatches.
   - The acceptance matrix shows explained exceptions as their own column, and owner acceptance stays a separate
     decision.
4. **Reproducible evidence is required.** The classification is computed by code from the same verified
   archives (never entered by hand). A test fixture reproduces each of the five cases above. A mutation test
   flips one of high, low, close, volume or trades and must make the exception fail.
5. **Scope.** The rule applies to every cross-checked interval (15m and 1h). It never changes stored bars,
   derived bars or the lake.

## Consequences
- **With the rule in place,** B10 and B12 would report 3 and 2 explained mismatches, 0 unexplained, and
  `cross_checks_ok = true`. They would still need owner acceptance (see the acceptance matrix).
- **Coverage is still sampled:** 2 of 24 months per batch. Full-month cross-checks are an optional owner-run
  action that does not re-import anything.
- **Implementation it needs** (not authorized): the classifier in `marketdata/archive/qa.py`, QA summary fields,
  tests, and re-running QA on the existing B10 and B12 data (read-only over cached archives, owner-run).

## Alternatives rejected
- **Tolerating open differences of one tick:** too broad. It would hide real aggregation defects.
- **Excluding `open` from the cross-check:** loses a real consistency check.
- **Marking B10 and B12 accepted by hand:** not reproducible, and it would weaken the QA contract.
