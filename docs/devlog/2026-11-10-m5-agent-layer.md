# 2026-11-10 — M5 agent layer, the part you can test with a mock (branch `task/m5-agents`)

**Status: the mock half of the M5 gate is implemented and tested. M5 is not accepted.** The live
half of the gate (a real key, measured cost and latency per signal on live signals) has not been
started. The agent gate is also not wired into the paper runtime yet. Nothing here is evidence of
agent value.

## What exists

Package `src/tsclaude/agents/` (SIGNALS_AND_AGENTS §3–§6, ADR-0007, REQ-AGT-01…06):

| Module | Function |
|---|---|
| `config.py` + `configs/agents.yaml` | `mode: "off"` by default (quoted, because YAML 1.1 reads a bare `off` as `false`). Sonnet 5.5 at $2/$10 per MTok, a hard cap of **$1 per UTC day** (D-03), a 30 s timeout per call, 1 retry, and a circuit breaker that opens after 3 failures and cools down for 600 s. Unknown keys and out-of-range values are rejected. **Any invalid config forces mode off** and the errors are reported. |
| `packet.py` | Evidence packet as a flat `field_id -> value` map. Prices are relative to the reference price in ATR units. Bars are oldest first. Volume is relative to the window mean. Features, health and risk values pass through allowlists from the config, and only dimensionless scalars and short lowercase labels are accepted. Dropped names are listed by name only, never by value. The symbol, dates, timestamps, the rationale and the stop-basis text are never included. A masking audit refuses the packet if the symbol (or its base asset), an ISO date or an epoch-like number appears anywhere. An allowlisted value that equals a raw price also refuses the packet. A bar with `close_time > decision_time` is refused (no look-ahead). |
| `prompts/analyst_v1.md`, `prompts/skeptic_v1.md`, `prompt_registry.py` | Versioned prompt files. Each prompt is identified by `version@sha256[:12]`: `analyst-v1@561d297cf48e`, `skeptic-v1@567cad61c4a5`. |
| `schema.py` | Strict JSON. A reply must be exactly one object: no fences, no prose, no duplicate keys, no NaN. Unknown keys are rejected at every level (`extra="forbid"`). No field can express a price, size, stop or take-profit. `score` is a `StrictInt` in −2..2. Every `evidence_refs` entry must name a packet field; `[i]` indexes into list fields are bounds-checked. Every number in the free text must exist in the packet (a lower-precision quote of a packet number is accepted). |
| `synthesis.py` | Rule table `synthesis-v1`, first match wins: fatal objection → VETOED_AGENT; oppose with total ≤ −3 → VETOED_AGENT; oppose → DOWNGRADED; total < 0 or any −2 → DOWNGRADED; ≥ 2 material objections → DOWNGRADED; otherwise APPROVED. `advisory_status` can only move CONFIRMED to VETOED. `book_admission`: agents never change the baseline book, and `agent_gated` admits only APPROVED reviews of signals that passed the risk gate. |
| `client.py` | The `AgentClient` protocol and a deterministic `MockClient`. Its scripted steps are valid, raw text, invalid JSON, timeout, API down, crash and slow. An unscripted call fails closed. |
| `budget.py` | Append-only JSONL cost ledger. Money is `Decimal` quantized to 1e-8. Each entry records model, tokens, cost, cost basis, latency and outcome. The ledger is reloaded on restart, so a restart does not reset the day's spend. Before each review, both calls are reserved at worst case: one token per input character plus the full output allowance. Each attempt is checked again. Timeouts and unexpected errors are charged the worst case; `ClientUnavailable` is charged 0. An unreadable or unwritable ledger blocks every call. |
| `breaker.py` | Circuit breaker: CLOSED, then OPEN after N consecutive failed reviews, then HALF_OPEN after the cooldown. HALF_OPEN allows a single trial. Budget refusals do not count as failures. |
| `cache.py` | Cache keyed by `sha256(packet, prompt refs, model, synthesis version)` and persisted as JSONL. Only completed reviews are cached. `reevaluate=True` appends a new record, and the newest record is served. |
| `gate.py` | `AgentGate.review(signal, risk_passed=, inputs=<thunk>)` returns an `AgentOutcome`. In mode off it returns immediately: no packet, no prompt, no files, no client. A per-call deadline is enforced by the gate (daemon thread), so a slow client cannot hold a signal. Any exception becomes `AI_UNAVAILABLE`. `AgentOutcome` refuses to exist with a verdict unless its state is REVIEWED. |
| `real_client.py` | **Stub.** It checks only whether a key is present, never reads its value, and does not look for a key in mode off. Every call raises `ClientUnavailable`, which the gate reports as `AI_UNAVAILABLE`. The `anthropic` SDK is not installed and is not imported anywhere. |
| `status.py`, `src/tsclaude/api/agents.py` | Read-only `GET /api/agents/status` as a `SYSTEM` value, or `UNAVAILABLE` with a reason. It reports mode, config errors, the cap, prompts, today's spend from the ledger, and `runtime_wired: false`. It is wired in `api/app.py` with one `include_router` call. |

Mode semantics, as implemented and following §4: `off` does nothing. `confirmed` reviews CONFIRMED
signals that passed the risk gate; non-LIVE signals are labelled `indicative`. `research` reviews
REPLAY/BACKTEST signals only, all indicative, synchronously (the Batch API is not implemented).
Every failure (`AI_UNAVAILABLE`, `BUDGET_EXHAUSTED`, `AGENT_OUTPUT_INVALID`) leaves the signal
exactly as in mode off for the baseline book and keeps it out of `agent_gated`.

## Evidence (this container: Python 3.13.18, no network, no fastapi, no httpx, no pytest-asyncio)

- `PYTHONPATH=src python3 -m pytest tests/unit/agents -q -p no:cacheprovider` → **113 passed**.
- `tests/integration/test_agents_status_api.py` → skipped here (fastapi missing); it will run in CI.
- `ruff check src tests`: all checks passed. `ruff format --check src tests`: clean.
- `mypy src`: the new findings are missing-stub noise only (`yaml` in `agents/config.py`,
  `fastapi` in `api/agents.py`). No other errors in the new code.
- Mutation spot checks (manual, then reverted), each caught by at least one test: letting
  APPROVED promote a status, admitting DOWNGRADED to `agent_gated`, building inputs in mode off,
  and `extra="ignore"` in the schema.
- The rest of `tests/unit` (excluding sim/research/backtest, which time out here) has 16 failures,
  all environmental: `httpx` and `pytest-asyncio` are not installed, and the failing modules were
  not touched.

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

This is well below the §5 planning assumption of about 5,000 input tokens per call. The worst-case
reservation per review is $0.0345 at Sonnet prices, so the $1 cap admits at least 28 reviews per
day. Real token counts, cost and latency must be measured with the API.

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
- The gate serializes reviews with a lock (the budget and breaker stay exact). Throughput is
  bounded by the latency of two calls.

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
