# H1-1 feasibility run on the owner PC: exact steps

**What this is:** the P&L-free count that chooses H1's decision interval (15m or 1h) and gives the
power plan its candidate numbers. Development segment only (2022-03-01 → 2025-01-01).

**What it does not do:**
- no backtest, no trial registry row, no outcome field, no holdout read;
- no network, no API key, no order;
- no write to the lake. It writes only `<data>\feasibility\` and the evidence folder below.

This file holds commands only; it contains no result. The commands are checked against the CLI
parser and rehearsed on a synthetic lake by `tests/unit/backtest/test_owner_feasibility_run.py`.

**Shell:** every block is **NORMAL PowerShell** (Windows PowerShell 5.1 or PowerShell 7), with no
Administrator rights. Comma lists are quoted so PowerShell passes them as one argument.

**What is counted:** the probe grid has two configurations, the H1 draft defaults on 15m and on
1h. The defaults are the most permissive entry setting of the H1-v3 grid (`rank_max` 0.35,
`vol_z_min` 1, `stop_atr` 1.5), and `tp_r` enters no entry stage. So each count is an upper bound
on the candidates of every H1-v3 configuration at that interval.

## 1. Update the validation worktree (once)

```powershell
cd C:\Users\felip\Projects\tsc-mvp
git status --short
if (Test-Path frontend\pnpm-workspace.yaml) { Move-Item frontend\pnpm-workspace.yaml "$env:TEMP\tsc-mvp-pnpm-workspace.local.yaml" -Force }
git fetch origin task/mvp-integration
git checkout --detach origin/task/mvp-integration
git log -1 --oneline
```

- `git status --short` should list only `?? frontend/pnpm-lock.yaml` and
  `?? frontend/pnpm-workspace.yaml` (from your pnpm 12 install). If it lists anything else, stop
  and send the output.
- The branch now tracks `frontend/pnpm-workspace.yaml`, which approves esbuild for every pnpm
  version, and git will not overwrite an untracked file with the same name. Your copy is kept in
  `%TEMP%`.
- `frontend/pnpm-lock.yaml` stays untracked and yours (D-G).

Optional: rebuild the dashboard to confirm pnpm 12 now needs no manual approval:
```powershell
pnpm --dir frontend install
pnpm --dir frontend build
$LASTEXITCODE
```
Expected: no `approve-builds` prompt or `ERR_PNPM_IGNORED_BUILDS`, and `0`.

## 2. Evidence folder and code identity

```powershell
$env:TSC_DATA_DIR = 'C:\Users\felip\Projects\trading-system-claude\data'
$ev = Join-Path $env:USERPROFILE ('tsc_evidence\h1-feasibility-' + (Get-Date -Format 'yyyyMMdd-HHmm'))
New-Item -ItemType Directory -Force $ev | Out-Null
git rev-parse HEAD | Out-File -Encoding ascii "$ev\code_commit.txt"
uv run tsclaude version | Out-File -Encoding ascii "$ev\tsclaude_version.txt"
```

The app may stay open. The count only reads the lake and opens `state.db` to read holdout
unlocks; it appends no event.

## 3. Count both intervals (one run)

```powershell
uv run tsclaude backtest feasibility --draft --strategy h1_compression_breakout --grid docs\research\grids\feasibility_h1_draft_probe.json --symbols 'spot:BTCUSDT,spot:ETHUSDT,spot:DOGEUSDT,spot:XRPUSDT' --from 2022-03-01 --to 2025-01-01 | Out-File -Encoding utf8 "$ev\feasibility_summary.json"
$summary = Get-Content "$ev\feasibility_summary.json" -Raw | ConvertFrom-Json
$summary.ok
foreach ($r in $summary.results) { $interval = (Get-Content $r.path -Raw | ConvertFrom-Json).strategy.interval; Copy-Item $r.path "$ev\feasibility_$interval.json" }
Get-ChildItem $ev | Select-Object Name, Length
```

Expected: `True`, then `feasibility_15m.json` and `feasibility_1h.json` in the folder. If it
prints `False`, send `feasibility_summary.json`, which holds the reason. The run time on the full
lake has not been measured: it reads 34 months of 1m bars for 4 symbols, once per interval.

## 4. Dataset identity

```powershell
$ids = @()
foreach ($f in "$ev\feasibility_15m.json", "$ev\feasibility_1h.json") { $ids += (Get-Content $f -Raw | ConvertFrom-Json).dataset_ids }
$ids = @($ids | Sort-Object -Unique)
$ids | Out-File -Encoding ascii "$ev\dataset_ids.txt"
$n = 0
foreach ($id in $ids) { $n++; uv run tsclaude data verify $id --summary "$ev\data_verify_$n.json" | Out-Null; "$id exit=$LASTEXITCODE" }
```

Expected: every line ends in `exit=0` (files and hashes match the manifests; read-only). `exit=2`
means a file or hash does not match: stop and send the folder. These ids become the
preregistration's dataset pins (H1-11).

## 5. Power plans for both take-profit sets

```powershell
foreach ($tp in '2,2.5,3', '2.5,3,3.5') {
  $tag = $tp.Replace(',', '_')
  uv run tsclaude backtest power-plan --feasibility "$ev\feasibility_15m.json" --anchor 2022-02-01 --tp-r $tp --out "$ev\power_15m_tp$tag.json" | Out-Null
  "15m $tp exit=$LASTEXITCODE"
  uv run tsclaude backtest power-plan --feasibility "$ev\feasibility_1h.json" --anchor 2022-02-01 --embargo-minutes 4320 --tp-r $tp --out "$ev\power_1h_tp$tag.json" | Out-Null
  "1h $tp exit=$LASTEXITCODE"
}
```

Expected: four lines ending in `exit=0`. The embargo is the draft's:
- 15m uses 1,440 minutes (at least 735 needed);
- 1h uses 4,320 minutes (at least 2,940 needed).

## 6. Hashes, clean-up, hand-over

```powershell
Get-FileHash -Algorithm SHA256 -Path "$ev\*.json", "$ev\*.txt" | ForEach-Object { '{0}  {1}' -f $_.Hash.ToLower(), (Split-Path $_.Path -Leaf) } | Out-File -Encoding ascii "$ev\SHA256SUMS.txt"
Remove-Item Env:\TSC_DATA_DIR
Compress-Archive -Path "$ev\*" -DestinationPath "$ev.zip"
"$ev.zip"
```

Send the zip. It holds:
- counts and diagnostics;
- dataset ids and their verification (hashes and row counts);
- the four power plans;
- the code commit.

It holds no P&L, no trade and no price after a decision bar.

## What happens next

1. I read the counts and report them as measured, including if they are too low.
2. The interval is chosen from the counts only, and the take-profit set by your decision (decision
   record).
3. The preregistration is prepared from those choices; you freeze it.

Nothing in this run consumes a trial. Running it again is harmless: the output folders are named
by content hash, so an identical run reproduces the same `feasibility.json` files.
