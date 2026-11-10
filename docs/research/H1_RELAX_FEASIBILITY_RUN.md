# H1 relaxation ladder: P&L-free feasibility count (owner PC, exact steps)

**What this is:** the outcome-blind count that applies the owner's pre-stated selection rule
([D-25](../DECISIONS_PENDING.md#2026-11-10-h1-research-decisions-d-25)).
- The H1-v3 rule is counted at 1h with the compression threshold `rank_max` relaxed to 0.5, 0.75 and 1.0. At 1.0 the compression condition is switched off.
- The least-relaxed value whose power plan meets T1, T2 and T3 is chosen (definitions below).
- If no value meets all three, H1 is recorded as infeasible and H2 is drafted next.

**What it does not do:**
- no backtest, no trial registry row, no outcome field, no holdout read;
- no network call by `tsclaude`, no API key, no order;
- no write to the lake. It writes only `<data>\feasibility\` and the evidence folder below.

This file holds commands only; it contains no result. The commands are parsed by the real CLI and
rehearsed on a synthetic lake by `tests/unit/backtest/test_owner_relax_feasibility_run.py`.

**Shell:** every block is **NORMAL PowerShell** (Windows PowerShell 5.1 or PowerShell 7). No
Administrator rights are needed or wanted.

**What is counted:**
- The grid `docs\research\grids\feasibility_h1_relax_probe.json` holds three configurations. Each is the H1 draft defaults at 1h (`vol_z_min` 1, `stop_atr` 1.5, the grid's most permissive values) with a different `rank_max`.
- `tp_r` enters no entry stage, so each count is the candidate ceiling for every configuration with that `rank_max`.
- The range is the development segment from the planned anchor: 2022-02-01 → 2025-01-01, on the D-F1 universe (spot BTC, ETH, DOGE, XRP).

## Selection rule (fixed before the count; D-25)
All three thresholds are read from the power plan of each count, with these settings:
- `--anchor 2022-02-01`, `--embargo-minutes 4320`, `--configurations 24`;
- `--tp-r 2.5,3,3.5` (take-profit set B);
- alpha 0.05 divided by the planned trial count; power 0.80; `n_min` 300.

| | Met when | Field in the power plan |
|---|---|---|
| **T1** | every training window, fold 0 included, can reach 300 trades | `training_windows.selection_possible_in_every_fold` is `true` |
| **T2** | at least 300 candidates fall inside the test windows | `oos_candidates` ≥ 300 |
| **T3** | the smallest detectable win-rate margin is at most 10 percentage points for **every** take-profit of set B, at `n_eff_ratio` 1.0 | every `geometries[].by_n_eff[n_eff_ratio = 1.0].min_detectable_margin` ≤ 0.10 |

Then:
- The chosen value is the smallest `rank_max` of {0.5, 0.75, 1.0} that meets T1, T2 and T3.
- A value that is undetermined (`selection_possible_in_every_fold` is `null`) does not meet T1.
- The thresholds are not changed after the count.

**What the thresholds mean, and their limits.**
- The candidates are an upper bound on trades. Position overlap, consecutive breakout bars on one symbol, portfolio rules and exits are not simulated. The overstatement probably grows as compression is relaxed.
- Correlated trades lower the effective sample. The power plan also shows `n_eff_ratio` 0.75 and 0.5, which are recorded as diagnostics, not thresholds.
- So T1–T3 are necessary for a selection, not sufficient. A walk-forward can still end `INSUFFICIENT_OOS_EVIDENCE`.
- A higher count is not evidence of an edge.

## Before you start
- **Shell:** one **NORMAL PowerShell** window (no Administrator). Run steps 1–6 in order in that same window, because later steps reuse `$ev` and `$stop`.
- **Paste each block whole.** Steps 2–6 are guarded: if an earlier step set `$stop`, they print `STOPPED` and do nothing, so a failed or empty count can never produce an evidence zip.
- **Time:** unmeasured. The earlier two-configuration count took about 30 minutes per configuration, judging from its file times. Expect roughly 1–1.5 h for the three configurations, plus a few minutes the first time `uv` builds this worktree's environment.
- **Resources:** it reads the lake read-only. The count earlier ran on this PC with no memory problem. The outputs are small (well under 50 MB); the new `.venv` needs a few hundred MB. Network is needed only if `uv` must download packages for that environment.
- **Output:** `%USERPROFILE%\tsc_evidence\h1-relax-feasibility-<yyyyMMdd-HHmm>\` and a zip of it next to it. The count's own copies stay in `<data>\feasibility\`.
- **A native crash** (exit `-1073741819`, as in E-1 and E-2) sets `STOP`. Rerunning from step 1 is harmless, and you should report it.

## 1. Code identity and evidence folder

```powershell
$stop = $false
cd C:\Users\felip\Projects\tsc-m4-r1
git log -1 --oneline
$env:TSC_DATA_DIR = 'C:\Users\felip\Projects\trading-system-claude\data'
if (-not (Test-Path $env:TSC_DATA_DIR)) { $stop = $true; "STOP: data folder not found: $env:TSC_DATA_DIR" }
$dirty = @(git status --short | Where-Object { $_ -match ' (src|configs)/' })
if ($dirty.Count -gt 0) { $stop = $true; 'STOP: src/ or configs/ differ in this worktree:'; $dirty }
$ev = Join-Path $env:USERPROFILE ('tsc_evidence\h1-relax-feasibility-' + (Get-Date -Format 'yyyyMMdd-HHmm'))
New-Item -ItemType Directory -Force $ev | Out-Null
git rev-parse HEAD | Out-File -Encoding ascii "$ev\code_commit.txt"
git status --short | Out-File -Encoding ascii "$ev\git_status.txt"
uv run tsclaude version | Out-File -Encoding ascii "$ev\tsclaude_version.txt"
if ($LASTEXITCODE -ne 0) { $stop = $true; "STOP: tsclaude version exit=$LASTEXITCODE" }
"evidence folder: $ev | stop=$stop"
```

Expected: the last line ends in `stop=False`.

## 2. Count the three `rank_max` values (one run)

```powershell
if ($stop) { 'STOPPED: fix the reason printed above, then restart from step 1' } else {
  uv run tsclaude backtest feasibility --draft --strategy h1_compression_breakout --grid docs\research\grids\feasibility_h1_relax_probe.json --symbols 'spot:BTCUSDT,spot:ETHUSDT,spot:DOGEUSDT,spot:XRPUSDT' --from 2022-02-01 --to 2025-01-01 | Out-File -Encoding utf8 "$ev\feasibility_summary.json"
  $code = $LASTEXITCODE
  $summary = Get-Content "$ev\feasibility_summary.json" -Raw | ConvertFrom-Json
  foreach ($r in $summary.results) { $rank = ([double](Get-Content $r.path -Raw | ConvertFrom-Json).strategy.params.rank_max).ToString([cultureinfo]::InvariantCulture); Copy-Item $r.path "$ev\feasibility_1h_rank$rank.json" }
  $made = @(Get-ChildItem "$ev\feasibility_1h_rank*.json").Count
  if ($code -ne 0 -or -not $summary.ok -or $made -ne 3) { $stop = $true; "STOP: count exit=$code ok=$($summary.ok) files=$made (expected 0 / True / 3); send feasibility_summary.json" } else { 'count OK: 3 files' }
}
```

Expected: `count OK: 3 files`. The files are `feasibility_1h_rank0.5.json`, `feasibility_1h_rank0.75.json` and
`feasibility_1h_rank1.json`. Their names use the invariant culture, so a comma decimal separator cannot change them.

## 3. Dataset identity

```powershell
if ($stop) { 'STOPPED: see the reason above' } else {
  $ids = @()
  foreach ($f in Get-ChildItem "$ev\feasibility_1h_rank*.json") { $ids += (Get-Content $f.FullName -Raw | ConvertFrom-Json).dataset_ids }
  $ids = @($ids | Sort-Object -Unique)
  $ids | Out-File -Encoding ascii "$ev\dataset_ids.txt"
  $n = 0
  foreach ($id in $ids) { $n++; uv run tsclaude data verify $id --summary "$ev\data_verify_$n.json" | Out-Null; "$id exit=$LASTEXITCODE"; if ($LASTEXITCODE -ne 0) { $stop = $true } }
  if ($ids.Count -eq 0) { $stop = $true }
  if ($stop) { 'STOP: a dataset failed verification or none was listed; send the folder' } else { "datasets OK: $($ids.Count) verified" }
}
```

Expected: one `exit=0` line per dataset, then `datasets OK`. `exit=2` means a file or hash does not match the manifest.

## 4. Power plans (take-profit set B)

```powershell
if ($stop) { 'STOPPED: see the reason above' } else {
  foreach ($f in Get-ChildItem "$ev\feasibility_1h_rank*.json") {
    $tag = $f.BaseName.Replace('feasibility_', '')
    uv run tsclaude backtest power-plan --feasibility $f.FullName --anchor 2022-02-01 --embargo-minutes 4320 --configurations 24 --tp-r '2.5,3,3.5' --out "$ev\power_$tag.json" | Out-Null
    "$tag exit=$LASTEXITCODE"
    if ($LASTEXITCODE -ne 0) { $stop = $true }
  }
  $plans = @(Get-ChildItem "$ev\power_1h_rank*.json").Count
  if ($stop -or $plans -ne 3) { $stop = $true; "STOP: a power plan failed or is missing (files=$plans, expected 3)" } else { 'power plans OK: 3 files' }
}
```

## 5. Threshold read-out (display only; the decision is recorded in the repository)

```powershell
if ($stop) { 'STOPPED: see the reason above' } else {
  foreach ($p in Get-ChildItem "$ev\power_1h_rank*.json") {
    $j = Get-Content $p.FullName -Raw | ConvertFrom-Json
    $m = @($j.geometries | ForEach-Object { ($_.by_n_eff | Where-Object { $_.n_eff_ratio -eq 1.0 }).min_detectable_margin })
    '{0}: T1 every-fold={1} fold0={2} | T2 oos={3} | T3 margins={4}' -f $p.BaseName, $j.training_windows.selection_possible_in_every_fold, $j.training_windows.folds[0].candidates_ceiling, $j.oos_candidates, ($m -join '/')
  }
}
```

How to read the margins:
- They are fractions of win rate (0.10 = 10 points), one per take-profit of set B (2.5 / 3 / 3.5 R).
- An empty margin means no margin at all is detectable with that count. It does not meet T3.

## 6. Hashes and hand-over

```powershell
if ($stop) { 'STOPPED: no evidence package was made; send the folder as it is' } else {
  Get-FileHash -Algorithm SHA256 -Path "$ev\*.json", "$ev\*.txt" | ForEach-Object { '{0}  {1}' -f $_.Hash.ToLower(), (Split-Path $_.Path -Leaf) } | Out-File -Encoding ascii "$ev\SHA256SUMS.txt"
  Compress-Archive -Path "$ev\*" -DestinationPath "$ev.zip"
  "evidence: $ev.zip"
}
Remove-Item Env:\TSC_DATA_DIR -ErrorAction SilentlyContinue
```

`SHA256SUMS.txt` covers every generated file:
- the count summary and the three counts;
- each `data_verify_<n>.json` and `dataset_ids.txt`;
- the three power plans;
- the code identity files.

Send the zip. It holds:
- the three counts and their diagnostics;
- the dataset ids and their verification;
- the three power plans;
- the code identity.

It holds no P&L, no trade and no price after a decision bar.

## What happens next
1. The counts are checked: content hashes, config hashes and dataset ids. They are reported as measured, including if no value qualifies.
2. The rule above picks the variant, or records H1 as infeasible.
3. Only then is a revised H1 preregistration (H1-v4) drafted. A relaxed compression rule is a revised hypothesis and needs its own preregistration, review and freeze before any official test.

Running this again is harmless. The output folders are named by content hash, and no trial is consumed.
