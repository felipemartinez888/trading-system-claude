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

## Known limits
- The live bar builder still writes closed live bars on the event-loop thread (one row per minute);
  it can wait briefly for the store lock while a ≤1000-row page commits.
- Resource limits bound the application's work; they do not, and cannot, guarantee anything about the
  operating system. Whether the earlier unresponsiveness is fully explained by the old unbounded
  write loop is unproven; the manual steps above are the check.
- `POST` to trigger history manually is intentionally not provided (D-D: read-only API); use the
  paper requirement or `history: background`.
