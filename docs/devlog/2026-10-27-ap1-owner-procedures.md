# AP-1 — owner-run procedures for plan steps E/F (written, NOT run by Claude)

Status: reference only. Running these needs the owner's AP-2 approval. They are not part of any
milestone gate by themselves.

All commands: **NORMAL TERMINAL** (Windows PowerShell 5.1, no administrator rights), run from the
repository root, on AC power, with browser/IDE closed. They create only the new `data_diag_<tag>\` and
`evidence\diagnostics\<tag>\` folders. They never write `data\` and never delete anything. The only
process they stop is the import process they started themselves (the resource stop rule).

## 0. Preconditions and resource assumptions (checked, not assumed)
- `.venv\Scripts\tsclaude.exe` exists. Nothing here runs `uv run` / `uv sync`, so the environment is not
  resynchronised.
- **Memory.** The stop rule kills the import when its private memory exceeds `$maxPrivateMb` (1,500 MB)
  or system free commit (RAM + page file) drops below `$minFreeCommitMb` (3,000 MB). The block refuses
  to start when free commit is already below `$minFreeCommitMb + 1000`, so a machine that is short of
  memory before the run is reported as such, not as a run result. These are owner-tunable thresholds,
  not measured properties of the importer. (At review time, 2026-10-27, this machine showed ~7.9 GB free
  commit and ~2.7 GB free physical memory.)
- **Disk.** Per run: about 120 MB to copy the BTC spot cache, plus up to about 150 MB of Parquet for a
  48-month run.
- **Network.** The 2022-01…2026-09 BTCUSDT spot 1m monthly zips are in `data_diag_spot_full_btc\archive_cache`
  (57 zips + checksums) and are copied, never modified. Any other symbol or market, and the 15m/1h zips
  of the two cross-check months, are downloaded from the public Binance archive (no API keys, no signed
  endpoints).

## 1. Measured import with outcome classification
Each run gets exactly one of four outcomes; the exit code alone never decides it:
- `NORMAL_COMPLETION`: exit 0, not stopped, sampled without error.
- `RESOURCE_LIMIT`: the sampler stopped the process because a limit was crossed. This is not a crash.
- `PROCESS_CRASH`: non-zero exit and the sampler did not stop it. The exit code is recorded in hex, so a
  native abort such as `0xC000001D`, `0xC0000409` or `0xC0000005` is visible.
- `MONITORING_FAILURE`: exit 0, but sampling failed or produced no sample, so the memory figures are
  unproven.

A sampling error (CIM query, `Refresh`) is counted and the loop keeps going until the process really
exits.

**Limits of the method.** Samples are taken every `$intervalS` seconds (default 5). A peak shorter than
that, or one in the last interval before exit, can be missed. The OS high-water marks
(`PeakWorkingSet64`, `PeakPagedMemorySize64`) can only be read while the process is alive, so they also
stop at the last successful sample. The stop rule can therefore fire late. `NORMAL_COMPLETION` means "no
sampled value above the limit", not "memory never exceeded it". Set `$intervalS = 1` for a tighter bound.

```powershell
# NORMAL TERMINAL, repository root
# ---- edit these lines only ----
$tag = 'm2_e_1m'; $from = '2024-01-01'; $to = '2024-01-31'
$market = 'spot'; $symbol = 'BTCUSDT'
$maxPrivateMb = 1500; $minFreeCommitMb = 3000; $intervalS = 5
# --------------------------------
$exe = '.\.venv\Scripts\tsclaude.exe'
if (-not (Test-Path $exe)) { throw "$exe not found" }
$dir = "data_diag_$tag"; $out = "evidence\diagnostics\$tag"
if ((Test-Path $dir) -or (Test-Path $out)) { throw "$dir or $out exists - choose a new tag" }
$freeStart = [int]((Get-CimInstance Win32_OperatingSystem).FreeVirtualMemory / 1KB)
if ($freeStart -lt $minFreeCommitMb + 1000) { throw "free commit $freeStart MB is below $($minFreeCommitMb + 1000) MB - free memory first" }
New-Item -ItemType Directory $dir, $out | Out-Null
if ($market -eq 'spot' -and $symbol -eq 'BTCUSDT') {
  Copy-Item data_diag_spot_full_btc\archive_cache "$dir\archive_cache" -Recurse   # source is only read
}
$commit = (git rev-parse HEAD); $branch = (git branch --show-current)
$env:PYTHONFAULTHANDLER = '1'; $env:RUST_BACKTRACE = 'full'   # this PowerShell window only
$p = Start-Process $exe -NoNewWindow -PassThru `
  -ArgumentList 'data','import','--data-dir',$dir,'--market',$market,'--symbols',$symbol,'--from',$from,'--to',$to,'--summary',"$out\summary.json" `
  -RedirectStandardOutput "$out\stdout.log" -RedirectStandardError "$out\stderr.log"
