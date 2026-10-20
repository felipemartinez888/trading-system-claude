# Scripts

| Script | Purpose |
|---|---|
| `TradingSystemClaude.cmd` | Windows launcher: double-click to start the app and open the dashboard. |
| `TradingSystemClaude.ps1` | Same launcher for PowerShell (`powershell -ExecutionPolicy Bypass -File scripts\TradingSystemClaude.ps1`). |
| `ci/` | CI helpers (used by `.github/workflows/ci.yml`). |
| `spikes/` | One-off verification scripts (e.g. Binance reachability). |

## First-time setup on Windows (3 commands)

In PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"   # 1. install uv
winget install OpenJS.NodeJS.LTS                                                      # 2. install Node.js (builds the dashboard once)
git clone https://github.com/felipemartinez888/trading-system-claude; cd trading-system-claude   # 3. get the code
```

No git? Download the repository ZIP from GitHub instead of step 3 and extract it.

Open a new terminal, then double-click `scripts\TradingSystemClaude.cmd` (or run it from the
terminal). The first run installs the Python dependencies (`uv sync --frozen`, or `uv sync` while
`uv.lock` is not committed yet), installs uv's Python 3.13 if needed, and builds the dashboard
bundle with pnpm through Node's `corepack`. Later runs start in a few seconds.

Without Node.js the app still starts, and the browser shows a page saying the dashboard bundle is
not built, with the current backend status. Install Node.js and run the launcher again to build it.

## What the launcher does

1. Checks that `uv` is on `PATH`; if not, prints the install command and stops.
2. On the first run (no `.venv`), runs `uv sync --frozen`.
3. If `src\tsclaude\web\static\index.html` is missing and pnpm or corepack is available, builds the
   bundle (`pnpm --dir frontend install` then `pnpm --dir frontend build`).
4. Runs `uv run tsclaude app` (extra arguments are passed through, e.g. `--no-browser`,
   `--port 9000`). The app listens on `http://127.0.0.1:8765/` only and opens the browser.

## Stopping

- **Quit** in the dashboard status bar, or **Ctrl+C** in the launcher window: monitoring stops and
  the session is recorded as ended cleanly.
- Closing the console window: the app tries to record a clean stop within the few seconds Windows
  allows. If the process is killed (Task Manager, power loss), the next start shows a notice
  that the previous session did not end cleanly, with the time span that was not observed.

## Manual acceptance check (owner, Windows)

Each case must end with the right notice on the next start (System › Health shows how the previous
session ended):

1. Double-click the launcher → the dashboard opens → **Start** / **Stop** toggle the state.
2. **Quit** in the dashboard → the page says the session was closed cleanly → relaunch: previous
   session "ended cleanly".
3. **Ctrl+C** in the launcher window → relaunch: "ended cleanly".
4. Close the console with the window's **X** button (this path uses the Windows console-close
   handler, not a signal, so it must be tested separately) → relaunch: "ended cleanly". If it
   shows a crash instead, report it: Windows allows only a few seconds for this path.
5. End the `python` process in Task Manager → relaunch: the crash notice with the unobserved
   interval.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `TSC_DATA_DIR` | `data` | Folder holding `state.db` and `logs\` (git-ignored). |
| `TSC_PORT` | `8765` | Dashboard port (loopback only). |
| `TSC_OPEN_BROWSER` | `true` | Open the browser on start. |
| `TSC_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `TSC_HEARTBEAT_INTERVAL_S` | `5` | Heartbeat interval used for crash detection. |

Only one app instance may use a data directory at a time; a second launch reports that the app is
already running.

## Exit codes

`0` clean exit · `1` unexpected error · `2` event store cannot be opened · `3` refused to start
(port in use or another instance running).
