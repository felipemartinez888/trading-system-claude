"""Advisory Claude signal-evaluation agents (M5; SIGNALS_AND_AGENTS §3-§6, ADR-0007).

Agents advise only: a review can approve, downgrade or veto a CONFIRMED signal that already passed
the deterministic risk gate. Nothing here sets a price, size, stop or take-profit, and nothing here
can admit a signal the risk engine blocked. With mode ``off`` (the default) no agent code runs on
any signal path. Every failure degrades to ``AI_UNAVAILABLE`` / ``BUDGET_EXHAUSTED`` /
``AGENT_OUTPUT_INVALID``, never to an approval.

Modules:
- `config`    -- ``configs/agents.yaml`` loader; invalid config fails closed to mode ``off``.
- `packet`    -- evidence packet builder: allowlisted, masked (symbol, dates, absolute prices).
- `prompt_registry` -- versioned, hashed prompt files under ``prompts/``.
- `schema`    -- strict output schemas and the packet-citation validator.
- `synthesis` -- deterministic rule table -> APPROVED / DOWNGRADED / VETOED_AGENT.
- `client`    -- client protocol, error types and the scripted mock client used by every test.
- `budget`    -- cost ledger (append-only JSONL) and the hard daily cap.
- `breaker`   -- circuit breaker over consecutive failures.
- `cache`     -- review cache keyed by packet hash + prompt hashes + model + synthesis version.
- `gate`      -- the orchestrator: `AgentGate.review(...)` -> `AgentOutcome`.
- `real_client` -- key-presence check and a stub for the real Anthropic adapter (not implemented).
- `status`    -- read-only status snapshot for ``GET /api/agents/status``.
- `wiring`    -- M5-3: the deterministic ``agent_gated`` state of a paper session (requests,
  journaled outcomes, admissions; replayable, never calls a client).
- `worker`    -- M5-3: reviews on a worker thread off the event loop, packet inputs from closed
  bars, the paper runtime's reviewer factory.
"""
