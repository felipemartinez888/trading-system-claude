# Owner validation package — O-2 (canonical row audit) and O-3 (B10 verify)

- **Both checks run in a NORMAL PowerShell window. No administrator rights are needed or wanted.** If Windows
  asks for elevation, cancel and report it.
- **Both are read-only on data.** No network, no imports, no changes to `data/`, `data_batches/`, the lake,
  catalogs or the holdout. Each writes only its own new report files (paths below) and refuses to overwrite an
  existing report.
- **Prepared and tested by Claude** on 2026-10-28 against the known cases. The O-2 script ran on 4 individual
  files: XRP 2025-04, SOL 2022-12, BTC 2023-03 and ETH 2024-06.
- **Code:** `scripts/owner_checks/o2_canonical_row_audit.py` (uncommitted). O-3 uses the existing
  `tsclaude data verify`.

## Before you start (optional hardware note)
Note the current WHEA count, so a run can be told apart from a hardware event:
```powershell
# NORMAL — no administrator. Reads the System event log. If access is denied, skip this step.
(Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-WHEA-Logger'} -ErrorAction SilentlyContinue | Measure-Object).Count
```

## O-2: canonical missing-row audit
- **What it does:** for every canonical 1m dataset in `data/imports` and every monthly source archive
  **outside the 2026 holdout** (which is never opened), it:
  1. counts the data lines in the cached ZIP (`data/archive_cache/...`);
  2. compares them with the row count the manifest recorded at import;
  3. lists every source row whose minute lies inside a gap that canonical QA reported, meaning the row exists at
     the source but is missing from the lake;
  4. treats a malformed source line (a truncated candle) as explained, not as a mismatch.
- **What it cannot detect:** a value altered without a row being lost. Only an independent re-import with
  parity can show that (see the C-1 plan).
- **Resources:**
  - about 240 ZIPs (5 symbols × 48 months, 2022-01 → 2025-12), about 0.7 GB read sequentially;
  - under 200 MB of RAM (one file at a time), one CPU core;
  - **about 1 minute** (0.1 s per file in the test);
  - no network; writes about 100 KB.
- **Safeguards:**
  - refuses to run outside the repository root;
  - refuses to overwrite its report files;
  - `python -I` (isolated mode);
  - skips 2026 holdout archives;
  - never opens the lake (Parquet or DuckDB).

```powershell
# NORMAL — no administrator. Read-only audit; writes two new files under evidence\owner_checks\.
cd C:\Users\felip\Projects\trading-system-claude
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
New-Item -ItemType Directory -Force evidence\owner_checks | Out-Null
git rev-parse HEAD | Out-File -Encoding utf8 "evidence\owner_checks\o2_$stamp.git_head.txt"
.\.venv\Scripts\python.exe -I scripts\owner_checks\o2_canonical_row_audit.py . "evidence\owner_checks\o2_canonical_rows_$stamp.json" "evidence\owner_checks\o2_canonical_rows_$stamp.txt"
```
**Output:**
- `evidence\owner_checks\o2_canonical_rows_<UTC stamp>.json` (full detail);
- `evidence\owner_checks\o2_canonical_rows_<UTC stamp>.txt` (summary, also printed);
- `evidence\owner_checks\o2_<UTC stamp>.git_head.txt`.

**Expected result,** if the two known cases are the only defects:
```
files checked: 240; mismatching files: 2; source rows missing from canonical: 2; files OK with malformed lines: 5; …
MISMATCH data/spot/monthly/klines/SOLUSDT/1m/SOLUSDT-1m-2022-12.zip recorded=44639 zip_lines=44640 malformed=[] rows_in_canonical_gaps=['2022-12-21T15:23:00Z']
MISMATCH data/spot/monthly/klines/XRPUSDT/1m/XRPUSDT-1m-2025-04.zip recorded=43199 zip_lines=43200 malformed=[] rows_in_canonical_gaps=['2025-04-03T17:24:00Z']
```
The order of the two lines may differ. The 5 "OK with malformed lines" files are the 2023-03 archives of the
five symbols (the truncated 12:39 candle on line 33880).

**Interpreting other results:**
- **More MISMATCH lines** are more C-1 defects; send them all.
- **`NOT_CACHED` lines** mean the archive is no longer in `data/archive_cache` and that month cannot be checked.
  Report them.
- **A Python error:** stop and send the full error text.

## O-3: B10 integrity and source verification
- **What it does:** `tsclaude data verify --all` on the isolated B10 lake. It re-hashes every lake part and
  every cached source archive against the manifest. This is the step B10 lacks; all other batches already have
  it.
- **Resources:** 24 parts (about 88 MB) plus 28 cached archives; well under 1 GB of RAM; **under 2 minutes**; no
  network.
- **Safeguards:** it refuses if B10's `verify.json` already exists (so nothing is overwritten); it only reads
  `data_batches\b10_spot_avax_2024_2025`; the summary is written next to the other batches' evidence.

```powershell
# NORMAL — no administrator. Read-only verification of the B10 lake parts and cached sources; writes one new verify.json.
cd C:\Users\felip\Projects\trading-system-claude
$out = 'evidence\imports\b10_spot_avax_2024_2025\verify.json'
if (Test-Path -LiteralPath $out) { throw "refusing to overwrite $out" }
.\.venv\Scripts\python.exe -m tsclaude.cli data verify --data-dir data_batches\b10_spot_avax_2024_2025 --all --summary $out
```
**Output:** `evidence\imports\b10_spot_avax_2024_2025\verify.json`.

**Expected** (same shape as B11 and B12): `"ok": true`, `"verified": 1`, and one report with `parts_checked` 24,
`rows` 1,052,640, `sealed_parts` 0, `sources_checked` 28, `errors` `[]`.

If `ok` is false or `errors` is not empty, stop and send the file.

## After both checks
Run the WHEA count again (optional). Then send back:
1. the O-2 `.txt` summary, plus the `.json` if there is any mismatch beyond the two expected;
2. the B10 `verify.json`;
3. the WHEA counts before and after, and any unexpected shutdown or crash.

Neither check changes any acceptance label or dataset; the results feed decisions P-7 and P-8.

## Results (owner, 2026-10-28; reconciled by Claude against the local reports)
- **O-2: PASS.** `evidence/owner_checks/o2_canonical_rows_20261028T014419Z.{txt,json}`, HEAD `ebefbc1`
  (`o2_20261028T014419Z.git_head.txt`), Python 3.13.2, 13.9 s.
  - 240 files (48 per symbol): 233 OK, 5 OK with the 2023-03 malformed halt line, and 2 mismatches, exactly the
    expected XRP 2025-04-03 17:24 (line 3925) and SOL 2022-12-21 15:23 (line 29724).
  - No `NOT_CACHED`; no 2026 archive opened.
- **O-3: PASS.** `evidence/imports/b10_spot_avax_2024_2025/verify.json`: `ok` true, `verified` 1, dataset
  `d10de5e2a6c1…`, 24 parts, 1,052,640 rows, 0 sealed parts, 28 sources, no errors.
  - A second, accidental run stopped at the overwrite guard, as designed.
- **Classifications:** no acceptance classification changed (B10 stays PENDING_VERIFICATION).
