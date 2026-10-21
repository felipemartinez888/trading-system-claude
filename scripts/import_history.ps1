# Trading System Claude - historical data import (owner machine; needs internet access to
# data.binance.vision, api.binance.com and fapi.binance.com).
# Usage: powershell -ExecutionPolicy Bypass -File scripts\import_history.ps1 [-From 2022-01-01] [-SkipUniverse]
#
# 1. computes the research universe frozen at 2025-01-01 (decision L-08;
#    configs\universe_snapshot_2025-01-01.yaml) and the live/forward universe for today
#    (configs\universe_live_<date>.yaml, never used for research);
# 2. imports spot and USD-M history from -From to the last complete month for the default universe
#    (configs\universe.yaml) and for the research universe's symbols;
# 3. verifies every dataset in the lake;
# 4. writes all JSON summaries to evidence\data\<timestamp>\ (commit that folder as evidence).
# Re-running is safe: downloads are cached and checksum-verified, imports are idempotent.

param(
    [string]$From = '2022-01-01',
    [switch]$SkipUniverse
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Join-Path $PSScriptRoot '..')

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host 'uv is not installed. See scripts\README.md.' -ForegroundColor Yellow
    exit 1
}

$nowUtc = (Get-Date).ToUniversalTime()
$today = $nowUtc.ToString('yyyy-MM-dd')
$lastComplete = (Get-Date -Year $nowUtc.Year -Month $nowUtc.Month -Day 1).AddDays(-1).ToString('yyyy-MM-dd')
$stamp = $nowUtc.ToString('yyyyMMddTHHmmssZ')
$out = Join-Path 'evidence\data' $stamp
New-Item -ItemType Directory -Force -Path $out | Out-Null
$results = [ordered]@{ started_utc = $nowUtc.ToString('o'); from = $From; to = $lastComplete; steps = @() }

function Invoke-Step([string]$Name, [string[]]$CliArgs) {
    Write-Host "==> $Name" -ForegroundColor Cyan
    & uv run tsclaude @CliArgs | Out-Host
    $code = $LASTEXITCODE
    $script:results.steps += [ordered]@{ step = $Name; exit_code = $code }
    if ($code -ne 0) { Write-Host "    step '$Name' exited with $code" -ForegroundColor Red }
    return $code
}

$sources = @('configs\universe.yaml')
if (-not $SkipUniverse) {
    $code = Invoke-Step 'universe-research' @('data', 'universe', '--summary', "$out\universe-research.json")
    $snap = 'configs\universe_snapshot_2025-01-01.yaml'
    if ($code -eq 0 -and (Test-Path -LiteralPath $snap)) { $sources += $snap }
    Invoke-Step 'universe-live' @('data', 'universe', '--live', '--snapshot', $today, '--summary', "$out\universe-live.json") | Out-Null
}

foreach ($src in $sources) {
    $name = [IO.Path]::GetFileNameWithoutExtension($src)
    foreach ($market in @('spot', 'usdm')) {
        Invoke-Step "import-$market-$name" @('data', 'import', '--market', $market, '--symbols-from', $src,
            '--from', $From, '--to', $lastComplete, '--summary', "$out\import-$market-$name.json") | Out-Null
    }
}

Invoke-Step 'verify' @('data', 'verify', '--all', '--summary', "$out\verify.json") | Out-Null

$results.finished_utc = (Get-Date).ToUniversalTime().ToString('o')
$failed = @($results.steps | Where-Object { $_.exit_code -ne 0 }).Count
$results.ok = ($failed -eq 0)
$results | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 -Path "$out\summary.json"
Write-Host ''
Write-Host "Summary written to $out\summary.json (failed steps: $failed)"
if ($failed -ne 0) { exit 1 }
