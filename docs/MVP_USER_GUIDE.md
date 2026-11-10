# MVP user guide (Windows, paper only)

Everything here runs on your PC, uses only public Binance market data and never sends an order
anywhere. All commands are **PowerShell with NORMAL privileges** (no Administrator) unless
marked otherwise.

## 1. Install (once)
Follow [scripts/README.md](../scripts/README.md) "First-time setup on Windows". It takes three commands:
- uv;
- Node.js LTS;
- a clone of the repository.

To use this branch:
```powershell
cd C:\Users\felip\Projects\trading-system-claude
git fetch origin task/mvp-integration
git worktree add ..\tsc-mvp origin/task/mvp-integration      # a new folder; nothing else changes
cd ..\tsc-mvp
uv sync --all-groups
pnpm --dir frontend install; pnpm --dir frontend build      # the dashboard bundle is not committed
```
An existing checkout keeps its old dashboard bundle until you run `pnpm --dir frontend build` again.

## 2. Before every paper session: the clock
The paper runtime refuses to observe live bars when your PC clock is more than 1 s off Binance.
It fails closed: bars are processed unobserved, and no entry is created or filled.
```powershell
w32tm /stripchart /computer:time.windows.com /dataonly /samples:5
```
If any value is beyond ±0.5 s:
- Settings → Time & language → Date & time → **Sync now**, then check again;
- only if that fails, in an ADMINISTRATOR PowerShell: `w32tm /resync /force`.

## 3. Start, watch, stop
- **Start the app:** double-click `scripts\TradingSystemClaude.cmd`, or run `powershell -ExecutionPolicy Bypass -File scripts\TradingSystemClaude.ps1`. It serves `http://127.0.0.1:8765/` only.
- **Start / Stop monitoring:** use the buttons in the status bar. Start returns at once; ingestion runs in the background (`GET /api/ingestion` shows it).
- **Quit:** use the dashboard's Quit, or Ctrl+C in the launcher window. Both record a clean stop.
- **After an unclean end:** if the PC restarts or the process is killed, the next Start shows the unobserved interval and backfills it where it can.

**Where to look**

| Page / endpoint | Shows |
|---|---|
| System › Health | Session, event-chain integrity, observed vs. unobserved coverage |
| Live › Markets | Live bars per symbol with provenance |
| Paper | Paper state, observation anchor, **"What to do"** when something blocks it, books, fills, the REFERENCE strategy |
| Research › Backtests | **Research status**: today "NO VALIDATED STRATEGY", with reasons; recorded runs and grades |
| `GET /api/ingestion` | Catch-up and history progress, writer state, `safe_to_close_store` |
| `GET /api/research/status` | Tiers per strategy: REFERENCE, CANDIDATE, VALIDATED, REJECTED, UNREGISTERED |

Paper trading is **off by default**. To try it with the reference strategy in a separate data
folder, follow "Stage C" in [RUNBOOK_INGESTION](RUNBOOK_INGESTION.md).

## 4. One-command evidence run (unattended, about 1 h 30 min)
```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\felip\tsc_runs\mvp_evidence.ps1
```
It runs the following, writing each output to `C:\Users\felip\tsc_runs\mvp-evidence-<stamp>\summary.json`:
1. clock check;
2. crash recovery on a **copy** of the session a PC restart ended;
3. a fresh 1-hour paper session;
4. a reference backtest run twice;
5. `verify-replay` / `verify-chain` after each live run.

Options: `-SkipRecovery -SkipPaper -SkipBacktest -PaperHours 0.5`.

## 5. Research commands
- **Backtest one configuration.** Every run is a recorded trial. Holdout ranges are refused without an owner unlock.
  ```powershell
  uv run tsclaude backtest run --strategy bracket_reference --symbols spot:BTCUSDT --from 2024-01-01 --to 2024-04-01
  uv run tsclaude backtest list
  ```
- **Walk-forward on the development segment.**
  - Selection uses train windows only; only the winner runs on each test window.
  - `--grid` is a JSON list of parameter objects.
  - The run is refused outside 2022-01-01 → 2025-01-01, and above `--max-configs`.
  ```powershell
  uv run tsclaude backtest walkforward --strategy bracket_reference --grid grid.json `
    --symbols spot:BTCUSDT --hypothesis WF-EXAMPLE --initial-train-months 12 --test-months 3
  ```
  - **Verdicts:** `NO_EDGE_DEMONSTRATED`, `INSUFFICIENT_OOS_EVIDENCE`, `OOS_MARGIN_CERTIFIED_DEVELOPMENT_ONLY`.
  - None of them is "validated": validation (2025) and the sealed holdout come after, once, preregistered.
  - The report is written to `data\walkforward\wf-<ms>\walkforward.json`.
- **Replay a recorded paper session** (read-only; it copies the store):
  ```powershell
  uv run tsclaude verify-replay --data-dir <run>\data --config-dir <run>\configs
  ```

## 6. Tests
```powershell
uv run pytest -q          # full suite (live tests excluded by default)
uv run ruff check src tests scripts; uv run mypy src
pnpm --dir frontend test
```
CI runs the same commands on Ubuntu and Windows for every push to `task/**`.

## 7. Troubleshooting
| Symptom | Meaning | Action |
|---|---|---|
| Paper RUNNING, observation "blocked — server clock offset … beyond 1000 ms" | Your clock is off | §2, then Stop/Start |
| Paper UNAVAILABLE "required history window is not complete" | History budget paused | Stop, Start again in the same data folder (resumes) |
| Paper AWAITING_BACKFILL | Minutes missing after a restart | Wait; nothing is traded meanwhile |
| Dashboard shows "bundle not built" | Node build missing | `pnpm --dir frontend install; pnpm --dir frontend build` |
| PC restarted during a run | Hardware/OS event | Start again in the same folder; the gap is shown and backfilled where possible |
