@echo off
rem Trading System Claude - Windows launcher (double-click me).
rem 1. checks that uv is installed (prints install instructions otherwise);
rem 2. first run: installs dependencies with `uv sync --frozen` (exact versions from uv.lock);
rem 3. first run: builds the dashboard bundle if Node.js is available;
rem 4. runs `tsclaude app`: dashboard on http://127.0.0.1:8765/ in your browser.
rem    `uv run` keeps the environment in sync with uv.lock on later runs (no-op when unchanged).
rem Quit with "Quit" in the dashboard or Ctrl+C here; both record a clean shutdown.
setlocal EnableExtensions
title Trading System Claude
cd /d "%~dp0.."

where uv >nul 2>nul
if errorlevel 1 goto :no_uv

set "FROZEN="
if exist "uv.lock" set "FROZEN=--frozen"

if not exist ".venv\" (
  echo First run: installing dependencies, this can take a minute...
  uv sync %FROZEN%
  if errorlevel 1 goto :sync_failed
)

if not exist "src\tsclaude\web\static\index.html" call :build_bundle

uv run %FROZEN% tsclaude app %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
  echo.
  echo  Trading System Claude exited with code %CODE%.
  pause
)
exit /b %CODE%

:build_bundle
rem The dashboard bundle is not committed; build it once if Node.js is available.
set "PNPM="
where pnpm >nul 2>nul && set "PNPM=pnpm"
if not defined PNPM where corepack >nul 2>nul && set "PNPM=corepack pnpm"
if not defined PNPM (
  echo.
  echo  Note: the dashboard bundle is not built and Node.js was not found. The app will start
  echo  and show a page explaining this. Install Node.js LTS ^(see scripts\README.md^) and run
  echo  this launcher again to build it.
  echo.
  exit /b 0
)
echo Building the dashboard bundle once ^(needs internet the first time^)...
set "COREPACK_ENABLE_DOWNLOAD_PROMPT=0"
call %PNPM% --dir frontend install
if errorlevel 1 goto :bundle_failed
call %PNPM% --dir frontend build
if errorlevel 1 goto :bundle_failed
exit /b 0

:bundle_failed
echo.
echo  Building the dashboard failed ^(see above^). The app will start without it.
echo.
exit /b 0

:no_uv
echo.
echo  uv is not installed. Install it once by running this in PowerShell:
echo.
echo    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
echo.
echo  Then close this window and double-click TradingSystemClaude.cmd again.
echo  More: https://docs.astral.sh/uv/getting-started/installation/
echo.
pause
exit /b 1

:sync_failed
echo.
echo  Dependency installation failed. See the messages above.
pause
exit /b 1
