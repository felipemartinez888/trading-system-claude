# Runbook — paper session restart (MVP, manual, supervised)

Scope: supervised, paper-only MVP. This is a manual procedure, not autonomous recovery
(M3 availability stays an open product requirement; see `tasks/M3-PB.md`, OD-2).

**A SAFE_BLOCK is NOT a successful recovery**: it is a fail-closed stop that is safe, not equivalent to the
uninterrupted run, and not availability. Never report a blocked session as recovered.

## Outcomes after a restart (OD-2)
- **RECOVERED**: the session resumes; ledgers equal the uninterrupted run.
- **SAFE_BLOCK**: the service stays in `AWAITING_BACKFILL`. No bar is processed and resting SL/TP are
  not resolved. Entries stay blocked (fail closed). Cause: a minute that no store holds
  (empty minute, or a gap of an *exposed* instrument).
- **FAIL**: a defect; stop and escalate.

## Procedure on SAFE_BLOCK
1. Do not edit the journal or the database. Do not force a resume.
2. Check the Paper page / logs for the backfill `problems` (instrument and minute).
3. If positions are open: check them manually at the venue's public price; the paper book will not
   close them while blocked. Note the mark in the devlog.
4. Stop monitoring, then start a **new** paper session (or restart monitoring once, if the market-data
   store has since recovered the missing minute; one backfill attempt is made per start).
5. Record the event (date, problems, exposure at the block) in `docs/devlog/`.

## Limitations
- A blocked session loses the interval until restarted; open paper positions are not managed meanwhile.
- A minute released with no bar still blocks (OD-1, characterised, not eliminated).
- Real SAFE_BLOCK frequency on Binance is not measured (harness: 15 of 60 delayed cases, synthetic).
- No CI or Linux run of this work yet.

## Recording a session and verifying its replay (M3 gate, supervised)

Paper only: nothing is sent to an exchange; only public Binance market data is read (no keys, no
paid service). A clean stop (the run ends by itself, the dashboard's Stop, or one Ctrl+C in the
console) writes `<data dir>\session_end\*.json` with the live ledger hashes. A crash, a killed
window or a power loss leaves no such file, and the verifier then says INCONCLUSIVE.

All commands: PowerShell, **NORMAL** privileges (no Administrator needed). From the repository root:

```powershell
# 1. fresh data directory and a private copy of the configs with paper trading on (NORMAL)
$run = "C:\Users\felip\tsc_runs\paper-" + (Get-Date -Format "yyyyMMdd-HHmm")
$env:TSC_DATA_DIR   = "$run\data"
$env:TSC_CONFIG_DIR = "$run\configs"
New-Item -ItemType Directory -Path $run | Out-Null          # fails if it already exists
Copy-Item configs $env:TSC_CONFIG_DIR -Recurse
(Get-Content "$env:TSC_CONFIG_DIR\paper.yaml") -replace 'enabled: false','enabled: true' |
  Set-Content "$env:TSC_CONFIG_DIR\paper.yaml" -Encoding utf8

# 2. record: monitoring auto-starts, ends cleanly after the given hours (NORMAL)
powershell -ExecutionPolicy Bypass -File scripts\verify_live.ps1 -Hours 0.5 -NoBrowser

# 3. verify the replay (NORMAL); exit code 0 PASS, 1 FAIL, 2 INCONCLUSIVE
uv run tsclaude verify-replay --data-dir $env:TSC_DATA_DIR --config-dir $env:TSC_CONFIG_DIR
```

Notes: the repository's `configs\paper.yaml` is not modified. `strategy: none` (default) trades only
the benchmark book, so the baseline and shadow ledgers stay empty and prove little; set
`strategy: bracket_reference` in the private copy to exercise them (it needs the startup backfill
of ~8 days of 1m bars, which Binance serves for free but takes minutes). Stop the app before step 3
(the verifier works on a copy, but a running app changes the files it fingerprints). The M1 report
that `verify_live` prints is about market data, not about this gate. A PASS of a short session
shows reproducibility of that session only; it is not availability, profitability or the
multi-restart property.
