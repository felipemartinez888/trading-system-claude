# Devlog — 2026-10-28 — M2 data-policy design (documentation and proposals only)

- **Authorization:** the owner's "documentation and policy-design phase", which follows M4-C. **Nothing here
  changes** backtesting rules, QA code, research grading, contracts, historical data or canonical datasets. No
  backtest or import was run. No merge or push.
- **Branch:** `feat/mvp-4-dashboard` (after `7295082`), uncommitted.
- **Deliverables:**
  - [ADR-0013](../adr/0013-exchange-wide-interruptions.md): exchange-wide interruptions (Proposed);
  - [ADR-0014](../adr/0014-zero-volume-candles.md): zero-volume candles (Proposed);
  - [ADR-0015](../adr/0015-qa-explained-open-difference.md): narrow QA open exception (Proposed);
  - [M2 dataset acceptance matrix](../M2_DATASET_ACCEPTANCE.md);
  - the RESEARCH_PROTOCOL §6 clarification (below, **not applied**);
  - the `docs/ROADMAP.md` status update.

## New evidence gathered (read-only)
- **The 2023-03-24 signature is identical in all five canonical symbols** and in B03:
  - a zero-trade run from 11:28 to 12:38 (71 minutes);
  - a truncated candle at 12:39 on line 33880 of each file (rejected as malformed);
  - absent minutes from 12:40 to 13:59; trading resumes at 14:00 UTC.
  - It is the only zero-trade run of 3 or more minutes in about 4.75 years of these symbols.
  - External reports agree: a matching-engine bug, cancel-only until about 13:40 UTC, and resumption at 14:00 UTC
    (TechCrunch, Forklog, FinanceFeeds, Kitco, 2023-03-24).
- **The five AVAX/HBAR open differences:**
  - In each case the bucket's first 1m candle is fully flat at the previous close, with every volume field and
    the trade count at 0.
  - The native 15m open equals the 2nd minute's open, and every other field matches.
  - The ADR-0015 evidence table lists the file SHA-256 values and line numbers.
- **Independent parity:** the batch part content hashes were compared with canonical `data/`.
  - Identical: B01 (1/1), B02 (12/12), B03 (24/24), B05 (24/24), B06 (24/24), B07 (24/24).
  - **B08 XRP: 23/24.** Month 2025-04 has 43,200 rows in the batch versus 43,199 in canonical, from the same
    source file (SHA-256 `a36194224399…`).
- **Canonical import defect (new):** two ordinary rows are present in the verified source archives but absent
  from canonical `data/`:
  - XRP 2025-04-03 17:24 (`XRPUSDT-1m-2025-04.zip`, line 3925);
  - SOL 2022-12-21 15:23 (`SOLUSDT-1m-2022-12.zip`, line 29724).

  The earlier importer build used for the 2026-10-24 canonical imports dropped them while parsing. QA reported
  each only as a gap, with no malformed or rejected record. Manifests record `tsclaude 0.1.0` without a commit,
  so the build cannot be identified from the manifest. In contrast, SOL 2023-02-14 16:39 is genuinely absent
  from its source file.
- **All 12 batches:** `NORMAL_COMPLETION`, exit code 0, and 0 WHEA or unexpected-shutdown events during their runs.
- **Simulator observation (for ADR-0014):**
  - The slippage model is fixed and ignores volume.
  - Orders can fill on zero-trade candles, including the 71 frozen minutes before the 2023 halt.

## Proposed RESEARCH_PROTOCOL §6 clarification (not applied; needs owner approval)
The signal contract is unchanged: `evidence_grade: str`, one of A/B/C/D/X (`contracts/signals.py:126`).
Proposed text, added after the grade table in `docs/RESEARCH_PROTOCOL.md` §6:

> **Reference-only records.** Signals and runs of a strategy labelled `REFERENCE` (for example
> `bracket_reference`, which exercises the paper-trading pipeline) carry grade `X` because no graded evidence
> exists for them, not because an integrity check failed. For such records `X` means **absence of validated
> evidence**. It is never a validation result, and it still blocks promotion to the live scanner (§7 requires
> grade ≥ C). Every consumer, the dashboard included, must check the `REFERENCE` label before interpreting the
> grade and must show such records as "REFERENCE / NOT VALIDATED". For any record without the `REFERENCE`
> label, `X` keeps its integrity-failure meaning.

