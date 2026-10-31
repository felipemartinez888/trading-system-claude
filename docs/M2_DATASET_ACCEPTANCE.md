# M2 dataset acceptance matrix: owner imports B01–B12

- **Status:** proposal (2026-10-28). **No acceptance label is changed by this document.** The "Owner status"
  column copies the owner's current classification. The "Proposed path" column is a recommendation for the
  owner to decide.
- **Scope:** Binance **spot 1m** batches imported by the owner with the AP-1 importer (worktree
  `tsc-import-7be73eb` @ `7be73eb`), stored in `data_batches/<tag>`, with evidence in `evidence/imports/<tag>`.
  None of them is in canonical `data/`; promotion needs separate owner approval.
- **Evidence sources:**
  - the batch `manifest.json`, `qa_report.json` and evidence files;
  - Claude's read-only checks of 2026-10-27/28: part content hashes compared with canonical `data/`, a re-hash
    of the B11 sources, and targeted reads of the cached archives;
  - `docs/devlog/2026-10-27-m2-handover-reconciliation.md` and `docs/devlog/2026-10-28-m2-policy-design.md`.

## Columns
1. **Exec:** `NORMAL` means `batch.json` records outcome `NORMAL_COMPLETION`, exit code 0, and 0 WHEA and 0
   unexpected-shutdown events during the run. This holds for all 12 batches. The sampled peak memory (0.8 MB in
   every batch) is not credible as importer memory and is treated as a monitoring artifact.
2. **QA:** the AP-1 criteria. Interior missing, gaps, duplicate conflicts, rejected, malformed and unavailable
   files must all be 0, and `cross_checks_ok` true. A cross-check covers the first and last month only.
3. **Verify:** `tsclaude data verify` part integrity (`verify.json`).
4. **Manifest provenance:** every source has an archive URL, a CHECKSUM URL, a SHA-256 and `verified=true`,
   recorded at ingestion.
5. **Re-hash:** the cached archives re-hashed after ingestion and compared with the manifest, either by
   `tsclaude data verify` (`sources_checked`) or by an owner or Claude script.
6. **Independent parity:** the batch's part content hashes are identical to an independently imported
   reference (canonical `data/`, or an earlier diagnostic run).
7. **Explained exceptions:** QA findings with a documented, reproducible cause (ADR-0013/0014/0015 proposals).
8. **Owner status:** the owner's formal classification (unchanged here).

**E-2 note (2026-10-30, integrated locally).** The QA classification and the ADR-0015 exception are
committed and integrated locally (`e973f86`; not pushed, not in `main`). They passed their targeted
regression and independent review (final verdict APPROVE WITH NOTES); some of the mutants ran on a
prior code revision. O-7 has not run, M2 is not formally accepted, and no statistical strategy
validation is implied. No row below changes: every QA value here comes from the existing version-1 reports, and
column 7 stays the documented explanation. A batch shows machine-classified values only after a QA version 2
re-run (O-7, not authorized).

