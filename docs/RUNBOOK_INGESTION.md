# Runbook — bounded startup ingestion (Start / Stop)

Applies to the live-bar ingestion that runs after **Start monitoring**. Design rationale and
acceptance criteria: [devlog 2026-11-09](devlog/2026-11-09-start-stability-bounded-ingestion.md).
This runbook is about application behaviour only; it does not cover the operating system or hardware.

## What Start does now
1. The dashboard Start returns as soon as the live tape is up. **It never waits for ingestion.**
2. In the background, one pair at a time, one REST page (≤ 1000 minutes) at a time, newest first:
   - **Catch-up** (every Start): the minutes since the last stored bar, capped at
     `backfill.startup.catchup_max_minutes` (production 1440 = 24 h). Older downtime is registered as
     a gap ("older than the startup catch-up"), not fetched.
   - **History** (the rest of `lookback_days`, 9 days): loaded **only on demand** (`history: on_demand`):
     when paper trading with a warm-up strategy is enabled, or never otherwise. `background` loads it
     right after the catch-up on every Start; `off` never loads it.
3. Each REST page is stored in **one SQLite transaction together with its checkpoint**, by a dedicated
   single writer thread. At most one page is in flight. Pauses, per-pair and per-run budgets and a
   consecutive-error limit bound the work (`backfill.run` in `configs/data_quality.yaml`).

## What Stop does
1. Asks the run to finish its current page (`stop_grace_s`), then cancels a REST fetch that is still
   pending (nothing is written by a fetch).
2. Joins the writer thread. A write that is running **is never abandoned and never overlapped**: Stop
   waits for the transaction (up to `write_join_timeout_s`).
3. Only then are WebSockets and REST clients closed, and only then may the SQLite connection close.
4. If the writer does not finish within `write_join_timeout_s`, the run is reported **stuck**
   (`phase: stuck`, `safe_to_close_store: false`) and the connection is deliberately left open.

## Readiness and incomplete history
- Paper with a warm-up strategy (`strategy` ≠ `none`) **requires** the whole `lookback_days` window.
  `wait_backfill` raises `HistoryIncomplete` unless every pair's history run finished `complete`
  (`partial`, `paused`, `failed`, `cancelled` do not count); paper then reports UNAVAILABLE with that
  reason instead of opening on a partial history.
- A partial history can never create readiness on its own: the strategy adapter counts contiguous
  stored bars and needs the full warm-up (`warmup_progress`).
- Nothing about stored bars changes: source-finality, conflict events and observation tags are as before.

## Where to look
- Dashboard/API: `GET /api/ingestion` (read-only): `phase`, `catchup_done`, `history.{policy,state,complete}`,
  `writer.{alive,busy,write_slow,stuck}`, `safe_to_close_store`, per pair `state`, `chunks_done/total`,
  `cursor_ms`, `rows_processed`, `inserted`, `errors`, `last_error`, `checkpoint`, plus `notes`.
- Plan without loading anything: `tsclaude data backfill-plan` (read-only, no network, no writes).
- Events: one `BACKFILL_COMPLETED` per pair per run; payload keys are additive (`status`, `chunks_done`, …).

## Configuration (`configs/data_quality.yaml`, validated at load)
| Key | Production | Meaning |
|---|---|---|
| `lookback_days` / `max_backfill_days` | 9 / 30 | history target / hard cap (unchanged) |
| `startup.catchup_max_minutes` | 1440 | bounded Start catch-up (`null` = legacy: whole lookback) |
| `startup.history` | `on_demand` | `off` \| `on_demand` \| `background` |
| `run.chunk_minutes` | 1000 | rows per transaction = rows in flight |
| `run.inter_chunk_pause_ms` | 25 | event-loop slack between pages |
| `run.chunk_timeout_s` / `write_timeout_s` / `write_join_timeout_s` | 30 / 30 / 90 | fetch cancel / write reported slow / writer declared stuck |
| `run.pair_budget_s` / `run_budget_s` | 120 / 600 | then the run **pauses** (resumable); `run_budget_s` ≤ paper `backfill_wait_s` (tested) |
| `run.max_consecutive_chunk_errors` / `stop_grace_s` | 3 / 10 | |