$null = $p.Handle                       # keeps the exit code readable after exit (Windows PowerShell 5.1)
$rows = New-Object System.Collections.Generic.List[object]
$stopReason = $null; $monErrors = 0; $osPeakWs = 0; $osPeakPaged = 0
while (-not $p.HasExited) {
  try {
    $p.Refresh()
    $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
    $r = [pscustomobject]@{
      t = (Get-Date).ToString('o')
      private_mb = [int]($p.PrivateMemorySize64 / 1MB)
      ws_mb = [int]($p.WorkingSet64 / 1MB)
      sys_free_commit_mb = [int]($os.FreeVirtualMemory / 1KB)
    }
    $osPeakWs = [int]($p.PeakWorkingSet64 / 1MB); $osPeakPaged = [int]($p.PeakPagedMemorySize64 / 1MB)
    $rows.Add($r)
    if ($null -eq $stopReason -and $r.private_mb -gt $maxPrivateMb) { $stopReason = "private_mb $($r.private_mb) > $maxPrivateMb" }
    if ($null -eq $stopReason -and $r.sys_free_commit_mb -lt $minFreeCommitMb) { $stopReason = "sys_free_commit_mb $($r.sys_free_commit_mb) < $minFreeCommitMb" }
    if ($stopReason) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }   # the import started above, nothing else
  } catch { $monErrors++ }               # a sampling error is a monitoring problem, not an exit
  Start-Sleep -Seconds $intervalS
}
$p.WaitForExit(); $code = $p.ExitCode
$hex = '0x{0:X8}' -f [BitConverter]::ToUInt32([BitConverter]::GetBytes([int]$code), 0)
$outcome = if ($stopReason) { 'RESOURCE_LIMIT' }
           elseif ($code -ne 0) { 'PROCESS_CRASH' }
           elseif ($monErrors -gt 0 -or $rows.Count -eq 0) { 'MONITORING_FAILURE' }
           else { 'NORMAL_COMPLETION' }
