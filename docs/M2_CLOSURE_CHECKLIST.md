# M2 closure checklist (2026-11-06, proposal; changes no acceptance label)

Worktree `tsc-m2-closure`, detached at `928aeb7`. Nothing committed. "Run" = executed on current bytes in this session.

**Update 2026-11-06 (later the same day):** S2 was decided (repair) and S5–S6 executed; this file's gate table,
C-1 section and sequence are updated below in place, each change marked with the date. See
[DECISIONS_PENDING D-17](DECISIONS_PENDING.md) and [the D-17 devlog](devlog/2026-11-06-m2-d17-canonical-promotion.md)
for the full evidence. **Still not formal M2 acceptance** (S3/S4/S7 remain open).

**Update 2026-11-06 (publication and CI):**
- **Publication.** The D-17 implementation and governance commits were rebuilt before their first push, to
  withhold 45 sealed-holdout metadata entries from two evidence inventories. They were published as `b24f41e`
  (local `60f5ce4`, never published) and `9f908d9` (local `f18eb9f`, never published).
- **CI.** Run `42322235295` on headSha `9f908d996cbc4b496adbf0869628a9b5f0b02130` is green on all 7 jobs, Ubuntu
  and Windows.
- **Status of the closure steps.** S3 and S4 are done. **S7, the owner sign-off, is the only open step.** The
  limited-scope sign-off package is [M2_ACCEPTANCE_RECORD](M2_ACCEPTANCE_RECORD.md).

## Approved scope (owner, 2026-11-06)
M2 data scope = Binance **Spot 1m** for **BTCUSDT, ETHUSDT, SOLUSDT, DOGEUSDT, XRPUSDT**, **2022-01 to 2025-12**.
The 2026 holdout is out of M2 scope and stays sealed. This approval does **not** accept any dataset and does **not** pass M2.
Out of M2 under this scope (moved to M4 prerequisites): BNB, ADA, AVAX, TRX, HBAR, USD-M, mark price, funding (O-9), B01–B12 acceptance (D-15), promotion of the B01–B12 batches (D-17 remains deferred for those). **Update 2026-11-06:** D-17 was separately authorized and executed, but only for the C-1 repair (SOL 2022-12, XRP 2025-04) — two months of symbols already in this five-symbol scope, not a batch promotion. See the C-1 section below.

## Literal gate (ROADMAP, M2 section)
"look-ahead canaries, golden trades, ledger property tests, window-parity test, cross-OS reproducibility hash — all green; Causality Auditor sign-off."

| # | Requirement | Evidence | Status | Verify | Remaining |
|---|---|---|---|---|---|
| 1 | Look-ahead canaries | `tests/unit/features/test_feat_parity_canaries.py`, `tests/unit/sim/test_sim_causality.py`, `tests/unit/backtest/test_m2t4_causality_audit.py` | PASS (run: 258 passed incl. items 2, 4) | `uv run pytest -q tests/unit/sim/test_sim_golden.py tests/unit/sim/test_sim_causality.py tests/unit/sim/test_sim_determinism.py tests/unit/features/test_feat_parity_canaries.py tests/unit/backtest tests/unit/archive/test_arc_import_integrity.py` | none |
| 2 | Golden trades | `tests/unit/sim/test_sim_golden.py` | PASS (run) | as above | none |
| 3 | Ledger property tests | `tests/property/sim/` | PASS (run: 10 passed; restart property delayed 25 cases / instant 24 cases, FAIL 0, 2+2 SAFE_BLOCK = fail-closed stops, not recovery) | `uv run pytest -q tests/property/sim` | **done 2026-11-06:** roadmap row (a) reworded and validation-plan blocker 7 marked resolved (stale) — `test_sim_property_regressions.py -k 13073`: 8 passed; full property suite 79 passed |
| 4 | Window-parity | `test_bt_runner.py`, `test_feat_parity_canaries.py` | PASS (run, inside the 258) | as item 1 | none |
| 5 | Cross-OS reproducibility hash | see "Cross-OS" below | **Updated 2026-11-06 (publication):** both OS PASS on the published HEAD `9f908d9`, CI `42322235295` (`m2 cross-os gate` on ubuntu and windows: both pinned-hash tests PASSED; full suite: ubuntu 1,729 passed, windows 1,727 passed). Earlier run `42275943593` on `b1da844` kept as history | CI on `ubuntu-latest` | none. The D-17 implementation is published as `b24f41e` (local `60f5ce4`, never published) and is an ancestor of the CI-covered HEAD |
| 6 | Causality Auditor sign-off (global M2) | see "Auditor package" below | **Updated 2026-11-06: CONDITIONAL PASS** ([D-17 devlog](devlog/2026-11-06-m2-d17-canonical-promotion.md)) | `causality-auditor` agent | the verdict stands as issued (history). Its three conditions were satisfied afterwards: the governance record (`9f908d9`); CI on the implementation (`42322235295`, a descendant of `b24f41e`); the sealed-source guard test. The auditor has not re-issued a verdict; see [M2_ACCEPTANCE_RECORD §3](M2_ACCEPTANCE_RECORD.md) |

