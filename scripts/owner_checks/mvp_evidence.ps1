# TSC MVP evidence run (owner-run, NORMAL PowerShell: no Administrator rights needed).
#
# Paper only, public Binance market data only: no API keys, nothing is ever sent to an exchange.
# Runs, one after the other, and stops at the first refused precondition:
#   0. environment fingerprint (git head/status, uv version);
#   1. clock pre-check with w32tm (the paper runtime refuses to observe beyond 1 s; 0.5 s required);
#   2. crash-recovery run: a COPY of a session that ended with a PC restart is started again
#      (the original folder is never modified), then verify-replay and verify-chain;
#   3. fresh supervised paper session with the REFERENCE strategy (Paper O-1, RUNBOOK_INGESTION
#      stage C), then verify-replay and verify-chain;
#   4. reference backtest mechanics on the canonical lake (read-only), run twice to check that the
#      metrics hash is reproducible. This writes runs/ and trial records into the canonical data
#      directory, as every backtest does (each evaluation is a recorded trial).
# Every output lands in <Runs>\mvp-evidence-<stamp>\ with summary.json. A PASS of any step is
# evidence for that step only: not profitability, not M3 acceptance, not MVP acceptance.
#
# Usage (from any folder): run the TRACKED script of the checkout under test. It works on that
# checkout (the folder two levels above this script), so a copy elsewhere or an older checkout is
# never used by accident (operational-readiness review R-03). -Repo still overrides it explicitly.
#   powershell -ExecutionPolicy Bypass -File C:\Users\felip\Projects\<checkout>\scripts\owner_checks\mvp_evidence.ps1
#   options: -PaperHours 1 -RecoveryMinutes 15 -SkipRecovery -SkipPaper -SkipBacktest
#   -SkipBacktest skips step 4, the only step that writes permanent trial records.
param(
    [double]$PaperHours = 1,
    [int]$RecoveryMinutes = 15,
    [switch]$SkipRecovery,
    [switch]$SkipPaper,
    [switch]$SkipBacktest,
    [string]$Repo = '',  # default: the checkout this script belongs to (see below)
    [string]$Runs = 'C:\Users\felip\tsc_runs',
    [string]$CrashedSession = 'paper-20261109-125923',
    [string]$CanonicalData = 'C:\Users\felip\Projects\trading-system-claude\data'
)
$ErrorActionPreference = 'Stop'
# The repository is the checkout this script belongs to (scripts\owner_checks\ -> its root),
# unless -Repo names one. A copy outside a checkout is refused before anything is written.
if (-not $Repo) {
    $Repo = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
}
$ownScript = Join-Path $Repo 'scripts\owner_checks\mvp_evidence.ps1'
if (-not (Test-Path -LiteralPath (Join-Path $Repo 'pyproject.toml')) -or -not (Test-Path -LiteralPath $ownScript)) {
    throw ("mvp_evidence.ps1: '$Repo' is not a Trading System Claude checkout. Run the tracked " +
        "script from the checkout under test: powershell -ExecutionPolicy Bypass -File " +
        "<checkout>\scripts\owner_checks\mvp_evidence.ps1 (or pass -Repo <checkout>).")
}
$stamp = Get-Date -Format 'yyyyMMdd-HHmm'
$out = Join-Path $Runs "mvp-evidence-$stamp"
New-Item -ItemType Directory -Path $out | Out-Null
$summary = [ordered]@{
    started_utc = (Get-Date).ToUniversalTime().ToString('o')
    repo = $Repo
    steps = [ordered]@{}
}
function Save-Summary {
    $summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $out 'summary.json') -Encoding utf8
}
function Set-Step([string]$name, [int]$code, [string]$note) {
    $summary.steps[$name] = [ordered]@{
        exit = $code
        note = $note
        at_utc = (Get-Date).ToUniversalTime().ToString('o')
    }
    Save-Summary
    Write-Host ("[{0}] exit {1}: {2}" -f $name, $code, $note)
}