$rows | Export-Csv "$out\memory.csv" -NoTypeInformation
$peakPriv = ($rows | Measure-Object private_mb -Maximum).Maximum
$minFree  = ($rows | Measure-Object sys_free_commit_mb -Minimum).Minimum
$result = [ordered]@{
  tag = $tag; git_commit = $commit; git_branch = $branch
  request = @{ market = $market; symbol = $symbol; from = $from; to = $to }
  outcome = $outcome; exit_code = $code; exit_code_hex = $hex
  stopped_by_sampler = [bool]$stopReason; stop_reason = $stopReason
  samples = $rows.Count; sample_interval_s = $intervalS; monitoring_errors = $monErrors
  free_commit_mb_at_start = $freeStart
  peak_private_mb_sampled = $peakPriv; min_free_commit_mb_sampled = $minFree
  os_peak_working_set_mb_last_seen = $osPeakWs; os_peak_paged_mb_last_seen = $osPeakPaged
  limits = @{ max_private_mb = $maxPrivateMb; min_free_commit_mb = $minFreeCommitMb }
  note = 'Peaks are sampled; a transient peak shorter than the interval, or in the last interval before exit, can be missed.'
  summary_json_present = (Test-Path "$out\summary.json")
}
$result | ConvertTo-Json -Depth 4 | Set-Content "$out\result.json" -Encoding utf8
"$outcome exit=$code ($hex) peak_private_mb=$peakPriv samples=$($rows.Count) monitoring_errors=$monErrors"
if ($outcome -ne 'NORMAL_COMPLETION') { '--- stderr (last 40 lines) ---'; Get-Content "$out\stderr.log" -Tail 40 }
```

Expected on success: `NORMAL_COMPLETION exit=0 (0x00000000) ...`. Return to Claude: `result.json`,
`summary.json`, `memory.csv` (small), and the stderr tail only when the outcome is not `NORMAL_COMPLETION`.

A `summary.json` with `ok: true` means the data was *imported*. *Verified* also needs `data verify` and the
parity check (section 2):
```powershell
# NORMAL TERMINAL, repository root; reads the new dir only; "ok" must be true
.\.venv\Scripts\tsclaude.exe data verify --all --data-dir data_diag_m2_e_1m --summary evidence\diagnostics\m2_e_1m\verify.json
```

## 2. Parity against the existing lake: matching periods only (2022-01 … 2025-12)
Why not compare `dataset_id`s directly with `data\`: the existing BTC dataset (request 2022-01-01 →
2026-10-01) also contains the nine sealed 2026 parts, so its id differs from any 2022–2025 run by
construction. The check therefore:
1. compares `content_hash`, `rows`, `first_time_ms` and `last_time_ms` of every part for the months both
   sides have;
2. recomputes the `dataset_id` the reference would have for exactly those months (same formula as
   `store/lake.py` `dataset_id_of`: SHA-256 of the sorted hashes joined by `\n`) and compares it with the
   new run's id;
3. lists the new run's QA summary.

`file_sha256` is deliberately **not** compared: the Parquet files embed `ingested_at_ms`, which
`content_hash` excludes by design. Holdout parts (2026) are never read: the script skips every manifest
entry with `year -gt 2025`, and it reads only manifest JSON, never a Parquet file.

```powershell
# NORMAL TERMINAL, repository root; read-only
$tag = 'm2_f48'; $symbol = 'BTCUSDT'; $market = 'spot'; $lastYear = 2025
$sel = { $_.kind -eq 'bars_1m' -and $_.symbol -eq $symbol -and $_.market -eq $market }
$new = @(Get-ChildItem "data_diag_$tag\imports\*\manifest.json" | ForEach-Object { Get-Content $_ -Raw | ConvertFrom-Json } | Where-Object $sel)
if ($new.Count -ne 1) { throw "expected exactly one bars_1m manifest in data_diag_$tag, got $($new.Count)" }
$ref = @(Get-ChildItem "data\imports\*\manifest.json" | ForEach-Object { Get-Content $_ -Raw | ConvertFrom-Json } | Where-Object $sel)
if ($ref.Count -ne 1) { throw "expected exactly one reference bars_1m manifest in data, got $($ref.Count)" }
$new = $new[0]; $ref = $ref[0]
function Key($p) { '{0:D4}-{1:D2}' -f $p.year, $p.month }
$newParts = @{}; foreach ($p in $new.parts) { if ($p.year -le $lastYear) { $newParts[(Key $p)] = $p } }
$refParts = @{}; foreach ($p in $ref.parts) { if ($p.year -le $lastYear) { $refParts[(Key $p)] = $p } }
$months = $newParts.Keys | Sort-Object
$bad = @()
foreach ($m in $months) {
  $a = $newParts[$m]; $b = $refParts[$m]
  if ($null -eq $b) { $bad += "$m missing in reference"; continue }
  foreach ($f in 'content_hash','rows','first_time_ms','last_time_ms') {
    if ($a.$f -ne $b.$f) { $bad += "$m $f new=$($a.$f) ref=$($b.$f)" }
  }
}
function DatasetId($hashes) {
  $s = ($hashes | Sort-Object) -join "`n"
  $h = [System.Security.Cryptography.SHA256]::Create().ComputeHash([System.Text.Encoding]::ASCII.GetBytes($s))
  -join ($h | ForEach-Object { $_.ToString('x2') })
}
$expectedId = DatasetId ($months | ForEach-Object { $refParts[$_].content_hash })
"parts compared      : $(@($months).Count)"
"part mismatches     : $(@($bad).Count)"
"new dataset_id      : $($new.dataset_id)"
"recomputed ref id   : $expectedId   (equal: $($new.dataset_id -eq $expectedId))"
"rows new / ref      : $((($months | % { $newParts[$_].rows }) | Measure-Object -Sum).Sum) / $((($months | % { $refParts[$_].rows }) | Measure-Object -Sum).Sum)"
$bad
$new.qa_summary | ConvertTo-Json
```
Expected:
- `part mismatches : 0`, `equal: True` and identical row sums. Any line printed under the table is a
  mismatch.
- In `qa_summary`: `missing_interior`, `gap_ranges`, `duplicate_conflicts`, `rejected_rows`,
  `malformed_rows` and `unavailable_files` are all `0`, and `cross_checks_ok` is `True`.
- `rows` equals the expected minute count: 44,640 (Jan 2024), 527,040 (2024), 1,051,200 (2022–23),
  2,103,840 (2022–25).

The comparison covers only the months the new run has. A new run with missing months relative to the
reference must be reported as such, not as parity.

**Whole-dataset parity (F-12 only).** The 2024 request equals the stored diagnostic run
`data_diag_spot_2024` (BTC dataset `e2d4a0dc6f5a…`, 527,040 rows), so the dataset id and QA summary can be
compared directly. Manifests also carry `created_at`, so the files themselves are never byte-equal
between runs.
```powershell
# NORMAL TERMINAL, repository root; read-only
$sel = { $_.kind -eq 'bars_1m' -and $_.symbol -eq 'BTCUSDT' -and $_.market -eq 'spot' }
$old = @(Get-ChildItem data_diag_spot_2024\imports\*\manifest.json | ForEach-Object { Get-Content $_ -Raw | ConvertFrom-Json } | Where-Object $sel)
$cur = @(Get-ChildItem data_diag_m2_f12\imports\*\manifest.json | ForEach-Object { Get-Content $_ -Raw | ConvertFrom-Json } | Where-Object $sel)
if ($old.Count -ne 1 -or $cur.Count -ne 1) { throw "expected one BTC spot bars_1m manifest on each side (old=$($old.Count) cur=$($cur.Count))" }
"dataset_id equal : $($old[0].dataset_id -eq $cur[0].dataset_id)"
"qa_summary equal : $((($old[0].qa_summary | ConvertTo-Json -Compress) -eq ($cur[0].qa_summary | ConvertTo-Json -Compress)))"
"row_count        : $($old[0].row_count) / $($cur[0].row_count)"
```