## C-1 (two real rows missing in canonical; in scope because both symbols are in scope)
Current bytes (run, read-only, tool `tests/owner_checks/c1_verify.py` against canonical):
- XRP 2025-04: source 43,200 rows, canonical 43,199; the only difference is `2025-04-03T17:24:00Z`; SHA-256 of the zip equals its CHECKSUM; all 43,199 common rows equal in all 9 value fields.
- SOL 2022-12: source 44,640 rows, canonical 44,639; the only difference is `2022-12-21T15:23:00Z`; same checks pass.
- SOL 2023-02-14 16:39 (C-2) is absent from the source itself: genuine gap, class M, stays grade X. Not part of C-1.
Canonical was **not** modified by anything below this line; D-17 (S2/S5) is documented separately.

**Update 2026-11-06: C-1 repaired.** The owner decided S2 = repair and the D-17 tool executed it once against
the real canonical lake (op id `45d9e72abf9c43458370a0b0a7e9e647`, `COMPLETE`/`accepted: true`): SOL 2022-12
44,639→44,640 rows, XRP 2025-04 43,199→43,200 rows; exactly 4 canonical objects added, 0 modified/deleted. Full
evidence: [DECISIONS_PENDING D-17](DECISIONS_PENDING.md), [D-17 devlog](devlog/2026-11-06-m2-d17-canonical-promotion.md).
**`c1_verify` must not be rerun on the canonical lake now**, as the D-17 promotion plan already warns: it reads
every Parquet file in the month folder, so the old and new parts together would be reported as duplicate rows.
Post-repair verification instead used a fresh `LakeReader` (unpinned and pinned to both the new and the old
dataset id), independently of the D-17 tool's own postcondition check; see the devlog for the exact results.

### Owner procedure: fresh-destination re-import of the two months (NORMAL, no administrator)
Bounded: 2 months, about 90,000 rows, seconds. Reads sources only from the batch caches (`data_batches/b08...`, `data_batches/b06...`), never from `data\`. Writes only `C:\Users\felip\Projects\data_c1_check`. Offline (`--cross-check-months 0`); no network use is expected, and if the importer tries to download, stop and report.
```powershell
# NORMAL
$main='C:\Users\felip\Projects\trading-system-claude'; $wt='C:\Users\felip\Projects\tsc-m2-closure'
$d='C:\Users\felip\Projects\data_c1_check'; if (Test-Path $d) { throw "$d exists: choose a new folder" }
$jobs=@(
  @{s='XRPUSDT'; ym='2025-04'; last='2025-04-30'; cache="$main\data_batches\b08_spot_xrp_2024_2025\archive_cache"},
  @{s='SOLUSDT'; ym='2022-12'; last='2022-12-31'; cache="$main\data_batches\b06_spot_sol_2024_2025\archive_cache"})
