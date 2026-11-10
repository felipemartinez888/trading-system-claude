# TSC dashboard smoke check (owner-run, NORMAL PowerShell: no Administrator rights needed).
#
# Paper only, public market data only: no API keys, nothing is ever sent to an exchange, and the
# app is NOT started monitoring (no Binance traffic unless you press Start yourself).
# Uses a FRESH, EMPTY data folder under <Runs>\smoke-<stamp>\data, so your real data, runs and
# trial registry are never touched.
#
# What it does, from the checkout it is run in:
#   1. rebuilds the dashboard bundle from this checkout (an old bundle would hide the new panels);
#   2. starts the app on http://127.0.0.1:8765/ in the background, waits for /api/health;
#   3. saves /api/research/status and /api/paper/status to the smoke folder and prints them;
#   4. opens the dashboard so you can look; you then press Quit in the dashboard (a clean stop).
# Output: <Runs>\smoke-<stamp>\summary.json. A PASS is evidence that the branch dashboard renders,
# nothing more.
#
# Usage, from the branch checkout:
#   powershell -ExecutionPolicy Bypass -File scripts\owner_checks\dashboard_smoke.ps1
param(
    [string]$Runs = 'C:\Users\felip\tsc_runs',
    [int]$Port = 8765,
    [switch]$SkipBuild
)
$ErrorActionPreference = 'Stop'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$out = Join-Path $Runs "smoke-$stamp"
$data = Join-Path $out 'data'
New-Item -ItemType Directory -Path $data -Force | Out-Null
$summary = [ordered]@{
    started_utc = (Get-Date).ToUniversalTime().ToString('o')
    checkout = (Get-Location).Path
    git_head = (& git rev-parse HEAD 2>$null)
    git_dirty = [bool](& git status --porcelain --untracked-files=no 2>$null)
    checks = [ordered]@{}
}
function Save-Summary {
    $summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $out 'summary.json') -Encoding utf8
}
function Check([string]$name, [bool]$ok, [string]$note) {
    $summary.checks[$name] = [ordered]@{ pass = $ok; note = $note }
    Save-Summary
    $color = if ($ok) { 'Green' } else { 'Red' }
    Write-Host ("[{0}] {1}: {2}" -f ($(if ($ok) { 'PASS' } else { 'FAIL' })), $name, $note) -ForegroundColor $color
}

if (-not (Test-Path -LiteralPath 'src\tsclaude\api\research.py')) {
    Write-Host 'Run this from the task/mvp-integration checkout (src\tsclaude\api\research.py not found).' -ForegroundColor Red
    exit 2
}
$uvRun = @('run')
if (Test-Path -LiteralPath 'uv.lock') { $uvRun += '--frozen' }

if (-not $SkipBuild) {
    Write-Host 'Building the dashboard bundle from this checkout...'
    $env:COREPACK_ENABLE_DOWNLOAD_PROMPT = '0'
    & pnpm --dir frontend install
    if ($LASTEXITCODE -eq 0) { & pnpm --dir frontend build }
    Check 'bundle_built' ($LASTEXITCODE -eq 0) "pnpm build exit $LASTEXITCODE"
    if ($LASTEXITCODE -ne 0) { exit 3 }
}

$env:TSC_DATA_DIR = $data
$env:PYTHONFAULTHANDLER = '1'
$log = Join-Path $out 'app.log'
$err = Join-Path $out 'app.err.log'
$proc = Start-Process -FilePath 'uv' -ArgumentList ($uvRun + @('tsclaude', 'app', '--no-browser', '--port', "$Port")) `
    -RedirectStandardOutput $log -RedirectStandardError $err -PassThru -WindowStyle Hidden
$base = "http://127.0.0.1:$Port"
$up = $false
for ($i = 0; $i -lt 90 -and -not $proc.HasExited; $i++) {
    try { Invoke-RestMethod "$base/api/health" -TimeoutSec 2 | Out-Null; $up = $true; break } catch { Start-Sleep -Seconds 1 }
}
Check 'app_started' $up $(if ($up) { "listening on $base" } else { "no answer from $base/api/health; see $err" })
if (-not $up) { if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }; exit 4 }

$research = Invoke-RestMethod "$base/api/research/status"
$research | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $out 'research_status.json') -Encoding utf8
$state = $research.status.value.state
Check 'research_status_api' ($state -eq 'NO_VALIDATED_STRATEGY') "state = $state, provenance = $($research.status.provenance)"

$paper = Invoke-RestMethod "$base/api/paper/status"
$paper | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $out 'paper_status.json') -Encoding utf8
$pv = $paper.status.value
$hasHintField = $null -ne $pv -and ($pv.PSObject.Properties.Name -contains 'hint')
Check 'paper_status_api' ($null -ne $paper.status) ("paper state = {0}; hint field present = {1}; hint = {2}" -f $pv.state, $hasHintField, $pv.hint)

$page = Invoke-WebRequest "$base/" -UseBasicParsing
Check 'dashboard_served' ($page.StatusCode -eq 200 -and $page.Content -match '<script') "GET / status $($page.StatusCode)"

Write-Host ''
Write-Host 'Look at the dashboard now (it opens in your browser):' -ForegroundColor Cyan
Write-Host '  1. Backtests page: a "No validated strategy" research panel.' -ForegroundColor Cyan
Write-Host '  2. Paper page: its status card. A "What to do" hint appears only when paper is blocked' -ForegroundColor Cyan
Write-Host '     or waiting (for example a PC clock more than 1 s off); paper is off in this smoke run.' -ForegroundColor Cyan
Write-Host '  3. Then press Quit in the dashboard. This window waits up to 10 minutes.' -ForegroundColor Cyan
Start-Process $base
$proc.WaitForExit(600000) | Out-Null
if (-not $proc.HasExited) {
    Stop-Process -Id $proc.Id -Force
    Check 'clean_quit' $false 'not quit within 10 minutes; the process was stopped'
} else {
    Check 'clean_quit' $true "app exited with code $($proc.ExitCode)"
}
$answer = Read-Host 'Did the research panel and the Paper page render? (y/n)'
Check 'owner_visual_check' ($answer -match '^[yY]') "owner answered '$answer'"
$summary.finished_utc = (Get-Date).ToUniversalTime().ToString('o')
Save-Summary
Write-Host "Summary: $(Join-Path $out 'summary.json')"
