# 2026-11-10 — M5 agent layer, the part you can test with a mock

Branch: written on `task/m5-agents`; the commits are cherry-picked onto `task/mvp-integration`,
which is where the work lands. Revised the same day after an independent review returned
CHANGES REQUESTED (see "Review fixes" below).

**Status: the mock half of the M5 gate is implemented and tested. M5 is not accepted.** The live
half of the gate (a real key, measured cost and latency per signal on live signals) has not been
started. The agent gate is also not wired into the paper runtime yet. Nothing here is evidence of
agent value.

## What exists

Package `src/tsclaude/agents/` (SIGNALS_AND_AGENTS §3–§6, ADR-0007, REQ-AGT-01…06):

| Module | Function |
|---|---|
| `config.py` + `configs/agents.yaml` | `mode: "off"` by default (quoted, because YAML 1.1 reads a bare `off` as `false`). Sonnet 5.5 at $2/$10 per MTok, a cap of **$1 per UTC day** (D-03), a 30 s timeout per call, 1 retry, and a circuit breaker that opens after 3 failures and cools down for 600 s. Unknown keys, duplicate YAML keys, out-of-range values and zero prices outside mode off are rejected. **Any invalid config forces mode off** and the errors are reported. |
| `packet.py` | Evidence packet as a flat `field_id -> value` map. Prices are relative to the reference price in ATR units. Bars are oldest first. Volume is relative to the window mean. Features, health and risk values pass through allowlists from the config, and only dimensionless scalars and short lowercase labels are accepted. Dropped names are listed by name only, never by value. The symbol, dates, timestamps, the rationale and the stop-basis text are never included. A masking audit refuses the packet if the symbol (or its base asset), an ISO date or an epoch-like number appears anywhere. An allowlisted value that equals a raw price also refuses the packet. A bar with `close_time > decision_time` is refused (no look-ahead), and so is a last bar more than one interval older than the decision time (stale). Labels with a run of 4 or more digits are dropped (they could be dates). |
| `prompts/analyst_v1.md`, `prompts/skeptic_v1.md`, `prompt_registry.py` | Versioned prompt files. Each prompt is identified by `version@sha256[:12]`: `analyst-v1@561d297cf48e`, `skeptic-v1@567cad61c4a5`. |
| `schema.py` | Strict JSON. A reply must be exactly one object: no fences, no prose, no duplicate keys, no NaN. Unknown keys are rejected at every level (`extra="forbid"`). No field can express a price, size, stop or take-profit. `score` is a `StrictInt` in −2..2. Every `evidence_refs` entry must name a packet field; `[i]` indexes into list fields are bounds-checked. Every number in the free text must exist in the packet (a lower-precision quote of a packet number is accepted). |
| `synthesis.py` | Rule table `synthesis-v1`, first match wins: fatal objection → VETOED_AGENT; oppose with total ≤ −3 → VETOED_AGENT; oppose → DOWNGRADED; total < 0 or any −2 → DOWNGRADED; ≥ 2 material objections → DOWNGRADED; otherwise APPROVED. `advisory_status` can only move CONFIRMED to VETOED. `book_admission`: agents never change the baseline book, and `agent_gated` admits only APPROVED reviews of signals that passed the risk gate. |
| `client.py` | The `AgentClient` protocol and a deterministic `MockClient`. Its scripted steps are valid, raw text, invalid JSON, timeout, API down, crash and slow. An unscripted call fails closed. |
| `budget.py` | Append-only JSONL cost ledger with **reserve, then settle**. Before each call attempt a `reservation` entry carries the worst case (one token per input character plus the full output allowance). It is appended only if `spent today + worst case <= cap`, and the check and append are atomic: a thread lock plus an OS file lock (POSIX `flock`; on Windows `msvcrt.locking`, which is untested here), after re-reading the file. Several gates or processes sharing one ledger file therefore share one cap. After the call, a `settlement` entry carries the real cost: measured usage; the worst case for a timeout or an unexpected error; 0 for `ClientUnavailable`. A call's spend is its settlement if one exists, otherwise its reservation, so a crash mid-call keeps the worst case after a restart. Every line is validated strictly (types, enums, finite cost of at least 0, `day == at_ms // day`, reservation/settlement pairing). Any violation, or an unreadable or unwritable file, makes the budget **refuse every reservation**. Money is `Decimal` quantized to 1e-8. **The cap holds only while each measured cost is at most its reservation.** A higher measured cost is recorded in full (not clamped down, since the real spend is what counts) and counted as `over_reservation_calls` in the status. |
| `breaker.py` | Circuit breaker: CLOSED, then OPEN after N consecutive failed reviews, then HALF_OPEN after the cooldown. HALF_OPEN allows a single trial. Budget refusals do not count as failures. |
| `cache.py` | Cache keyed by `sha256(run class, packet, prompt refs, model, synthesis version)`, where the run class is `live` or `indicative`, so a review is never served across LIVE and REPLAY/BACKTEST signals. The cache is persisted as JSONL. Only completed reviews are cached. `reevaluate=True` appends a new record, and the newest record is served. |
| `gate.py` | `AgentGate.review(signal, risk_passed=, inputs=<thunk>)` returns an `AgentOutcome`. In mode off it returns immediately: no packet, no prompt, no files, no client. A per-call deadline is enforced by the gate (daemon thread), so a slow client cannot hold a signal. A non-`Exception` raised in the worker (e.g. `SystemExit`) becomes a charged client failure. Any exception becomes `AI_UNAVAILABLE`, and an exception during a half-open breaker trial counts as a failure (try/finally), so the trial is never left in flight. `AgentOutcome` refuses to exist with a verdict unless its state is REVIEWED. |
| `real_client.py` | **Stub.** It checks only whether a key is present, never reads its value, and does not look for a key in mode off. Every call raises `ClientUnavailable`, which the gate reports as `AI_UNAVAILABLE`. The `anthropic` SDK is not installed and is not imported anywhere. |
| `status.py`, `src/tsclaude/api/agents.py` | Read-only `GET /api/agents/status` as a `SYSTEM` value, or `UNAVAILABLE` with a reason. It reports mode, config errors, the cap, prompts, today's spend from the ledger, and `runtime_wired: false`. It is wired in `api/app.py` with one `include_router` call. |