Set-Location $wt
foreach ($j in $jobs) {
  $rel="data\spot\monthly\klines\$($j.s)\1m"; $dst="$d\archive_cache\$rel"
  New-Item -ItemType Directory -Force $dst | Out-Null
  Copy-Item "$($j.cache)\$rel\$($j.s)-1m-$($j.ym).zip*" $dst      # zip + .CHECKSUM, copied, originals untouched
  uv run --frozen tsclaude data import --data-dir $d --market spot --symbols $j.s --from "$($j.ym)-01" --to $j.last --concurrency 1 --cross-check-months 0 --summary "$d\summary_$($j.s).json"
  if ($LASTEXITCODE -ne 0) { throw "import $($j.s) exit $LASTEXITCODE" }
}
uv run --frozen tsclaude data verify --data-dir $d --all --summary "$d\verify.json"
if ($LASTEXITCODE -ne 0) { throw "verify exit $LASTEXITCODE" }
foreach ($j in $jobs) {
  $z="$d\archive_cache\data\spot\monthly\klines\$($j.s)\1m\$($j.s)-1m-$($j.ym).zip"
  uv run --frozen python -I tests\owner_checks\c1_verify.py $z "$z.CHECKSUM" "$d\lake" $j.s $j.ym "$d\c1_$($j.s)_$($j.ym).json"
  "c1_verify $($j.s) exit $LASTEXITCODE"
}
```
Expected (hypothesis, not a target): both `c1_verify` exits are 0 (no finding), `rows` 43,200 and 44,640, verify ok. Any finding stops the plan for an owner decision; do not retry, do not edit data.
Preserve `$d` untouched as evidence. Do not copy anything into `data\`.

### How the re-imported records are checked (independent chains)
| Question | Check | Independent of the importer? |
|---|---|---|
| Is the source the exchange's file? | ZIP SHA-256 == Binance `.CHECKSUM` (S1), == importer manifest source `sha256`, == canonical manifest's sha256 for the same file | Yes (Binance-published checksum) |
| Timestamps | S2: ms/µs normalised, `close_time == open_time + 59,999`, no duplicate/unordered/out-of-month rows | Yes (stdlib CSV, no tsclaude code) |
| Continuity | S3: all 43,200 / 44,640 minutes present in the source; X1: every source minute present in the new lake and no extra | Yes (Parquet read by duckdb) |
| Values | X2: 9 fields equal per minute, float-exact against the CSV text | Yes |
| Stored bytes | `tsclaude data verify --all`: part hashes, rows, source re-hash | No (same code, but checks the bytes on disk) |
| Cross-importer parity | XRP 2025-04 `content_hash` of the new part must equal B08's part for 2025-04 (independent earlier import by AP-1 build `7be73eb`); SOL has no earlier independent import, so only the four chains above apply | Partly |
| Native exchange cross-check (15m/1h) | Optional, **needs downloads** of 4 public archives (no keys): rerun the two imports with `--cross-check-months 1` after owner approval | Yes, but not required: the whole month is already checked against the source minute by minute |

## Cross-OS reproducibility (gate item 5)
The existing tests already define the artifacts; no new test is needed. Both platforms must independently reproduce, byte for byte:
1. `PINNED_FINGERPRINT_SEED0 = 93ecca1026bf93241ae991a632d471ea0a4e3df2087b735961a09cd5f2049089` (`tests/unit/sim/test_sim_determinism.py::test_golden_fingerprint_is_pinned`, simulator event stream).
2. `GOLDEN_MANIFEST = 519558a8…7b58c5`, `GOLDEN_TRADES = 2dff9ce6…52e6e6` (SHA-256 of `trades.parquet`) and `GOLDEN_METRICS = e32098e5…7c7000` (`tests/unit/backtest/test_bt_runner.py::test_golden_hashes_identical_across_platforms`, runner on a synthetic lake).
Windows: PASS today (inside the 258). Linux: the project has neither WSL nor Docker, so the only independent Linux executor is the CI matrix (`ubuntu-latest` + `windows-latest`, `.github/workflows/ci.yml`, triggered by a push to `task/**`). Evidence = one CI run in which both OS jobs run `uv run pytest -q` (whole suite, plus ruff and mypy) green, with the run id recorded. A pass on both platforms of the same pinned constants is the reproducibility proof; the constants must not be edited to make it pass.

## Auditor package (gate item 6): minimum evidence
Scope: the M2 surface (importer, lake, QA, simulator, runner, statistics), at the commit that carries the CI run.
1. Commit hash + CI run id (both OS green) and this checklist.
2. Per-package audit records already on file: E-1 PASS, E-1b PASS, E-2 PASS (after the fix), M2-T4 attack tests (`test_m2t4_causality_audit.py`, now green).
3. Current-bytes test output of items 1–4 (258 + 10 passed) and the golden/pinned hashes.
4. C-1 verdict: the `c1_verify` JSONs (canonical, and the fresh re-import) and the `data verify` JSON.
5. Standing limits to re-confirm: holdout never opened (`sealed_parts` 0, `lake_sealed` untouched), fills not earlier than next bar open, no sim/risk contract change since the E-1/E-2 audits other than M3 runtime work.
6. Stated limits, not hidden: SOL 2023-02-14 16:39 (C-2) and the 2023-03-24 halt (ADR-0013) are known source facts; canonical values 2022–2023 (ETH, SOL, DOGE, XRP) and BTC 2025 have no independent re-import parity (O-5/O-6 not run); the E-2 mutants partly ran on a prior revision.
The auditor does not need the Parquet data except the `c1_verify` reports.

## Shortest closure sequence (genuine blockers only)
| Step | Action | Who | Blocker? |
|---|---|---|---|
| S1 | Run the C-1 procedure above; read the exits and the XRP `content_hash` comparison | owner (NORMAL) | no, seconds — **done** |
| S2 | D-17 minimal decision: repair canonical months by replacing the two parts with the verified ones (via a logged, non-overwriting step, old manifest kept), **or** accept C-1 as documented grade X on those minutes | owner | **done 2026-11-06: repair**, authorized in writing and executed (op id `45d9e72a…`) |
| S3 | Approve a push of one branch `task/m2-closure` (D-07) so CI runs both OS | owner | **done 2026-11-06:** normal fast-forward push `b1da844..9f908d9` to `origin/task/m2-closure`, owner-authorized; the local unredacted commits `60f5ce4`/`f18eb9f` were not published |
| S4 | Commit the checklist, `c1_verify.py`, the roadmap/validation-plan rewording, the D-17 tool and this reconciliation; CI green on both OS for the commit that carries all of it | Claude after approval | **done 2026-11-06:** `a4e4f9e`, `b1da844`, `b24f41e`, `9f908d9`; CI `42322235295` 7/7 green on `9f908d9` |
| S5 | If S2 = repair: do it; verify independently (not `c1_verify`, which would double-count the old and new parts — see above) | Claude/owner | **done 2026-11-06:** executed; verified with a fresh `LakeReader`, unpinned and both pins, 0 duplicates/missing/conflicts, old pin unchanged |
| S6 | Global `causality-auditor` run on the package above | Claude | **done 2026-11-06: CONDITIONAL PASS** — see the D-17 devlog |
| S7 | Owner sign-off of M2 with the five-symbol scope | owner | final — **still open**; proposed text and caveats in [M2_ACCEPTANCE_RECORD](M2_ACCEPTANCE_RECORD.md) |
Not blockers under the approved scope: Top 10, USD-M, funding, B01–B12, QA v2 for other batches, E-4(b), O-5/O-6 (a parity re-import of 108 months is optional hardening; a data-quality caveat in the sign-off covers it, unless the owner wants it as a gate).
