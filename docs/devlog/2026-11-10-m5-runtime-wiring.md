# 2026-11-10 — M5-3: the agent gate in the paper runtime (`agent_gated` book)

Branch `task/m5-runtime-wiring` (from `task/mvp-integration` at 9c681ca). Written by the AI agents
engineer acting for the Lead on this task, which allowed additive contract types.

**Status: implemented and tested with the mock client only. M5 is not accepted.** The real
Anthropic adapter is still the stub from M5-AGENTS-MOCK, so in production every review is
`AI_UNAVAILABLE` and `agent_gated` takes nothing. Cost and latency on live signals are not
measured. No frontend work (Agent tab, cost page) was done. Nothing here is evidence of agent
value.

## What was built

| Part | Where | What it does |
|---|---|---|
| Contract types (additive) | `contracts/events.py` | `EventType.AGENT_REVIEW_REQUESTED`, `AGENT_REVIEW_COMPLETED`, `AGENT_GATED_ADMISSION`; `AggregateType.AGENT` (aggregate id = signal id). No existing member, field or serialization changed. |
| Deterministic state | `agents/wiring.py` (`AgentGatedState`) | Lives inside `PaperSession` when `PaperConfig.agent_gated` is set. It turns baseline submissions into review requests, validates and applies journaled outcomes, and turns approved outcomes into `agent_gated` entry requests. It never calls a client. |
| Session and replay | `sim/paper.py` | The fourth book `agent_gated` (the baseline's risk hook, `BookConfig` and kill switch). The input kind `agent_review` (`PaperSession.agent_review`). `replay` re-applies recorded outcomes. The service methods `_agent_launch` / `_agent_collect` and `agent_status`. |
| Live reviews | `agents/worker.py` (`ReviewWorker`) | `AgentGate.review` runs on one worker thread, off the asyncio loop. Finished outcomes are taken in submission order without blocking. Packet inputs come from closed 1m bars (`packet_inputs_from_bars`, `bar_reader_inputs`). `reviewer_factory` builds one gate per runtime and one worker per monitoring run. `agent_gated_enabled` covers the config side. |
| Runtime | `runtime/paper_runtime.py` | `build_paper_config` sets `agent_gated` only for a valid `agents.yaml` in mode `confirmed`. `PaperRuntime` builds the reviewer at open, closes it at stop, and publishes `agents` (coverage) for an agent_gated session. `load_paper_runtime` wires the production factory, whose client is the stub. |
| API | `agents/status.py`, `api/agents.py`, `api/app.py`, `api/paper.py` | `/api/agents/status` now says `runtime_wired: true` and adds `runtime`, the per-session counts with their own label: `SYSTEM` with the publication time, or `UNAVAILABLE` with the reason. `/api/paper/status` adds `agents` (SYSTEM). `/api/paper/books` lists the `agent_gated` book with the existing SIMULATED/STALE labels. |

## Design decisions

1. **Off means absent.** `PaperConfig.agent_gated` defaults to False. In that case no book, no
   state, no event, no `describe()` key and no reviewer exist. A reviewer passed to an off session
   is dropped. The only code that runs in mode off is a few `is None` checks. Proven against
   golden values pinned from 9c681ca (see the table below).
2. **What is reviewed.** Every signal whose baseline order was accepted at submit
   (`sim_order_submitted` in `baseline`), i.e. it passed the deterministic risk check at submit.
   Risk-vetoed signals are never reviewed, so agents cannot approve a risk-blocked signal. The
   request is journaled in the same batch as the baseline submission.
3. **Baseline untouched (L-12).** The baseline acts in the bar step, before any review exists.
   No agent output reaches the baseline, shadow or benchmark book or the signal lifecycle. The
   verdict's advisory status (VETOED for `VETOED_AGENT`) is recorded only in the
   `AGENT_REVIEW_COMPLETED` event. Tested for 11 outcome kinds: the three books' ledger hashes
   are equal to the 9c681ca golden values.
4. **Admission rule.** `agent_gated` admits a signal only if the state is `REVIEWED` and the
   verdict is `APPROVED` (`synthesis.book_admission`, unchanged). **Downgrade semantics:**
   `DOWNGRADED` blocks admission to `agent_gated` and does nothing else. It cannot resize,
   reprice, re-time or reorder anything, and the baseline takes the signal as always.
   `VETOED_AGENT` blocks in the same way. `AI_UNAVAILABLE`, `BUDGET_EXHAUSTED`,
   `AGENT_OUTPUT_INVALID` and `NOT_REQUESTED` keep the signal out and are counted under
   `coverage.by_state`.
5. **Same request, own book.** The admitted request is `EntryRequest.from_signal(record,
   books=("agent_gated",))`: the baseline's entry rule, stop, take-profit and validity. The
   book sizes it from its own equity with the baseline's `BookConfig`. Agents never set prices
   or sizes (tested: same stop/TP/validity/type/price, same qty on equal equity, and no
   price/size key in any agent payload).
6. **Latency mapping (no look-ahead).** A finished outcome becomes available at the poll that
   journals it (`available_at_ms` = that poll's `now_ms`). It is applied at the **first observed
   bar step of the signal's own instrument whose release time is ≥ `available_at_ms`**. The
   agent_gated risk check at submit then sees that instrument's bar of the minute, as the
   baseline's did, and is not blocked as stale because another symbol's bar came first. That
   step's decision time is its bar close, and its earliest execution is `max(next 1m open after the close + decision latency,
   first 1m open ≥ release)`. So execution is never earlier than the first 1m open after the
   review result existed. If the signal's validity ends first, the book rejects it
   (`validity_ends_before_execution`, counted). Unobserved (downtime) steps never admit. With
   the live poll schedule, baseline enters at the open of T+2 minutes and `agent_gated` at T+3
   at the earliest (tested).
7. **Journal before apply.** Each outcome is its own batch: input entry `kind: agent_review` with
   the outcome and `available_at_ms`, plus the `AGENT_REVIEW_COMPLETED` event. No book changes in
   that batch. The admission happens in a later bar batch (`AGENT_GATED_ADMISSION` +
   `sim_order_submitted` in `agent_gated`).
8. **Order and replay (mirrors `RecordedViews`).** Outcomes must arrive in request order. A
   recorded outcome that is for another signal, out of order, without a request, malformed
   (keys, a verdict on a failure state, PENDING, `risk_passed` false) or in a session without
   the book raises `AgentReplayMismatch`. `replay` turns that into
   `RestoreError("… agent review replay mismatch …")`. A well-formed but altered outcome (a
   flipped verdict) is caught by the batch comparison (`diverged`). Replay never builds a
   reviewer or client.
9. **Never blocking.** The bar loop only submits to the worker and polls `Future.done()`. The
   gate keeps its per-call deadline, single retry, breaker and budget. The worker queue is
   bounded (32 running); a full or closed worker gives `AI_UNAVAILABLE`.
10. **Fail closed, books unaffected.** A gate exception, a client crash, timeout, API down,
    invalid output, a budget refusal or a corrupt ledger all give no admission and the service
    keeps `RUNNING`. A reviewer that raises, returns an unexpected or unknown outcome, or a
    malformed one sets `agent_fault`. No further reviews are journaled or launched in that run,
    so `agent_gated` takes nothing more, and the books continue. A failed journal append of an
    outcome faults the service like any other batch (the in-memory state is no longer
    durable). This is the existing rule, kept deliberately.
11. **Restart with a review in flight.** A restored session's unanswered requests are journaled
    at the first poll as `AI_UNAVAILABLE` "review interrupted: …". They are never re-requested
    and never approved late. Approved admissions already journaled survive the restart and apply
    at the first observed bar after it, normally rejected for validity.
12. **Mode changes need a new session.** `describe()` carries `agent_gated` (rules version
    `agent-gated-v1`), so switching the agents mode of an existing session makes its restore
    fail (`UNAVAILABLE`), as a `risk.yaml` change does. This keeps the A/B fair: both books
    start together with the same equity. Mode `research` never gates paper, because paper
    signals are LIVE.
13. **Packet inputs.** The inputs are closed 1m bars of the instrument with `close_time ≤ decision
    time`, derived to the signal timeframe with the strategy's own aggregator (complete buckets
    only), the last 48 of them. ATR is the simple mean of the last 14 true ranges and is used for
    normalisation only. Features come from the signal's `market_conditions` and pass through the
    allowlist. `data_health` and `risk_state` are left empty (not wired). Reading happens inside
    the worker thread (the `LiveBarStore` is lock-protected).

## Invariants → tests

All in `tests/unit/agents/test_agents_paper_wiring.py` (P) and `test_agents_runtime_wiring.py` (R).

| Invariant | Tests |
|---|---|
| 1. mode off byte-identical | P `test_off_mode_journal_bytes_ledgers_and_fingerprint_equal_9c681ca` (journal digest, 39 batches, ledger hashes, fingerprint, config digest pinned from 9c681ca), `test_off_mode_ignores_a_reviewer_and_never_calls_the_client`, `test_off_mode_event_store_chain_head_equals_9c681ca` (hash-chain head `123/6d3aa61f…`); R `test_paper_config_gets_agent_gated_only_in_mode_confirmed[off/research/invalid/missing]`, `test_repo_default_config_builds_the_agent_free_session`, `test_production_factory_is_off_by_default`. The existing paper, replay, restart and session-evidence suites pass unchanged (full run below). |
| 2. confirmed: baseline unaffected, admission rule | P `test_baseline_is_byte_identical_to_off_for_every_outcome[approve, downgrade, veto, timeout, api_down, crash, invalid, budget, corrupt_ledger, gate_exception, no_client]`, `test_approved_entry_mirrors_the_baseline_request_and_waits_for_the_outcome`, `test_entry_kinds_other_than_market_are_mirrored`, `test_agent_gated_uses_the_baseline_risk_engine_at_submit_and_fill` |
| 3. no look-ahead, no blocking | P `test_approved_entry_mirrors_…` (request in the baseline batch, outcome batch before admission, earliest exec ≥ first open after `available_at_ms`), `test_admission_waits_for_a_bar_of_the_signals_own_instrument`, `test_an_outcome_is_applied_only_at_a_release_after_it_was_available`, `test_signal_that_expires_before_the_outcome_is_not_taken`, `test_a_slow_review_never_delays_the_bar_loop` (1 s mock call, each poll < 0.5 s, baseline filled meanwhile), `test_gate_deadline_turns_a_hung_client_into_ai_unavailable`; R `test_packet_inputs_use_only_bars_closed_by_the_decision`, `test_packet_inputs_derive_strategy_bars_from_complete_buckets` |
| 4. journal before apply, replay | P every parametrized case replays to identical hashes for all four books and the fingerprint with zero client calls; `test_replay_mismatch_wrong_signal_fails_closed`, `…_out_of_order_…`, `test_replay_with_a_missing_outcome_fails_closed`, `test_replay_with_a_malformed_outcome_fails_closed[5]`, `test_replay_with_a_flipped_verdict_diverges`, `test_confirmed_journal_does_not_replay_as_an_off_session`, `test_review_batch_in_an_off_session_fails_closed`, `test_event_store_session_restores_and_passes_verify_replay` (EventStore restore + `verify_recorded_session` PASS) |
| 5. fail closed everywhere | P the failure cases of the parametrized test, `test_a_broken_reviewer_stops_agent_gated_only[raises, malformed, unknown]`, `test_without_a_reviewer_every_review_is_unavailable`, `test_journal_failure_on_an_outcome_faults_like_any_batch`, `test_restart_with_a_review_in_flight_marks_it_unavailable`, `test_invalidation_skips_a_pending_admission_and_exits_an_admitted_one`; R `test_a_failing_reviewer_factory_leaves_paper_running`, `test_worker_refuses_when_full_or_closed_without_raising` |
| 6. provenance | R `test_status_counts_are_labelled`, `test_agent_status_reports_wired_and_the_session_counts`, `test_runtime_open_builds_a_reviewer_publishes_counts_and_closes_it`, `test_runtime_without_agent_gated_session_never_calls_the_factory`; P `test_status_exposes_coverage_only_for_an_agent_gated_session`. `tests/integration/test_agents_status_api.py` was updated (`runtime_wired` true, `runtime` labelled) but is skipped here (no fastapi). |
| 7. secrets | R `test_production_stub_client_is_unavailable_and_the_key_never_appears` (a fake key in an injected env: absent from the journal, logs, status and agent files; `.env` path injected, never the repo's). Every other test uses `MockClient`. |

## Evidence (this container: Python 3.13, no network; fastapi, httpx and pytest-asyncio not installed)

- New tests: `tests/unit/agents/test_agents_paper_wiring.py` (42) and
  `test_agents_runtime_wiring.py` (16). Both run on 91e2fa9: **58 passed**. `tests/unit/agents`
  overall: **215 passed**.
- Full unit suite, `PYTHONPATH=src:tests/unit/agents:tests/unit/backtest python3 -m pytest
  tests/unit -q -p no:cacheprovider`:
  - baseline at 9c681ca, before any edit: **1901 passed, 16 failed, 2 skipped**;
  - fe3dec3 (both feature commits): **1957 passed, 16 failed, 2 skipped**. The 16 failures are
    identical to the baseline and all environmental: 11 in `tests/unit/marketdata/test_subscription_nowait.py`
    (pytest-asyncio) and 5 in `tests/unit/test_o4_zero_trade_capture.py` (httpx). No other
    failure;
  - the two later test-only commits (9d9e888, 91e2fa9) were verified with the agents subset
    above. The final full run on the docs commit is reported in the task hand-off.
- Commit cf8cfe9 tested alone (a `git archive` copy outside the repo): agents + paper session +
  restart + session-evidence suites, 234 passed.
- `ruff check src tests` clean; `ruff format --check src tests` clean; `mypy --strict` on every
  changed source and test file shows only the pre-existing missing-stub noise (yaml in
  `agents/config.py` and `quality/interruptions.py`; fastapi untyped decorators in `api/`).
- Mutation checks, applied in a scratch copy and re-run against the two new test files:

| Mutant | Result |
|---|---|
| DOWNGRADED admitted to agent_gated | killed |
| admission ignores the instrument | killed |
| outcome order not checked | killed |
| invalidation before admission not skipped | killed |
| restored in-flight reviews not marked interrupted | killed |
| invalidation does not exit agent_gated | killed |
| a verdict on a failure state accepted | killed |
| admission ignores `available_at_ms` | survived the first set (redundant in live polling); killed by `test_an_outcome_is_applied_only_at_a_release_after_it_was_available` |
| agent_gated built with `allow_all` instead of the baseline risk hook | survived the first set; killed by `test_agent_gated_uses_the_baseline_risk_engine_at_submit_and_fill` |

Measured sizes (chars/4 estimate, no tokenizer offline), synthetic 1m paper signal:

| Item | Chars | ≈ tokens |
|---|---|---|
| packet, 2 bars (first minutes) | 481 | ~121 |
| packet, 48 bars (`max_bars`) | 1,540 | ~385 |
| analyst call input (prompt + 48-bar packet) | 3,967 | ~992 |
| `AGENT_REVIEW_COMPLETED` payload | 677 | — |
| one `agent_review` journal batch (2 entries) | 1,496 | — |

## What is NOT done

- The real Anthropic adapter (the stub is unchanged): no live review, no measured cost or latency.
- The dashboard Agent tab and cost page (frontend skipped: pnpm cannot install offline here).
- `data_health` / `risk_state` in the packet from the runtime (empty; allowlisted fields exist).
- An `agent_review_id` / `agent_review_state` on the signal projection or `/api/signals`. The
  review is visible in the journal (`AGENT_REVIEW_COMPLETED`, aggregate `agent`) and in the
  counts only. `SignalLifecycle` is unchanged.
- `EventType.AGENT_COST_RECORDED`: the cost ledger stays the JSONL file. Each outcome cites it
  (`cost_ledger`, `signal_id`, `review_key`), and every ledger entry carries those keys.
- The A/B evaluation of `agent_gated` vs `baseline` (SIGNALS_AND_AGENTS §6), and the M6 scorer's
  handling of the new event types (it skips non-bar steps and reads books from
  `sim_run_started`, but no test covers an agent_gated export).
- The fastapi-based API test (`tests/integration/test_agents_status_api.py`) was not run here.

## Open questions for the owner

1. Is "a mode change needs a new paper session" acceptable? The alternative is an agent_gated
   book that starts mid-session with fresh equity, which weakens the A/B comparison.
2. Restart policy: reviews in flight at a restart are recorded `AI_UNAVAILABLE`, never
   re-requested. Re-requesting could recover some (the cache may hold a finished review), but it
   would spend budget on signals that have usually expired by then. Keep fail-closed?
3. Head-of-line ordering: one worker, outcomes in request order. A slow review delays the
   journaling of later outcomes (the gate serializes them anyway). With several confirmed signals
   in one minute, later ones may expire before review. Should coverage loss from this be
   reported separately?
4. Downgrade = block (same as veto for `agent_gated`). Should DOWNGRADED instead admit at a
   reduced priority later (for example, never when the book is at its position limit)? That
   would need a new pre-registered rule; not done.

## Requests for the Lead / follow-ups

- Contract additions made on the Lead's delegation (listed above); please review them as Lead.
- `docs/ROADMAP.md` M5 row and `docs/devlog/metrics.csv` updated with this task.

## Independent review (Fable, fresh context): CONDITIONAL PASS, 0 blocking

The reviewer reproduced the mode-off identity on a `git archive` of `9c681ca`: 39 batches; the
journal digest, fingerprint, config digest and three ledger hashes are all equal. It also confirmed
strictly additive contracts, L-12/ADR-0007, no look-ahead, and replay that fails closed.
20 of 21 mutants were killed. Focused suite: 759 passed.

| # | Finding | Status |
|---|---|---|
| 1 | `verify-replay` builds its config from the current `configs/`, which now includes `agents.yaml`. With mode `confirmed`, replays of the three off-mode real sessions (the D-A evidence) return INCONCLUSIVE ("configuration differs"), never a false PASS or FAIL; the same holds in reverse | **Operational rule:** run `verify-replay` of an off-mode session with `configs/agents.yaml` at `mode: "off"` (the repo default). A mode change needs a new paper session (decision 12). Recorded here and in DECISIONS_PENDING L-13 |
| 2 | The off-mode API differs: `/api/paper/status` gains `"agents": null`; `/api/agents/status` reports `runtime_wired: true`. The fastapi integration test was not run in this container | CLOSED: `tests/integration/test_agents_status_api.py` ran and passed in CI `42838323339` (7/7 on `97168b2`; the only skipped test is a jsonschema one) |
| 3 | After an `agent_fault`, `awaiting_review` grows without bound in the status | FIXED. The status shows the exact `awaiting_review_count` and at most `AWAITING_SHOWN` (20) ids; test added |
| 4 | Untested `released_at_ms is None` guard in `due()` | FIXED. Test added; the review's surviving mutant is now killed |
| 5 | The status endpoint reads the cost ledger without the lock while the worker appends | Accepted (minor). A torn last line gives a transient "ledger unreadable" in status only; spend decisions take the lock |
| 6 | `ReviewWorker.close()` does not wait for a running review | Accepted (minor). The gate deadline bounds it; the cost ledger stays consistent (append under lock) |
| 7 | `sim/paper.py` imports `agents.wiring` (a sim↔agents layering change) | Accepted by the Lead (L-13) |
| 8 | The journaled `reason` is the gate's text, clipped to 300 chars | Follow-up for the real adapter: sanitize provider exception text before it reaches the journal |

## Export format v2: agent sessions through the M6 export (2026-11-10, integration session)

**Defect found.** Before this change, no `agent_gated` session could be exported. The M6 exporter
refuses any unmapped event type inside a paper batch. `agent_review_requested` is journaled in the
baseline's submission batch, so every export failed with `ExportError: event
'agent_review_requested' at seq 14 inside a paper batch`. Reproduced by
`tests/unit/agents/test_agents_export.py` before the fix: 6 failed. It failed closed (no wrong
export was written), but it would have blocked M6 for every confirmed-mode session after a merge.

**Fix: export format v2.**
- New `agents.jsonl` file for `agent_review_requested`, `agent_review_completed` and
  `agent_gated_admission`, with `agents.schema.json`. The schema refuses
  `baseline_affected: true`, an admission to a book other than `agent_gated`, a provenance other
  than SIMULATED and an aggregate other than `agent`.
- `steps.schema.json` v2 accepts `agent_review` steps and requires their `review` and
  `available_at_ms`.
- `schemas/v1` is unchanged, and v1 exports are still checked and scored. The scorer and verifier
  pick the schema set and file list by the manifest's `format_version`.
- A v1 export compared with the store is refused: a fresh export would be v2. The structural
  checks still apply.
- The scorer is unchanged otherwise: it ignores record types it does not score and non-bar steps.
- Scorer version `tsc-eval-scorer-2`.

**Evidence.**
- Four outcome mixes (approve, downgrade, veto, mixed): each agent_gated session exports, verifies
  against its store, and scores. The scorer reproduces all four books' ledger hashes, agent_gated
  included. Every agent event appears in `agents.jsonl` once.
- Mode off: the books are unchanged and `agents.jsonl` is empty.
- The jsonschema reference validator agrees on every agent and step line.
- Backward compatibility, checked by hand:
  - The `9c681ca` exporter (v1) and this exporter (v2, rewritten to v1 by
    `test_eval_export._as_v1`) were run on the same store. The outputs are byte-identical.
  - The `9c681ca` v1 export checks, verifies and scores under the new scorer. It reproduces every
    ledger hash of both sessions, also with the standalone `scorer.py --check-only`.
- Tests: `tests/unit/agents/test_agents_export.py` (8); `tests/unit/evaluation` 3 new
  format-version tests; the schema tests now cover both versions.

**Mode-off identity, re-verified independently.** The paper scenario of
`tests/unit/sim/test_paper_session.run_live` was run with each tree's own code, on `9c681ca` and on
`127e445`. Both give the same 39 batches, journal digest `61c6163a…`, three ledger hashes and
fingerprint.

**D-06 question added:** agent events in the shared format (a separate file, advisory only), and
whether the other team's system needs an equivalent.
