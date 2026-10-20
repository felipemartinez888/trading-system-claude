# Scripts

| Script | Purpose |
|---|---|
| `TradingSystemClaude.cmd` | Windows launcher: double-click to start the app and open the dashboard. |
| `TradingSystemClaude.ps1` | Same launcher for PowerShell (`powershell -ExecutionPolicy Bypass -File scripts\TradingSystemClaude.ps1`). |
| `verify_live.ps1` / `verify_live.sh` / `verify_live.py` | M1 gate evidence: timed live run with monitoring auto-started, JSON report in `evidence/live/`. |
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

## M1 live verification run (owner)

Collects the M1 gate evidence (ROADMAP M1): a live Binance run with monitoring auto-started.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify_live.ps1            # 2 hours (default)
powershell -ExecutionPolicy Bypass -File scripts\verify_live.ps1 -Hours 0.25 # quick check
```

(`scripts/verify_live.sh --hours 2` on Linux/macOS.) The dashboard opens as usual, so the run can
be watched. Do not start another instance on the same data folder during the run. While it runs:

1. **Induced disconnect:** disable the network for about one minute, then re-enable it. Expected:
   streams go DISCONNECTED → RECOVERING → HEALTHY, a notification appears, and the missed minutes
   are recovered via REST (or listed as an open gap with cause `disconnect`).
2. **Hard kill (separate run):** end the `python` process in Task Manager, start the launcher
   again. Expected: the crash notice with the unobserved interval, startup backfill of the
   missing bars (shown as backfill in the chart legend), and a `downtime` gap if REST cannot
   return some minutes.

At the end the app quits cleanly and writes `evidence/live/verify-<UTC>.json`:

| Field | Meaning |
|---|---|
| `stream_uptime` | per stream: share of monitored time in HEALTHY, transitions |
| `totals` | reconnects, planned recycles, connection-task crashes, malformed messages |
| `gaps` | detected / resolved / still open, minutes, by cause |
| `bars` | per symbol: minutes fully inside monitoring vs stored bars (by source); `missing_unexplained` lists minutes missing *without* an open gap |
| `clock_offset_ms` | min / max / mean / last of the per-minute samples, skew events |
| `verdict` | `pass` and the list of failed criteria |

Exit code: `0` pass · `1` monitoring did not start (reason in the report and on screen) · `2` the
run completed but a criterion failed. Attach the JSON file to the M1 review.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `TSC_DATA_DIR` | `data` | Folder holding `state.db` and `logs\` (git-ignored). |
| `TSC_PORT` | `8765` | Dashboard port (loopback only). |
| `TSC_OPEN_BROWSER` | `true` | Open the browser on start. |
| `TSC_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `TSC_HEARTBEAT_INTERVAL_S` | `5` | Heartbeat interval used for crash detection. |
| `TSC_CONFIG_DIR` | `configs` | Folder with `universe.yaml` and `data_quality.yaml` (read when monitoring starts; an invalid file makes Start fail with the reason). |
| `TSC_DEMO_DETECTOR` | `on` | `off` disables the demo WATCH detector. |
| `TSC_DEMO_THRESHOLD` | `0.003` | Demo detector: minimum 1m body |close/open − 1| (0.003 = 0.3 %). |

Only one app instance may use a data directory at a time; a second launch reports that the app is
already running.

## Exit codes

`0` clean exit · `1` unexpected error · `2` event store cannot be opened · `3` refused to start
(port in use or another instance running).