Mode semantics, as implemented and following §4: `off` does nothing. `confirmed` reviews CONFIRMED
signals that passed the risk gate; non-LIVE signals are labelled `indicative`. `research` reviews
REPLAY/BACKTEST signals only, all indicative, synchronously (the Batch API is not implemented).
Every failure (`AI_UNAVAILABLE`, `BUDGET_EXHAUSTED`, `AGENT_OUTPUT_INVALID`) leaves the signal
exactly as in mode off for the baseline book and keeps it out of `agent_gated`.

## Evidence (this container: Python 3.13.18, no network, no fastapi, no httpx, no pytest-asyncio)

- `PYTHONPATH=src python3 -m pytest tests/unit/agents -q -p no:cacheprovider` → **148 passed**
  (113 before the review fixes).
- `tests/integration/test_agents_status_api.py` → skipped here (fastapi missing); it runs in CI.
  It now uses a temporary working directory with a known config and asserts the configured
  state; it no longer returns early.
- `ruff check src tests`: all checks passed. `ruff format --check src tests`: clean.
- `mypy src`: the new findings are missing-stub noise only. They are `yaml` in `agents/config.py`
  (including "cannot subclass SafeLoader (Any)" and one `no-any-return`, both artefacts of the
  missing stubs) and `fastapi` in `api/agents.py`.
- The rest of `tests/unit` (excluding sim/research/backtest, which time out here) has 16 failures,
  all environmental: `httpx` and `pytest-asyncio` are not installed, and the failing modules were
  not touched.

Mutation checks (applied by a script, suite re-run, then reverted):

| Mutant | Result |
|---|---|
| APPROVED promotes a status; DOWNGRADED admitted to `agent_gated`; packet inputs built in mode off; schema `extra="ignore"` | killed (first round) |
| per-attempt reservation ignores the cap | killed |
| unexpected client error charged $0 | killed |
| negative ledger cost accepted; `day` check removed | killed |
| reservation written with cost 0 | killed |
| ledger not re-read before a reservation (per-instance cap) | killed |
| OS file lock removed | killed (a test holds the lock and checks the review blocks) |
| run class removed from the cache key | killed |
| breaker `finally` removed | killed |
| **gate lock removed** | **survives, by design**: the cap no longer depends on it (atomic ledger reservations). The concurrent test (8 threads, 2 gates, one ledger) passes with and without it. The lock now only serializes the breaker and counters. |
| **worker re-raises a `BaseException`** | **survives, behaviour unchanged**: the thread dies, the empty result box is turned into `ClientCrashed` by the second guard, and it is charged the same way. |

Measured sizes (synthetic packet with 48 bars and 7 features; tokens are a **chars/4 estimate**
because no tokenizer is available offline):

| Item | Chars | ≈ tokens |
|---|---|---|
| packet (48 bars) | 2,823 | ~706 |
| analyst system prompt | 2,410 | ~603 |
| skeptic system prompt | 1,878 | ~470 |
| analyst call input (prompt + packet) | 5,250 | ~1,313 |
| skeptic call input (prompt + packet + analyst JSON) | 5,210 | ~1,303 |
| typical analyst / skeptic output | 505 / 176 | ~127 / ~44 |

This is well below the §5 planning assumption of about 5,000 input tokens per call. At Sonnet
prices and one token per character, the up-front check needs $0.0721 of headroom per review
(it assumes the largest possible analyst reply). The per-call reservations actually held are about
$0.021 each, about $0.042 per review, before settlement. The $1 cap therefore admits at least
about 13 reviews per day even if every call cost its full worst case; measured costs settle lower. Real token counts, cost and latency must be measured with the API.

## What the M5 gate still needs