**Consequential §6 wording if ADR-0013 is approved:** the X row would read "Integrity failure: data gaps in
range (minutes inside a registered exchange interruption, ADR-0013, are reported separately and are not gaps),
look-ahead canary failure, missing manifest fields."

**Option for M4, with its own ADR:** a contract amendment that separates `validation_status` from
`evidence_grade`. It is not proposed now.

## Decisions requiring owner approval (none is implemented)
| ID | Decision | Recommendation |
|---|---|---|
| P-1 | Accept ADR-0013: a registry of exchange-wide interruptions with an objective, source-level admission rule; registered minutes are not counted as missing; no trading inside them; positions closed at the first traded open after the interval (aligned with L-11); flagged trades counted into the `spans_gap` share | Approve, **together with P-4** (otherwise backtest and paper diverge on zero-trade candles) |
| P-2 | Admit `binance-spot-2023-03-24` = [11:28, 14:00) UTC, spot only, including the 71 zero-trade minutes (the alternative is the absent part only, [12:39, 14:00)) | Approve the full interval (evidence above); USD-M decided separately once its data exists |
| P-3 | Accept ADR-0014 classification (Z1, Z2, M, A) and QA reporting | Approve |
| P-4 | ADR-0014 rule 4: no fills on zero-trade candles (simulator change, version bump) | Approve in principle; implement after verifying the live-kline behaviour |
| P-5 | Accept ADR-0015: the narrow `leading_zero_trade_open` explained difference | Approve |
| P-6 | Apply the §6 clarification text above | Approve |
| P-7 | B01–B12 acceptance per the matrix's "Proposed path" column | Decide after P-1/P-5 and owner actions O-2/O-3 |
| P-8 | Treat canonical defect C-1 as an M2 blocker: diagnose the importer, then do a targeted owner re-import of the affected months | Approve (blocks trustworthy research over XRP 2025-04 and SOL 2022-12) |
| P-9 | Promotion policy for `data_batches/` into canonical `data/` (B04 and B09–B12 add new symbols) | Defer until P-7/P-8 |

**Open tradeoffs and evidence still required, per decision:**
- **P-1:**
  - *Tradeoff:* closing exposed positions at the first traded open after an interval (aligned with L-11) forces
    exits that a human might not take. The alternative, keeping positions open and resolving levels with the
    gap-fill rules, diverges from the live L-11 rule.
  - *Required:* approval together with P-4; a registry hash in every manifest; a canary test that an unregistered
    gap still grades X.
- **P-2:**
  - *Tradeoff:* the full interval [11:28, 14:00) labels the 71 zero-trade minutes as outage; the absent part
    alone, [12:39, 14:00), leaves them as genuine no-trade minutes.
  - *Required:* the start of the halt from an independent source (the reports give the 14:00 UTC resumption,
    but no precise start time). USD-M behaviour in the same window is unknown until USD-M data exists.
- **P-3:**
  - *Tradeoff:* class A after a missing minute is conservative and may block datasets that are in fact
    harmless.
  - *Required:* QA re-run on the cached archives after implementation.
- **P-4:**
  - *Tradeoff:* it changes every backtest result that touched a zero-trade candle and needs a simulator version
    bump.
  - *Required:* verify that the live kline stream emits closed zero-trade candles; golden tests.
- **P-5:**
  - *Tradeoff:* none known beyond keeping the rule narrow.
  - *Required:* the five fixtures plus mutation tests; fail closed at month boundaries.
- **P-6:**
  - *Tradeoff:* "X" keeps two readings, told apart only by the REFERENCE label, until a contract change.
  - *Required:* none beyond the text.
- **P-7:**
  - *Required:* O-3 for B10. P-1 (and the halt evidence) for B03. P-5 and the QA re-run for B10 and B12. The
    owner's own sign-off.
  - Nothing is accepted automatically.
- **P-8:**
  - *Required:* the O-2 sweep result, a root-cause diagnosis of the importer, and a targeted re-import plan
    approved before any canonical change.
- **P-9:**
  - *Tradeoff:* promoting batches adds four new symbols but also re-imports five existing ones.
  - *Required:* P-7 and P-8 resolved; a promotion procedure that never overwrites canonical parts silently.

**Implementation work once approved** (each needs its own brief and approval):
- registry loader and runner accounting (research);
- simulator no-fill window and zero-trade rule (sim);
- QA classification and explained differences (marketdata);
- importer C-1 diagnosis (marketdata);
- report fields;
- tests.

