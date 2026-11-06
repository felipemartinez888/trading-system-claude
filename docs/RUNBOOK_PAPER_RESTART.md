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