1. **A real adapter and a live measurement.** Implement the real client: lazy SDK import,
   `messages.create` with timeout, usage mapping, prompt caching of the system prompt. Then run it
   with a key in `.env` on the owner's machine and record cost and latency per signal on live
   signals. Also verify the exact Sonnet 5.5 model id and refresh the prices.
2. **Wiring into the paper runtime.** Call `AgentGate.review` after the risk gate for CONFIRMED
   signals, off the bar-processing thread so a review cannot delay deterministic execution, and
   persist outcomes as events (see the contract requests below).
3. **The `agent_gated` paper book**: not built. `book_admission` defines its admission rule.
4. **The dashboard Agent tab and cost page**: not built. `GET /api/agents/status` exists.
5. A test that the app is fully functional with mode `off`: the unit tests show the gate is inert,
   but an end-to-end paper run with the wired gate in mode off is still missing.

## Open issues

- **Ambiguity for the Lead:** the lifecycle diagram says `CONFIRMED → VETOED` on an agent veto,
  but the A/B design needs `baseline` unaffected by agents. I implemented baseline as unaffected
  and expose the veto as `advisory_status` and the `agent_gated` admission only. Please confirm.
- The rule "no numbers that are not in the packet" is strict, and prompts ask the model to prefer
  words. The rate of invalid outputs must be measured live; the rule was not loosened.
- The masking audit can refuse a packet by chance when a base asset name (3 letters or more)
  occurs inside a field name or label. That fails closed (`AI_UNAVAILABLE`); watch the rate.
- Deadline threads are abandoned, not killed. The real adapter must also pass `timeout` to the SDK.
- The gate serializes reviews with a lock (it keeps the breaker and counters exact; the budget is
  enforced by the ledger). Throughput is bounded by the latency of two calls.
- The ledger file is re-read on every reservation; this is fine at tens of calls a day, but it
  would need compaction at much higher volume.
- The Windows `msvcrt.locking` path runs on Linux only with a stub `msvcrt` (re-review N4: the
  retries are bounded at 6 x ~10 s and then fail closed); a real lock needs a run on the
  owner's PC.
- **Operator note (re-review N6):** a crash that tears the last ledger line blocks all agent
  spend (fail closed, `BUDGET_EXHAUSTED`, `ledger_error` in `/api/agents/status`) until the
  torn line is removed by hand from `<data_dir>/agents/` (copy the file first; delete only the
  last, incomplete line). No automatic repair: an unreadable ledger cannot prove the spend.

## Re-review of the fixes (Fable, 2026-11-10)

Verdict CHANGES REQUESTED, narrow: B2, B3, M1, M2, M3 and the minors FIXED; B1 PARTIAL. Fixed in
the follow-up commit (`tests/unit/agents/test_agents_rereview_fixes.py`, 8 tests; the three N1
cases fail on the previous code and the N4 test hangs there):

| Finding | Fix |
|---|---|
| N1 `cost_usd "1E+40"` (`decimal.InvalidOperation`), invalid UTF-8, a deeply nested line escaped as exceptions (constructor, `snapshot()`) | `parse_ledger` also maps `ArithmeticError` and `RecursionError`, `read_ledger` maps `UnicodeDecodeError`, to `LedgerUnreadable`: spend refused with `BUDGET_EXHAUSTED`, no client call |
| N3 surviving mutants (up-front two-call check, `release_trial`, settlement day) | one test each: a cap that fits the analyst alone makes no call; a budget refusal in a half-open trial lets the next trial run; a settlement on another day refuses |
| N4 unbounded Windows lock retry | 6 attempts, then `LedgerUnreadable` (fail closed); the constructor records it as the ledger error |
| N6 torn last line | operator note above (no code change: fail closed is the intended behaviour) |

Closure review (Fable): CONDITIONAL PASS, 0 blocking. Its R3 (a ledger lock that times out mid-review gave AI_UNAVAILABLE, counted as a breaker failure, and `snapshot()` raised) and R4 (the lock test would hang, not fail, on a regression) are fixed: `preview`, `try_reserve` and `settle` turn a lock failure into a budget refusal (a settle keeps the worst-case reservation), and `snapshot()` reports `ledger_error` with unknown spend fields.

## Requests for the Lead (contracts — not edited)

- `EventType.AGENT_REVIEW_COMPLETED` (payload: `AgentOutcome` state, verdict, review_id or key,
  reason, latency, calls) and `EventType.AGENT_COST_RECORDED` (one `CostEntry`, i.e. the
  `cost_ledger` event in §4), plus `AggregateType.AGENT`. Until they exist, the ledger and the
  cache are JSONL files under `<data_dir>/agents/`.
- An `agent_review_id` / `agent_review_state` on the signal projection (the §2 table lists it;
  `SignalRecord` has no such field).
- A decision on the baseline-veto ambiguity above.
- `docs/devlog/metrics.csv` and `docs/ROADMAP.md` were not updated (outside this task's scope),
  although CLAUDE.md asks every task to update them.