## Owner actions worth delegating (NORMAL PowerShell, no administrator)
O-2 is read-only and writes one report to `%TEMP%`. O-3 is read-only on the data but writes `verify.json` into
`evidence/imports/b10…`, the same file the other batches already have.
**O-2: canonical row audit (C-1 sweep).** About 5 symbols × 57 monthly ZIPs; minutes, not hours. It compares
every canonical source's recorded row count with the data lines in its cached archive.
```powershell
# NORMAL — no administrator. Read-only: reads data\imports manifests and data\archive_cache ZIPs; writes one report in %TEMP%.
cd C:\Users\felip\Projects\trading-system-claude
@'
import io, json, sys, zipfile
from pathlib import Path
root = Path("data")
bad = []
for m in sorted((root / "imports").glob("*/manifest.json")):
    d = json.loads(m.read_text(encoding="utf-8"))
    if d.get("kind") != "bars_1m":
        continue
    for s in d.get("sources", []):
        if s.get("role") != "data" or "-2026-" in s["cache_path"]:
            continue  # data sources only; holdout months (2026) are skipped, never opened
        p = root / "archive_cache" / s["cache_path"]
        if not p.is_file():
            bad.append((d["symbol"], s["cache_path"], "NOT CACHED", s.get("rows"), None)); continue
        with zipfile.ZipFile(p) as z:
            n = sum(1 for line in io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf-8") if line[:1].isdigit())
        if n != s.get("rows"):
            bad.append((d["symbol"], s["cache_path"], "ROWS DIFFER", s.get("rows"), n))
print("mismatches:", len(bad))
for b in bad: print(*b)
'@ | Set-Content -Encoding utf8 "$env:TEMP\tsc_audit_rows.py"
.\.venv\Scripts\python.exe -I "$env:TEMP\tsc_audit_rows.py" | Tee-Object "$env:TEMP\tsc_audit_rows.txt"
```
- **Expected:** at least the two known mismatches, XRP 2025-04 (43,199 recorded vs 43,200 lines) and SOL 2022-12.
- **Send back:** the content of `%TEMP%\tsc_audit_rows.txt`.
- **Interpreting a line:** if a file's data-line count includes a malformed row (2023-03), a difference of 1 there
  is expected, because the rejected truncated candle counts as a line.

**O-3: B10 integrity and source re-check** (seconds):
```powershell
# NORMAL — no administrator. Verifies the B10 lake files and cached sources (read-only); writes verify.json into evidence.
cd C:\Users\felip\Projects\trading-system-claude
.\.venv\Scripts\python.exe -m tsclaude.cli data verify --data-dir data_batches\b10_spot_avax_2024_2025 --all --summary evidence\imports\b10_spot_avax_2024_2025\verify.json
```
Send back the printed summary (`ok`, parts, rows, errors, `sources_checked`).

**O-1 (from MVP-4, still open):** a timed first start with paper enabled, to see the RUNNING page and measure
the 9-day backfill. The procedure is in `2026-10-28-mvp4-dashboard.md`.

## Review
**Independent causality audit:** every factual claim was confirmed against the local evidence (canonical and
batch manifests, QA reports, cached archives, `batch.json` and `verify.json`). It returned FAIL on two text
items, both fixed:
- **ADR-0013 §4 said live and paper need no change.** Zero-trade filler candles are fresh and observed, so
  nothing blocks fills on them today. §4 now states that parity depends on ADR-0014 rule 4 (P-1 goes together
  with P-4), and position handling is aligned with L-11 (close at the first traded open after the interval).
- **ADR-0015 lacked the condition "derived open = first 1m open = filler = previous close".** Without it, an
  aggregation that takes the open from the wrong minute could hide behind the exception. The condition is added,
  and the check fails closed when the candle before the bucket is unavailable (month boundary).

**Non-blocking items also fixed:**
- the per-symbol truncated close times;
- "without a QA finding" corrected (QA reported the C-1 minutes only as gaps);
- the holdout handling of the whole-lake scan, and O-2 skips the 2026 holdout months;
- `missing_warmup_minutes`, and the flag-cap wording;
- feeding of filler bars to strategies, and next-open entries inside the interval;
- the definition of "independently imported";
- the 71 zero-trade minutes stated consistently with P-2;
- the consequential §6 "X" wording;
- ADR-0014 wording on live candles, class A after a missing minute, and the full Z1 definition;
- the ADR-0015 holdout redaction;
- the matrix re-hash column (`data verify` already re-hashed every batch's sources except B10);
- the "not yet in CI" qualifier restored in the roadmap.
