# M1 gate evidence: run the app with monitoring auto-started, then write a JSON report.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\verify_live.ps1 [-Hours 2] [-NoBrowser]
# The report is written to evidence\live\verify-<UTC>.json (see scripts\README.md).
param(
    [double]$Hours = 2,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Join-Path $PSScriptRoot '..')

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host ' uv is not installed; see scripts\README.md (first-time setup).' -ForegroundColor Yellow
    exit 1
}

$frozen = @()
if (Test-Path -LiteralPath 'uv.lock') { $frozen = @('--frozen') }
$extra = @('--hours', [string]$Hours)
if ($NoBrowser) { $extra += '--no-browser' }

& uv run @frozen python scripts/verify_live.py @extra
exit $LASTEXITCODE
