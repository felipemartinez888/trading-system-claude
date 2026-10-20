# Trading System Claude - Windows launcher (PowerShell).
# Usage: powershell -ExecutionPolicy Bypass -File scripts\TradingSystemClaude.ps1 [--no-browser] [--port N]
# Same behaviour as TradingSystemClaude.cmd: check uv, install dependencies on first run, run the app.
# `uv run` keeps the environment in sync with uv.lock on later runs (no-op when unchanged).

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Join-Path $PSScriptRoot '..')

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host ''
    Write-Host ' uv is not installed. Install it once with:' -ForegroundColor Yellow
    Write-Host ''
    Write-Host '   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
    Write-Host ''
    Write-Host ' Then open a new terminal and run this script again.'
    Write-Host ' More: https://docs.astral.sh/uv/getting-started/installation/'
    exit 1
}

$frozen = @()
if (Test-Path -LiteralPath 'uv.lock') { $frozen = @('--frozen') }

if (-not (Test-Path -LiteralPath '.venv')) {
    Write-Host 'First run: installing dependencies, this can take a minute...'
    & uv sync @frozen
    if ($LASTEXITCODE -ne 0) {
        Write-Host ' Dependency installation failed. See the messages above.' -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

if (-not (Test-Path -LiteralPath 'src\tsclaude\web\static\index.html')) {
    # The dashboard bundle is not committed; build it once if Node.js is available.
    $pnpm = $null
    if (Get-Command pnpm -ErrorAction SilentlyContinue) { $pnpm = @('pnpm') }
    elseif (Get-Command corepack -ErrorAction SilentlyContinue) { $pnpm = @('corepack', 'pnpm') }
    if ($null -eq $pnpm) {
        Write-Host ''
        Write-Host ' Note: the dashboard bundle is not built and Node.js was not found. The app will' -ForegroundColor Yellow
        Write-Host ' start and show a page explaining this. Install Node.js LTS (scripts\README.md)' -ForegroundColor Yellow
        Write-Host ' and run this launcher again to build it.' -ForegroundColor Yellow
        Write-Host ''
    } else {
        Write-Host 'Building the dashboard bundle once (needs internet the first time)...'
        $env:COREPACK_ENABLE_DOWNLOAD_PROMPT = '0'
        $exe = $pnpm[0]
        $pre = @($pnpm | Select-Object -Skip 1)
        & $exe @pre --dir frontend install
        if ($LASTEXITCODE -eq 0) { & $exe @pre --dir frontend build }
        if ($LASTEXITCODE -ne 0) {
            Write-Host ' Building the dashboard failed (see above). The app will start without it.' -ForegroundColor Yellow
        }
    }
}

& uv run @frozen tsclaude app @args
exit $LASTEXITCODE