Set-Location -LiteralPath $Repo
# print the Python stack of every thread if a child process crashes natively (access violation)
$env:PYTHONFAULTHANDLER = '1'
$uvRun = @('run')
if (Test-Path -LiteralPath 'uv.lock') { $uvRun += '--frozen' }

# Native commands write progress to stderr; Start-Process keeps stdout/stderr in files and gives
# the exit code without PowerShell turning stderr lines into errors.
function Invoke-Uv([string]$name, [string[]]$uvArgs) {
    $o = Join-Path $out "$name.out.txt"
    $e = Join-Path $out "$name.err.txt"
    $p = Start-Process -FilePath 'uv' -ArgumentList ($uvRun + $uvArgs) -RedirectStandardOutput $o `
        -RedirectStandardError $e -NoNewWindow -Wait -PassThru
    return $p.ExitCode
}

# ------------------------------------------------------------------ 0. environment
(& git rev-parse HEAD) | Out-File -LiteralPath (Join-Path $out 'git_head.txt') -Encoding utf8
(& git status --short) | Out-File -LiteralPath (Join-Path $out 'git_status.txt') -Encoding utf8
(& uv --version) | Out-File -LiteralPath (Join-Path $out 'uv_version.txt') -Encoding utf8
Set-Step 'environment' 0 'git head, git status and uv version recorded'

# ------------------------------------------------------------------ 1. clock
function Get-ClockOffset([string]$label) {
    $lines = & w32tm /stripchart /computer:time.windows.com /dataonly /samples:5
    $lines | Out-File -LiteralPath (Join-Path $out "clock_$label.txt") -Encoding utf8
    $worst = $null
    foreach ($l in $lines) {
        if ($l -match ',\s*([+-]?\d+\.\d+)s') {
            $v = [math]::Abs([double]$Matches[1])
            if ($null -eq $worst -or $v -gt $worst) { $worst = $v }
        }
    }
    return $worst
}
$worst = Get-ClockOffset 'before'
if ($null -eq $worst -or $worst -gt 0.5) {
    Set-Step 'clock_before' 1 "worst offset: $worst s (need <= 0.5 s). Settings > Time & language > Date & time > Sync now, then run again."
    exit 1
}
Set-Step 'clock_before' 0 "worst offset $worst s"

function Invoke-Live([string]$name, [string]$dataDir, [string]$configDir, [double]$hours) {
    $env:TSC_DATA_DIR = $dataDir
    $env:TSC_CONFIG_DIR = $configDir
    Remove-Item Env:TSC_INGEST_PROFILE -ErrorAction SilentlyContinue
    $live = Join-Path $Repo 'evidence\live'
    $before = @()
    if (Test-Path -LiteralPath $live) { $before = @(Get-ChildItem -LiteralPath $live -Filter 'verify-*.json' | ForEach-Object { $_.Name }) }
    $code = Invoke-Uv "$name.live" @('python', 'scripts/verify_live.py', '--hours', [string]$hours, '--no-browser')
    if (Test-Path -LiteralPath $live) {
        $new = Get-ChildItem -LiteralPath $live -Filter 'verify-*.json' | Where-Object { $before -notcontains $_.Name } |
            Sort-Object LastWriteTime | Select-Object -Last 1
        if ($new) { Copy-Item -LiteralPath $new.FullName -Destination (Join-Path $out "$name.verify_live.json") }
    }
    $rcode = Invoke-Uv "$name.verify_replay" @('tsclaude', 'verify-replay', '--data-dir', $dataDir, '--config-dir', $configDir)
    $ccode = Invoke-Uv "$name.verify_chain" @('tsclaude', 'verify-chain')
    # the step's exit is the FIRST non-zero code (live, then replay, then chain), so a native crash
    # (negative code) or a replay FAIL is never hidden behind another step's result
    $first = 0
    foreach ($c in @($code, $rcode, $ccode)) { if ($first -eq 0 -and $c -ne 0) { $first = $c } }
    Set-Step $name $first ("verify_live exit $code (0 pass, 1 monitoring did not run, 2 gate criteria not met; " +
        "-1073741819 = native access violation); verify-replay exit $rcode (0 PASS, 1 FAIL, 2 INCONCLUSIVE); " +
        "verify-chain exit $ccode (0 ok)")
}

# ------------------------------------------------------------------ 2. crash recovery (a copy)
if (-not $SkipRecovery) {
    $src = Join-Path $Runs $CrashedSession
    if (Test-Path -LiteralPath $src) {
        $copy = Join-Path $Runs "recovery-$stamp"
        Copy-Item -LiteralPath $src -Destination $copy -Recurse  # the original stays untouched
        Invoke-Live 'recovery' (Join-Path $copy 'data') (Join-Path $copy 'configs') ($RecoveryMinutes / 60.0)
    } else {
        Set-Step 'recovery' 1 "crashed session folder not found: $src"
    }
}

# ------------------------------------------------------------------ 3. fresh paper session
if (-not $SkipPaper) {
    $worst = Get-ClockOffset 'before_paper'
    if ($null -eq $worst -or $worst -gt 0.5) {
        Set-Step 'paper' 1 "skipped: clock offset $worst s (need <= 0.5 s)"
    } else {
        $run = Join-Path $Runs "paper-$stamp"
        New-Item -ItemType Directory -Path $run | Out-Null
        Copy-Item -LiteralPath (Join-Path $Repo 'configs') -Destination (Join-Path $run 'configs') -Recurse
        $py = Join-Path $run 'configs\paper.yaml'
        (Get-Content -LiteralPath $py) -replace 'enabled: false', 'enabled: true' -replace 'strategy: none', 'strategy: bracket_reference' |
            Set-Content -LiteralPath $py -Encoding utf8
        Invoke-Live 'paper' (Join-Path $run 'data') (Join-Path $run 'configs') $PaperHours
    }
}

# ------------------------------------------------------------------ 4. reference backtest mechanics
if (-not $SkipBacktest) {
    $env:TSC_DATA_DIR = $CanonicalData
    Remove-Item Env:TSC_CONFIG_DIR -ErrorAction SilentlyContinue
    $bt = @('tsclaude', 'backtest', 'run', '--strategy', 'bracket_reference', '--symbols', 'spot:BTCUSDT',
        '--from', '2024-01-01', '--to', '2024-04-01', '--hypothesis', 'MVP-MECHANICS-REFERENCE')
    $c1 = Invoke-Uv 'backtest_run1' $bt
    $c2 = Invoke-Uv 'backtest_run2' $bt
    $null = Invoke-Uv 'backtest_trials' @('tsclaude', 'backtest', 'trials', '--strategy', 'bracket_reference')
    $same = 'unknown'
    try {
        $r1 = Get-Content -LiteralPath (Join-Path $out 'backtest_run1.out.txt') -Raw | ConvertFrom-Json
        $r2 = Get-Content -LiteralPath (Join-Path $out 'backtest_run2.out.txt') -Raw | ConvertFrom-Json
        $same = [string]($r1.metrics_sha256 -eq $r2.metrics_sha256)
        if ($r1.run_id) { $null = Invoke-Uv 'backtest_show1' @('tsclaude', 'backtest', 'show', $r1.run_id) }
    } catch { $same = "not comparable: $($_.Exception.Message)" }
    Set-Step 'backtest' ([int]($c1 -ne 0 -or $c2 -ne 0)) "run exits $c1 / $c2; identical metrics hash: $same (REFERENCE strategy: mechanics only, not research evidence)"
}

$summary.finished_utc = (Get-Date).ToUniversalTime().ToString('o')
Save-Summary
Write-Host "Done. Evidence: $out"