These numbers are starting points, validated by the synthetic tests; tune them only from measurements.

## Development profile
`TSC_INGEST_PROFILE=dev` (opt-in; `configs/ingest_dev.yaml`): BTCUSDT spot only, 60-minute catch-up,
100-minute pages, 250 ms pauses, small budgets, `history: off`. It narrows the universe, never widens
it. With `history: off`, enabling a paper warm-up strategy makes paper UNAVAILABLE by design; keep
`strategy: none` in dev, or change the profile deliberately.

## Manual validation (nothing here loads real history)
All commands are PowerShell, run from the repository folder. **None of them needs Administrator.**

1. Plan only (read-only, no network):
   `uv run tsclaude data backfill-plan`
   Expect `history_policy: on_demand`, `catchup.window_minutes: 1440`, small chunk counts, and the note
   "plan only".
2. Dev profile plan: `$env:TSC_INGEST_PROFILE = "dev"; uv run tsclaude data backfill-plan`
   Expect one pair, `catchup.window_minutes: 60`.
3. Start the dashboard in the dev profile (`$env:TSC_INGEST_PROFILE = "dev"`), press Start. The page must
   stay responsive. Open `http://127.0.0.1:<port>/api/ingestion` (the dashboard's address): `phase`
   `running`, pair `state` moving `running` → `complete`, `writer.stuck: false`.
4. Press Stop while `phase` is `running` (the catch-up is short, so repeat Start/Stop a few times).
   Expect Stop to return within about `stop_grace_s`; `/api/ingestion` shows `phase: stopped`,
   `safe_to_close_store: true`.
5. Press Start again: the catch-up resumes from the stored bars (the plan's `stored_minutes` grows,
   `missing_minutes` shrinks); no gap is opened for an interrupted first load.
6. Only after steps 1–5 are uneventful, try the production profile (unset the variable). Its first
   catch-up is 24 h (two pages), not 9 days.

If something looks wrong, report `/api/ingestion` (the `notes` array explains `write_slow` and
`writer_stuck`) and the log lines starting `ingestion:`.

## Production-profile bounded test and Paper O-1
Owner-run, supervised, paper-only, public market data only (no keys, nothing sent to an exchange). All commands are PowerShell with **NORMAL privileges
(no Administrator)**, from the root of the checkout under test (the commit being accepted; the
stages were first run on branch `feat/ingestion-bounded`, since merged into the integration branch). Do one stage at a time and stop at the first surprise.
Production pairs: spot and USD-M BTCUSDT, ETHUSDT, SOLUSDT (6).

**Stage A: plan only (seconds, no network, no writes).**
```powershell
Remove-Item Env:TSC_INGEST_PROFILE -ErrorAction SilentlyContinue   # production profile
uv run tsclaude data backfill-plan
```
Expect `history_policy: on_demand` and, per pair, `catchup.window_minutes: 1440` and `history.window_minutes: 12960`; `catchup.chunks` / `history.chunks` count the REST pages still missing (at most 2 and 13 on an empty store).

**Stage B: production Start with paper OFF, fresh private data directory (about 12 REST pages in total, about 17k rows; not a historical import).**
```powershell
$run = "C:\Users\felip\tsc_runs\prod-" + (Get-Date -Format "yyyyMMdd-HHmm")
$env:TSC_DATA_DIR = "$run\data"
New-Item -ItemType Directory -Path $run | Out-Null
powershell -ExecutionPolicy Bypass -File scripts\TradingSystemClaude.ps1
```
Start, open `<dashboard address>/api/ingestion`. Expect the 6 `catchup` pairs (`pairs[].state`) moving to `complete`, `history.state: not_requested` (history is **not** loaded),
`writer.stuck: false`, dashboard responsive. Press Stop: it returns within about 10 s, `phase: stopped`, `safe_to_close_store: true`. Start again: only the
minutes since the stop are fetched. Report the `/api/ingestion` JSON.

**Stage C: Paper O-1 (first RUNNING paper session with the reference strategy; this is the first real 9-day history load).**
Only after Stage B is clean. Paper needs about 7.8 days of unbroken 1m history per instrument, loaded on demand (6 pairs x 13 pages of at most 1000 rows).
```powershell
$run = "C:\Users\felip\tsc_runs\paper-" + (Get-Date -Format "yyyyMMdd-HHmm")
$env:TSC_DATA_DIR   = "$run\data"
$env:TSC_CONFIG_DIR = "$run\configs"
Remove-Item Env:TSC_INGEST_PROFILE -ErrorAction SilentlyContinue   # make sure the dev profile is off
New-Item -ItemType Directory -Path $run | Out-Null
Copy-Item configs $env:TSC_CONFIG_DIR -Recurse
(Get-Content "$env:TSC_CONFIG_DIR\paper.yaml") -replace 'enabled: false','enabled: true' -replace 'strategy: none','strategy: bracket_reference' |
  Set-Content "$env:TSC_CONFIG_DIR\paper.yaml" -Encoding utf8
powershell -ExecutionPolicy Bypass -File scripts\verify_live.ps1 -Hours 1 -NoBrowser
```
Watch `/api/ingestion` (history `running`, `history_required: true`, pairs `history` job advancing) and the Paper page.
- **Expected and safe:** the history run has a 120 s per-pair and 600 s per-run budget. If it pauses before all 6 pairs finish, paper shows UNAVAILABLE with
  "required history window is not complete" (it never opens on a partial history). Stop, Start again in the same data directory: the stored bars and
  checkpoints are reused and only the rest is fetched. Two or three cycles are normal for a first load. Do not raise budgets or reduce pairs to force it;
  report the numbers instead (the budgets are starting points).
- Once history is complete, paper opens; the strategy is `ready` only after its warm-up, and an unobserved live minute restarts the warm-up (known limitation).
- A `strategy` decision before readiness must not appear. If the Paper page shows a signal while `/api/ingestion` history is not `complete`, stop and report.
- After the run ends by itself (clean stop writes `session_end`), verify (NORMAL, exit code 0 PASS, 1 FAIL, 2 INCONCLUSIVE):
```powershell
uv run tsclaude verify-replay --data-dir $env:TSC_DATA_DIR --config-dir $env:TSC_CONFIG_DIR
```
Report: `/api/ingestion` before and after, time to complete the history, the Paper state, and the verify-replay result. A PASS shows reproducibility of that
session only; it is not availability, profitability, M3 acceptance or MVP acceptance.

## Dependency lock files (owner review, read-only, NORMAL privileges)
Before anyone commits `uv.lock` or `frontend\pnpm-lock.yaml`, check each locally:
```powershell
git status --short                                   # the three files are untracked
(Get-Content uv.lock).Count; (Get-FileHash uv.lock).Hash
Select-String -Path uv.lock -Pattern 'source = \{ registry = "([^"]+)"' | ForEach-Object { $_.Matches[0].Groups[1].Value } | Sort-Object -Unique
Select-String -Path frontend\pnpm-lock.yaml -Pattern 'resolution: \{tarball' | Select-Object -First 3   # expect none (registry hashes only)
Get-Content frontend\pnpm-workspace.yaml
uv lock --check
```
Expected: the only registry is `https://pypi.org/simple`, no local-path or git sources, `uv lock --check` succeeds, and `pnpm-workspace.yaml` adds nothing beyond
`onlyBuiltDependencies: [esbuild]` already in `frontend/package.json`. Paste the output here and the review can be finished; committing remains a separate owner decision.

## Known limits
- The live bar builder still writes closed live bars on the event-loop thread (one row per minute);
  it can wait briefly for the store lock while a ≤1000-row page commits.
- Resource limits bound the application's work; they do not, and cannot, guarantee anything about the
  operating system. Whether the earlier unresponsiveness is fully explained by the old unbounded
  write loop is unproven; the manual steps above are the check.
- `POST` to trigger history manually is intentionally not provided (D-D: read-only API); use the
  paper requirement or `history: background`.
