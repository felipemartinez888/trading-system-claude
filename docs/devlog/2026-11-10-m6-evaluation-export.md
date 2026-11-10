# 2026-11-10 — M6 (MVP part): evaluation export, schemas, verifier, independent scorer

Branch `task/m6-export` (base `9ef847a`). Role: Quant Engineer (evaluation). Not pushed, not merged.
**Not M6 acceptance**: the export part of the M6 gate has local test evidence only (no CI run yet,
no real recorded session exported).

## What is exported (format `tsc-eval` v1)
`tsclaude.evaluation.export.export_store` reads the hash-chained event store through
`EventStore.read` / `verify` / `head` only (no lake, pyarrow or duckdb; the import guard passes) and
writes one canonical-JSON Lines file per record family plus `manifest.json`:

| File | Records (event types) | Provenance |
|---|---|---|
| `steps.jsonl` | `paper_step`: the input of every paper batch (bar with `observed`, `released_at_ms`, `release_latency_ms`, `minute_closed`; funding; manual reset; the first carries the session config) | bar source: `ws_live` LIVE, `rest_recovered` RECOVERED, archive/backfill HISTORICAL; other steps SIMULATED |
| `signals.jsonl` | `signal_created` (full SIGNALS_AND_AGENTS §2 record, every run mode) | demo detector DEMO; in a paper session SIMULATED |
| `transitions.jsonl` | `signal_transitioned`, `signal_shadow_outcome` | SIMULATED (DEMO for a demo signal) |
| `fills.jsonl`, `positions.jsonl`, `orders.jsonl`, `ledger.jsonl`, `sim_events.jsonl` | every simulator event per book (fills, opened/closed positions = trade records, order lifecycle and request rejections, ledger postings, run start, bar gaps, halts, resets, funding, observation changes) | SIMULATED |
| `coverage.jsonl` | app sessions, monitoring start/stop, crash detection, `gap_detected`/`gap_resolved`, backfill, late/conflicting bars, stream and clock state, chain checks | SYSTEM |

Each line holds the **verbatim source event** (seq, ts, type, aggregate, payload, schema version,
`prev_hash`, `hash`), `paper_session_id` and `batch_seq` (its paper batch), the chain head at export
time, `line_no`, `prev_line_hash` and `line_hash`. The manifest holds the app version, optional git
commit, the protocol document and status (DRAFT v0, hash null until frozen), optional export time,
chain head, per-file line counts, SHA-256 and last line hash, schema hashes, paper session ids, and
the event types present but not exported with counts (research events and any unknown type).
Nothing is fabricated or inferred: gaps appear only as the store recorded them. The export fails
closed on a broken chain, a truncated/nested batch, an unmapped type inside a batch, a simulator or
lifecycle event outside a batch that is not a demo signal, or a non-empty output directory.
Deterministic: the same store up to the same head gives byte-identical files (export time only in
the manifest, and only when given).

Deviation from EVALUATION_PROTOCOL §4 (draft): schemas live in
`src/tsclaude/evaluation/schemas/v1/` (not `contracts/export/`, which is Lead-only), there are more
files than §4 lists (steps, orders, ledger, sim_events are needed to recompute the books), and
signals are exported for every run mode with `run_mode` in the record instead of LIVE only (dropping
records would break the batch-completeness check; a consumer filters).

## Verification
- `scorer.check_export` (export alone): JSON Schema 2020-12 validation of every line and the
  manifest, canonical form, line hash, `prev_line_hash` links, line numbers, file SHA-256, line
  counts and last line hash against the manifest, every source event's chain hash recomputed,
  chain links between consecutive exported events, increasing seq per file, one chain head,
  every paper batch complete (`batch_size` contiguous lines).
- `verify.verify_export(..., store=)` adds: the store chain verifies, the export head is in the store
  (the store may have grown), the bundled schema hashes match, and a re-export up to that head is
  byte-identical (no missing, added or edited line).

## Scorer: what it reproduces
`src/tsclaude/evaluation/scorer.py` is standard-library only (a test parses its imports and runs a
copy with `python -I` outside the package). From the export alone, per paper session and book:
equity (mark-to-market at the journaled bar closes), cash, unrealized P&L, open positions, signed
position quantities, halted reason, ledger reconciled, ledger equity at cost, ledger transactions,
**ledger hash**, closed trades and their flag counts (ambiguous, gap, flagged, deferred, exchange
downtime), fill-time rejections by reason, max drawdown with peak/trough times, longest drawdown,
equity points; the fills, closed trades and request rejections lists; batches, last minute and
`values_as_of_ms`; and the Signals page demo WATCH list (DEMO).

Evidence that it equals the app's own numbers (local, this branch):
`PYTHONPATH=src python3 -m pytest tests/unit/evaluation tests/unit/archive/test_arc_import_guard.py -q -p no:cacheprovider`
-> **44 passed, 1 skipped** (the HTTP-route test needs fastapi; it runs in CI only). The store holds
an app session with a crash record, two DEMO signals and two paper sessions through the production
journal: `paper` (spot lifecycle scenario: TP, stop, veto with shadow outcome, invalidation, a
missing bar; **restarted mid-session** and restored from the journal) and `paper-2` (USD-M short left
open, a spot long gapping through its stop, **drawdown kill switch halt**, operator reset). The
scorer equals `PaperTradingService.status()["books"]` (every key but `no_trade_deferrals`), the
ledger hashes, the `PaperRuntime` published snapshot that `/api/paper/books|fills|activity` serve,
and `signals_response` (`/api/signals`). Scratch mutants of the scorer (short-equity formula, minute
flush, halt and reset tracking, rejection counter, `sim_seq` stripping) are caught.

## What it cannot reproduce (listed in `scorer.NOT_DERIVABLE`)
Runtime status (state, reason, observation anchor, fault, clock stats, dropped updates, backfill,
previous run, live bar since start); the session audit log (separate file); the SIMULATED/STALE
freshness label (wall clock at read time); the reference strategy panel (CONFIRMED intents, WATCH
proposals, history restarts, warm-up, recent signals: per-run in-memory counters, WATCH never
journaled); `no_trade_deferrals` (API only, no event). The §5 protocol metrics are not on the
dashboard and are **not computed** (`PROTOCOL_METRICS_NOT_SCORED` states what each needs; by-regime
is not derivable: no regime label is recorded).

## Limitations
- No real recorded session has been exported yet; no CI run of these tests.
- Not exercised end to end: unobserved (downtime) bar steps, funding steps, fill-time order
  rejections (a direct unit test covers the counter), the `>=` tie at the running peak.
- The JSON Schema validator is a built-in subset (cross-checked against `jsonschema` 4.26 where
  installed, which is not a project dependency).
- Reading uses `EventStore(path)`, which creates the schema tables on open; the CLI refuses a
  missing file so no new store is created, and it never appends events.

## What remains for the M6 gate
- Lead: wire `register_evaluation(sub)` into `src/tsclaude/cli.py`; decide the schema location
  (§4 says `contracts/export/`); a CI run.
- Owner: export a real recorded session and run `tsclaude evaluation verify` and `score` on it.
- Not engineering: agreeing the protocol and the export format with the other team, then freezing
  it (hash into the manifest). Daily chain-head evidence commits and the analytics / agent-value
  parts of M6 are post-MVP and not started.