## Matrix
| Batch | Dataset | Rows | 1 Exec | 2 QA | 3 Verify | 4 Manifest | 5 Re-hash | 6 Parity | 7 Explained | 8 Owner status | Proposed path |
|---|---|---:|---|---|---|---|---|---|---|---|---|
| B01 BTC 2024-01 | `829c381a…` | 44,640 | NORMAL | PASS | ok | 1+2 verified | verify: 3/3 sources | **1/1 = canonical** (+ AP-1 parity file) | none needed | ACCEPTED | Keep |
| B02 BTC 2024 | `e2d4a0dc…` | 527,040 | NORMAL | PASS | ok | 12+4 | verify: 16/16 | **12/12 = canonical** (+ AP-1 parity 12/12) | none needed | ACCEPTED | Keep |
| B03 BTC 2022–23 | `f700272d…` | 1,051,119 | NORMAL | **FAIL**: 81 missing, 1 gap, 1 malformed; Z1 71 min | ok | 24+4 | verify: 28/28 | **24/24 = canonical** (+ AP-1 parity) | 2023-03-24 exchange halt (ADR-0013 proposal), source-level; the malformed row is the truncated halt candle | PENDING | Accept **with documented gap** once ADR-0013 is approved; the gap stays in the data |
| B04 BNB 2024 | `9e71b0a6…` | 527,040 | NORMAL | PASS | ok | 12+4 | **owner: 12/12 ZIP = CHECKSUM**; verify: 16/16 | no reference (BNB not in canonical) | none needed | PENDING | Accept on the QA, verify and re-hash evidence; parity not possible yet |
| B05 ETH 2024–25 | `78306a32…` | 1,052,640 | NORMAL | PASS | ok | 24+4 | owner: 24/24; verify: 28/28 | **24/24 = canonical** | none needed | PENDING | Accept (parity is now available) |
| B06 SOL 2024–25 | `b240b0b7…` | 1,052,640 | NORMAL | PASS | ok | 24+4 | owner: 24/24; verify: 28/28 | **24/24 = canonical** | none needed | PENDING | Accept |
| B07 DOGE 2024–25 | `a010f302…` | 1,052,640 | NORMAL | PASS | ok | 24+4 | owner: 24/24; verify: 28/28 | **24/24 = canonical** | none needed | PENDING | Accept |
| B08 XRP 2024–25 | `10c2c062…` | 1,052,640 | NORMAL | PASS | ok | 24+4 | owner: 24/24; verify: 28/28 | **23/24 = canonical; 2025-04 differs**: batch 43,200 rows vs canonical 43,199 from the **same** source file (SHA-256 `a3619422…`) | the batch is complete; **canonical lost the 2025-04-03 17:24 row** (import defect in the older canonical import) | PENDING | Accept B08. **Repair canonical XRP 2025-04** (see the canonical findings below) |
| B09 ADA 2024–25 | `d72aa5d9…` | 1,052,640 | NORMAL | PASS | ok | 24+4 | owner: 24/24; verify: 28/28 | no reference | none needed | PENDING | Accept on the QA, verify and re-hash evidence |
| B10 AVAX 2024–25 | `d10de5e2…` | 1,052,640 | NORMAL | **FAIL**: 3 × 15m open (2025-12) | **ok** (owner O-3, 2026-10-28: 24 parts, 1,052,640 rows, 0 errors) | 24+4 | verify: 28/28 | no reference | all 3 explained as `leading_zero_trade_open` (ADR-0015 proposal), source-level, every other field equal | PENDING | After ADR-0015, re-run QA (isolated re-import from the cached archives); then decide |
| B11 TRX 2024–25 | `f89e172f…` | 1,052,640 | NORMAL | PASS | ok | 24+4 | **Claude: 28/28 = manifest** (the per-batch cache; the wrapper's path was wrong); verify: 28/28 | no reference | none needed | PENDING | Accept on the QA, verify and re-hash evidence |
| B12 HBAR 2024–25 | `3d67799f…` | 1,052,640 | NORMAL | **FAIL**: 2 × 15m open (2024-01, 2025-12); Z1 445 min / 134 runs | ok | 24+4 | verify: 28/28 | no reference | both explained as `leading_zero_trade_open` (ADR-0015); zero-volume minutes are genuine no-trade (ADR-0014 Z1; sampled runs of 3–5 min) | PENDING | After ADR-0014/0015, re-run QA; the audit already found all 445 zero-volume minutes Z1-shaped (0 class A); then decide |

**Coverage of the frozen Top 10** (`configs/universe_snapshot_2025-01-01.yaml`, spot), across batches and
canonical:

| Symbol | Covered | Missing for the D-05 splits |
|---|---|---|
| BTC, ETH, SOL, DOGE, XRP | 2022–2026-09 (canonical; holdout parts sealed) | none |
| BNB | 2024 (B04) | 2022–23, 2025, 2026 holdout |
| ADA, AVAX, TRX, HBAR | 2024–25 | 2022–23, 2026 holdout |
| All ten | — | USD-M 1m, mark prices and funding (D-02) |

## Canonical-lake findings from the same audit (not B01–B12; nothing changed)
- **C-1, rows present in the source but missing from canonical (import defect):**
  - **XRP 2025-04-03 17:24:** the row is in `XRPUSDT-1m-2025-04.zip` at line 3925. The canonical manifest records
    43,199 source rows for the file whose SHA-256 is the same as B08's, while the AP-1 importer read 43,200.
  - **SOL 2022-12-21 15:23:** the row is in `SOLUSDT-1m-2022-12.zip` at line 29724.
  - Both rows look normal (traded candles). The canonical datasets were created 2026-10-24 by an earlier importer
    build, which dropped them while parsing.
  - QA reported each minute only as a gap (XRP `missing_interior` 82, SOL 83). No malformed or rejected record
    explains it.
  - Batch and canonical manifests both record `tsclaude 0.1.0` without a commit, so the manifest alone cannot
    identify which build dropped them.
  - The cause is not yet diagnosed. It may already be fixed in AP-1 (B08 kept its row).
  - **Impact:**
    - the canonical XRP and SOL datasets each lack one real minute;
    - any backtest range containing those minutes grades X;
    - parity claims against canonical for those months are invalid.
  - **O-2 sweep (owner, 2026-10-28, report `evidence/owner_checks/o2_canonical_rows_20261028T014419Z.*`, HEAD
    `ebefbc1`):**
    - 240 canonical source archives (5 symbols × 2022-01 → 2025-12; the 2026 holdout was not opened);
    - 233 OK; 5 OK with the known malformed 2023-03 halt line; **exactly 2 mismatches**, the two rows above
      (XRP line 3925, SOL line 29724);
    - **no other count-level row loss** within that scope.
    - The sweep cannot detect a value altered without a row lost. Value-level verification still needs parity
      with an independent re-import (see the C-1 plan in `devlog/2026-10-28-m2-decision-recommendations.md`).
- **C-2, genuine source gap:** SOL 2023-02-14 16:39 is absent from the source file itself. It is a real missing
  observation (class M), not an interruption.
- **C-3, the canonical 2023-03-24 signature:** identical in all five symbols (see ADR-0013).

## Recommended next steps (each needs owner approval; none is authorized by this document)
1. **Approve or amend** ADR-0013, ADR-0014 and ADR-0015, and the RESEARCH_PROTOCOL §6 clarification.
2. **Read-only owner audit of canonical rows** (cheap; no import): for every canonical part, compare the
   manifest's source row count with the number of data lines in the cached ZIP. A script is in
   `docs/devlog/2026-10-28-m2-policy-design.md` (Owner action O-2). Any mismatch is a C-1-type defect.
3. **Diagnose C-1** in the importer (a code investigation), then repair the affected canonical months with a
   targeted owner re-import, never a hand edit.
4. **Then** decide B01–B12 acceptance per the "Proposed path" column, and whether to promote B04 and B09–B12
   into canonical.
