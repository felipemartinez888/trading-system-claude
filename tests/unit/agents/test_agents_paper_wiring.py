"""M5-3: the advisory agent gate wired into paper trading (the ``agent_gated`` book).

Invariants (task M5-3, ADR-0007, L-12), each proven here with the `MockClient` only (no network,
no API key, no real client):

1. mode off: journal bytes, ledger hashes and fingerprint equal those of commit 9c681ca (pinned
   golden values), with or without a reviewer passed; no agent book, no agent event;
2. mode confirmed: the baseline, shadow and benchmark books are byte-identical to mode off for
   every review outcome; agent_gated admits only REVIEWED + APPROVED;
3. no look-ahead / no blocking: the request is journaled with the baseline submission, the
   outcome in a later batch, the agent_gated entry executes after the outcome existed; a slow
   review does not delay the bar loop;
4. replay re-applies journaled outcomes without any client, and fails closed on a missing,
   reordered or malformed outcome;
5. any failure (client, gate, ledger, reviewer, restart) keeps agent_gated out and the service
   running.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from agent_helpers import DECISION_MS
from paper_agent_helpers import (
    ETH,
    ScriptedStrategy,
    approve,
    config,
    downgrade,
    entries,
    gated_config,
    mock_client,
    new_service,
    record,
    run,
    sid,
    step_minute,
    two_symbol_bars,
    veto,
    worker,
)

from tsclaude.agents.client import MockClient, MockStep
from tsclaude.agents.gate import LEDGER_FILE
from tsclaude.agents.wiring import (
    AGENT_GATED,
    INTERRUPTED,
    OUTCOME_KEYS,
    REVIEW_KIND,
    unavailable_payload,
)
from tsclaude.contracts.events import EventType
from tsclaude.contracts.signals import EntryRuleKind, SignalRecord, SignalStatus
from tsclaude.contracts.timeutil import MS_PER_MINUTE
from tsclaude.runtime.session_evidence import (
    capture_session_end,
    config_digest,
    journal_digest,
    verify_recorded_session,
)
from tsclaude.sim.journal import Batch, JournalEntry, MemoryJournal
from tsclaude.sim.paper import (
    BASELINE,
    BENCHMARK,
    SHADOW,
    PaperTradingService,
    RestoreError,
    ServiceState,
    replay,
)
from tsclaude.sim.testing import minute
from tsclaude.store.eventstore import EventStore

# ------------------------------------------------------------------ pinned at commit 9c681ca
# `test_paper_session.run_live()` and the same scenario on the event store, computed with the code
# of 9c681ca (before M5-3) by running the scenario; any byte change in mode off breaks these.
GOLDEN_JOURNAL_DIGEST = "61c6163af305d9484c381ae867623c0d718c8978e020e8c1ade24dc51a7ff293"
GOLDEN_BATCHES = 39
GOLDEN_FINGERPRINT = "feec14f06bd2e7ae8344366bd8a461b15e59a0fb10528e3b4a58dc8429864108"
GOLDEN_CONFIG_DIGEST = "47a4c40cc5a3083e8553c1c20bf98085699c9be70d7826685e892427780ded19"
GOLDEN_LEDGERS = {
    BASELINE: "46725adff264a66fbf84334cdcf1afa52ebfc9488202bd397c7fda0b4ba0d70d",
    SHADOW: "7195967d903437ddd34f2ecf576152cba2c0ace55dbec109ec1f1e1d2b6f61da",
    BENCHMARK: "fc0652f86f266b154fa038a1ae9fd2206d75bcd449489a060d9f6f6fe80ea608",
}
GOLDEN_STORE_HEAD = (123, "6d3aa61fd0e5cf5e72612ba15ffa54f2fee540596ebdea01e5efdc61da054017")

# the scenario of tests/unit/sim/test_paper_session.py (imported, not copied)
import test_paper_session as ps  # noqa: E402

AGENT_TYPES = {
    str(EventType.AGENT_REVIEW_REQUESTED),
    str(EventType.AGENT_REVIEW_COMPLETED),
    str(EventType.AGENT_GATED_ADMISSION),
}
#: signals of the scenario the baseline accepts at submit (sid 3 is risk-vetoed, sid 4 stays WATCH)
REVIEWED = [sid(1), sid(2), sid(5)]
MISSING = frozenset({(13, "ETHUSDT")})


def scenario_bars() -> Any:
    return two_symbol_bars(ps.N, ps.BTC_ROWS, ps.ETH_ROWS)


def run_scenario(cfg: Any, reviewer: Any = None) -> tuple[PaperTradingService, MemoryJournal]:
    svc, journal = new_service(cfg, ps.scenario(), reviewer)
    run(svc, scenario_bars(), list(range(ps.N)), wait=reviewer, missing=MISSING)
    return svc, journal


def lifecycle_view(svc: PaperTradingService) -> dict[str, Any]:
    return {
        s.signal_id: (str(s.status), s.reason, s.outcome, s.shadow_outcome)
        for s in svc.session.lifecycle.signals()
    }


def canon(batches: list[Batch]) -> list[str]:
    return [e.canonical() for b in batches for e in b]


# ------------------------------------------------------------------ 1. mode off
def test_off_mode_journal_bytes_ledgers_and_fingerprint_equal_9c681ca() -> None:
    svc, journal, _ = ps.run_live()
    batches = journal.batches()
    assert len(batches) == GOLDEN_BATCHES
    assert journal_digest(batches) == GOLDEN_JOURNAL_DIGEST
    assert svc.ledger_hashes() == GOLDEN_LEDGERS
    assert svc.session.fingerprint() == GOLDEN_FINGERPRINT
    assert config_digest(config()) == GOLDEN_CONFIG_DIGEST
    assert config().describe()["books"] == [BASELINE, SHADOW, BENCHMARK]
    assert "agent_gated" not in config().describe()
    assert not {e.type for b in batches for e in b} & AGENT_TYPES
    assert svc.session.agents is None
    assert "agents" not in svc.status()
    assert svc.agent_status() is None


def test_off_mode_ignores_a_reviewer_and_never_calls_the_client() -> None:
    client = mock_client(approve() * 3)
    w = worker(client, scenario_bars())
    svc, journal = run_scenario(config(), w)
    assert journal_digest(journal.batches()) == GOLDEN_JOURNAL_DIGEST
    assert svc.ledger_hashes() == GOLDEN_LEDGERS
    assert svc.reviewer is None
    assert client.calls == []
    assert w.submitted == 0
    w.close()


def test_off_mode_event_store_chain_head_equals_9c681ca(tmp_path: Path) -> None:
    with EventStore(tmp_path / "state.db") as store:
        svc = PaperTradingService.open(
            config(), store, session_id="paper-g", strategy=ps.scenario()
        )
        run(svc, scenario_bars(), list(range(ps.N)), missing=MISSING)
        assert tuple(store.head) == GOLDEN_STORE_HEAD
        assert svc.ledger_hashes() == GOLDEN_LEDGERS


# ------------------------------------------------------------------ 2. mode confirmed
def _steps(kind: str) -> list[tuple[MockStep, MockStep]]:
    """Three reviews (one per accepted signal) of the given kind."""
    if kind == "approve":
        return approve() * 3
    if kind == "downgrade":
        return downgrade() * 3
    if kind == "veto":
        return veto() * 3
    if kind == "invalid":
        return [(MockStep.invalid_json(), MockStep.invalid_json())] * 3
    return []


EXPECTED = {
    "approve": ("REVIEWED", "APPROVED"),
    "downgrade": ("REVIEWED", "DOWNGRADED"),
    "veto": ("REVIEWED", "VETOED_AGENT"),
    "timeout": ("AI_UNAVAILABLE", None),
    "api_down": ("AI_UNAVAILABLE", None),
    "crash": ("AI_UNAVAILABLE", None),
    "invalid": ("AGENT_OUTPUT_INVALID", None),
    "budget": ("BUDGET_EXHAUSTED", None),
    "corrupt_ledger": ("BUDGET_EXHAUSTED", None),
    "gate_exception": ("AI_UNAVAILABLE", None),
    "no_client": ("AI_UNAVAILABLE", None),
}


def _reviewer_for(kind: str, tmp_path: Path) -> tuple[Any, MockClient | None]:
    bars = scenario_bars()
    if kind == "timeout":  # the gate's deadline, then its one retry, both time out
        client = MockClient.scripted(analyst=[MockStep.timeout()] * 6)
        return worker(client, bars), client
    if kind == "api_down":
        client = MockClient.scripted(analyst=[MockStep.api_down()] * 6)
        return worker(client, bars), client
    if kind == "crash":
        client = MockClient.scripted(analyst=[MockStep.crash()] * 3)
        return worker(client, bars), client
    if kind == "budget":
        client = mock_client(approve() * 3)
        return worker(client, bars, daily_cap_usd=Decimal("0.00000001")), client
    if kind == "corrupt_ledger":
        (tmp_path / LEDGER_FILE).write_text('{"torn": \n', encoding="utf-8")
        client = mock_client(approve() * 3)
        return worker(client, bars, data_dir=tmp_path), client
    if kind == "gate_exception":
        client = mock_client(approve() * 3)

        def broken(signal: SignalRecord) -> Any:
            raise RuntimeError("inputs exploded")

        return worker(client, bars, inputs=broken), client
    if kind == "no_client":
        return worker(None, bars), None
    client = mock_client(_steps(kind))
    return worker(client, bars), client


@pytest.mark.parametrize("kind", sorted(EXPECTED))
def test_baseline_is_byte_identical_to_off_for_every_outcome(kind: str, tmp_path: Path) -> None:
    off, _ = run_scenario(config())
    reviewer, client = _reviewer_for(kind, tmp_path)
    svc, journal = run_scenario(gated_config(), reviewer)
    reviewer.close()
    assert svc.state is ServiceState.RUNNING, svc.fault
    assert svc.agent_fault is None
    hashes = svc.ledger_hashes()
    # L-12: agents never change the baseline (nor shadow, nor benchmark)
    assert {k: hashes[k] for k in GOLDEN_LEDGERS} == GOLDEN_LEDGERS
    assert lifecycle_view(svc) == lifecycle_view(off)

    batches = journal.batches()
    done = entries(batches, str(EventType.AGENT_REVIEW_COMPLETED))
    assert [e.payload["signal_id"] for _, e in done] == REVIEWED
    state, verdict = EXPECTED[kind]
    assert {(e.payload["state"], e.payload["verdict"]) for _, e in done} == {(state, verdict)}
    admitted = verdict == "APPROVED"
    assert all(e.payload["agent_gated_admitted"] is admitted for _, e in done)
    assert all(e.payload["baseline_affected"] is False for _, e in done)
    advisory = "VETOED" if verdict == "VETOED_AGENT" else "CONFIRMED"
    assert {e.payload["advisory_status"] for _, e in done} == {advisory}
    gated = entries(batches, "sim_order_submitted", book=AGENT_GATED)
    assert len(gated) == (3 if admitted else 0)
    cov = svc.session.agents.coverage()  # type: ignore[union-attr]
    assert cov["requested"] == cov["completed"] == 3
    assert cov["by_state"] == {state: 3}
    assert cov["admitted"] == (3 if admitted else 0)

    # replay: recorded outcomes re-applied, every book identical, no client call
    calls = len(client.calls) if client is not None else 0
    again = replay(gated_config(), batches)
    assert again.ledger_hashes() == hashes
    assert again.fingerprint() == svc.session.fingerprint()
    assert (len(client.calls) if client is not None else 0) == calls


def test_approved_entry_mirrors_the_baseline_request_and_waits_for_the_outcome() -> None:
    bars = two_symbol_bars(12)
    strat = ScriptedStrategy({(minute(1), "BTCUSDT"): [record(1, 1)]})
    w = worker(mock_client(approve()), bars)
    svc, journal = new_service(gated_config(), strat, w)
    run(svc, bars, list(range(12)), wait=w)
    w.close()
    batches = journal.batches()

    [(req_i, req)] = entries(batches, str(EventType.AGENT_REVIEW_REQUESTED))
    [(base_i, base)] = entries(
        batches, "sim_order_submitted", book=BASELINE, order_id=f"{BASELINE}:{sid(1)}:entry"
    )
    assert req_i == base_i  # requested in the very batch of the baseline submission
    assert req.payload["requested_at_ms"] == base.payload["decision_time_ms"]
    [(done_i, done)] = entries(batches, str(EventType.AGENT_REVIEW_COMPLETED))
    assert batches[done_i][0].payload["kind"] == REVIEW_KIND
    assert len(batches[done_i]) == 2  # input entry + the outcome event; no book changed
    assert set(batches[done_i][0].payload["review"]) == OUTCOME_KEYS
    [(adm_i, adm)] = entries(batches, str(EventType.AGENT_GATED_ADMISSION))
    [(sub_i, sub)] = entries(batches, "sim_order_submitted", book=AGENT_GATED)
    assert req_i < done_i < adm_i == sub_i
    assert batches[adm_i][0].payload["bar"]["symbol"] == "BTCUSDT"
    available = done.payload["available_at_ms"]
    assert adm.payload["available_at_ms"] == available
    assert adm.payload["released_at_ms"] >= available
    # execution no earlier than the first 1m open after the outcome existed
    first_open_after = -(-available // MS_PER_MINUTE) * MS_PER_MINUTE
    assert sub.payload["earliest_exec_ms"] >= first_open_after
    assert sub.payload["earliest_exec_ms"] > base.payload["earliest_exec_ms"]
    # the same request: agents set no price, stop, take-profit, size or validity
    for key in ("stop_price", "take_profit_price", "valid_until_ms", "type", "price", "qty"):
        assert sub.payload[key] == base.payload[key], key
    req_b, req_g = dict(base.payload["request"]), dict(sub.payload["request"])
    assert req_b.pop("books") == [BASELINE, SHADOW]
    assert req_g.pop("books") == [AGENT_GATED]
    assert req_b == req_g
    fills = {
        e.payload["book"]: e.payload
        for _, e in entries(batches, "sim_order_filled", role="entry")
        if e.payload["book"] in (BASELINE, AGENT_GATED)
    }
    assert fills[AGENT_GATED]["qty"] == fills[BASELINE]["qty"]  # sized by its own book alike
    assert fills[AGENT_GATED]["bar_open_ms"] >= first_open_after
    forbidden = {"price", "qty", "size", "stop_price", "take_profit_price", "entry_price"}
    assert not forbidden & set(done.payload)


def test_admission_waits_for_a_bar_of_the_signals_own_instrument() -> None:
    # clock order puts BTCUSDT before ETHUSDT in each minute: an ETH signal's admission must not
    # happen at the BTC bar (its risk view would lack the ETH bar of that minute)
    bars = two_symbol_bars(10)
    strat = ScriptedStrategy({(minute(1), "ETHUSDT"): [record(1, 1, symbol="ETHUSDT")]})
    w = worker(mock_client(approve()), bars)
    svc, journal = new_service(gated_config(), strat, w)
    run(svc, bars, list(range(10)), wait=w)
    w.close()
    batches = journal.batches()
    [(done_i, _)] = entries(batches, str(EventType.AGENT_REVIEW_COMPLETED))
    [(adm_i, adm)] = entries(batches, str(EventType.AGENT_GATED_ADMISSION))
    assert adm.payload["action"] == "submitted"
    assert batches[done_i + 1][0].payload["bar"]["symbol"] == "BTCUSDT"  # not admitted there
    assert adm_i == done_i + 2
    assert batches[adm_i][0].payload["bar"]["symbol"] == "ETHUSDT"
    [(_, sub)] = entries(batches, "sim_order_submitted", book=AGENT_GATED)
    assert sub.payload["symbol"] == "ETHUSDT"
    assert replay(gated_config(), batches).ledger_hashes() == svc.ledger_hashes()


def test_signal_that_expires_before_the_outcome_is_not_taken() -> None:
    # valid through minute 3: the baseline enters at the minute-3 open, agent_gated could only
    # execute at the minute-4 open (after the outcome existed) -> rejected by its book
    bars = two_symbol_bars(10)
    strat = ScriptedStrategy({(minute(1), "BTCUSDT"): [record(1, 1, valid_bars=2)]})
    w = worker(mock_client(approve()), bars)
    svc, journal = new_service(gated_config(), strat, w)
    run(svc, bars, list(range(10)), wait=w)
    w.close()
    batches = journal.batches()
    assert entries(batches, "sim_order_filled", book=BASELINE, role="entry")
    [(_, rej)] = entries(batches, "sim_request_rejected", book=AGENT_GATED)
    assert rej.payload["reason"] == "validity_ends_before_execution"
    assert not entries(batches, "sim_order_filled", book=AGENT_GATED)
    cov = svc.session.agents.coverage()  # type: ignore[union-attr]
    assert cov["book_rejections"] == {"validity_ends_before_execution": 1}
    assert replay(gated_config(), batches).ledger_hashes() == svc.ledger_hashes()


class HeldReviewer:
    """Runs the real gate (MockClient) synchronously at submit, but releases an outcome only when
    the test says so: deterministic control of when a review result becomes available."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.results: dict[str, dict[str, Any]] = {}
        self.order: list[str] = []
        self.released: set[str] = set()
        self.closed = False

    def submit(self, signal: SignalRecord) -> None:
        self.order.append(signal.signal_id)
        self.results[signal.signal_id] = self.inner._review(signal)

    def release(self, *ids: str) -> None:
        self.released.update(ids)

    def completed(self) -> list[tuple[str, dict[str, Any]]]:
        out = []
        while self.order and self.order[0] in self.released:
            s = self.order.pop(0)
            out.append((s, self.results[s]))
        return out

    def snapshot(self) -> dict[str, Any]:
        return {"held": len(self.order)}

    def close(self) -> None:
        self.closed = True


def test_invalidation_skips_a_pending_admission_and_exits_an_admitted_one() -> None:
    m = minute
    bars = two_symbol_bars(12)
    strat = ScriptedStrategy(
        {
            (m(1), "BTCUSDT"): [record(1, 1, valid_bars=8)],
            (m(1), "ETHUSDT"): [record(2, 1, symbol="ETHUSDT", valid_bars=8)],
            (m(3), "BTCUSDT"): [ps.Invalidate(sid(1))],  # outcome still held: skipped later
            (m(6), "ETHUSDT"): [ps.Invalidate(sid(2))],  # agent_gated holds it: exits
        }
    )
    held = HeldReviewer(worker(mock_client(approve() * 2), bars))
    svc, journal = new_service(gated_config(), strat, held)
    for i in range(12):
        if i == 2:
            held.release(sid(2))  # ETH was requested first (its bar comes first): released
        if i == 4:
            held.release(sid(1))  # after the BTC invalidation at minute 3
        step_minute(svc, bars, i)
    batches = journal.batches()
    adm = {e.payload["signal_id"]: e.payload for _, e in entries(batches, "agent_gated_admission")}
    assert adm[sid(1)]["action"] == "skipped"
    assert adm[sid(1)]["reason"] == "invalidated_before_admission"
    assert adm[sid(2)]["action"] == "submitted"
    [(_, exit_req)] = entries(batches, "sim_exit_requested", book=AGENT_GATED)
    assert exit_req.payload["request_id"] == f"invalidate:{sid(2)}"
    closed = entries(batches, "sim_position_closed", book=AGENT_GATED)
    assert [e.payload["request_id"] for _, e in closed] == [sid(2)]
    assert replay(gated_config(), batches).ledger_hashes() == svc.ledger_hashes()


# ------------------------------------------------------------------ 3. replay fails closed
def _with(batches: list[Batch], i: int, review: dict[str, Any]) -> list[Batch]:
    out = [list(b) for b in batches]
    head = out[i][0]
    out[i][0] = dataclasses.replace(head, payload={**head.payload, "review": review})
    return out


def _review_batches(batches: list[Batch]) -> list[int]:
    return [i for i, b in enumerate(batches) if b[0].payload["kind"] == REVIEW_KIND]


@pytest.fixture(scope="module")
def approved_journal() -> list[Batch]:
    w = worker(mock_client(approve() * 3), scenario_bars())
    _, journal = run_scenario(gated_config(), w)
    w.close()
    return journal.batches()


def test_replay_mismatch_wrong_signal_fails_closed(approved_journal: list[Batch]) -> None:
    i = _review_batches(approved_journal)[0]
    review = copy.deepcopy(approved_journal[i][0].payload["review"])
    review["signal_id"] = sid(9)
    with pytest.raises(RestoreError, match="agent review replay mismatch"):
        replay(gated_config(), _with(approved_journal, i, review))


def test_replay_mismatch_out_of_order_fails_closed(approved_journal: list[Batch]) -> None:
    a, b = _review_batches(approved_journal)[:2]
    swapped = [list(x) for x in approved_journal]
    swapped[a], swapped[b] = swapped[b], swapped[a]
    with pytest.raises(RestoreError, match="agent review replay mismatch"):
        replay(gated_config(), swapped)


def test_replay_with_a_missing_outcome_fails_closed(approved_journal: list[Batch]) -> None:
    i = _review_batches(approved_journal)[0]
    dropped = [b for j, b in enumerate(approved_journal) if j != i]
    with pytest.raises(RestoreError):
        replay(gated_config(), dropped)


@pytest.mark.parametrize(
    "edit",
    ["verdict_on_failure", "extra_key", "missing_key", "pending", "not_risk_passed"],
)
def test_replay_with_a_malformed_outcome_fails_closed(
    approved_journal: list[Batch], edit: str
) -> None:
    i = _review_batches(approved_journal)[0]
    review = copy.deepcopy(approved_journal[i][0].payload["review"])
    if edit == "verdict_on_failure":
        review["state"] = "AI_UNAVAILABLE"
    elif edit == "extra_key":
        review["size"] = "2"
    elif edit == "missing_key":
        del review["verdict"]
    elif edit == "pending":
        review.update(state="PENDING", verdict=None)
    else:
        review["risk_passed"] = False
    with pytest.raises(RestoreError, match="agent review replay mismatch"):
        replay(gated_config(), _with(approved_journal, i, review))


def test_replay_with_a_flipped_verdict_diverges(approved_journal: list[Batch]) -> None:
    i = _review_batches(approved_journal)[0]
    review = copy.deepcopy(approved_journal[i][0].payload["review"])
    review["verdict"] = "VETOED_AGENT"  # a well-formed outcome, but not the recorded one
    with pytest.raises(RestoreError, match="diverged"):
        replay(gated_config(), _with(approved_journal, i, review))


def test_confirmed_journal_does_not_replay_as_an_off_session(
    approved_journal: list[Batch],
) -> None:
    with pytest.raises(RestoreError):
        replay(config(), approved_journal)


def test_review_batch_in_an_off_session_fails_closed() -> None:
    _, journal, _ = ps.run_live()
    batches = journal.batches()
    head = JournalEntry(
        "paper_step",
        "sim",
        batches[0][0].aggregate_id,
        batches[-1][0].ts_ms,
        {"kind": REVIEW_KIND, "review": unavailable_payload(sid(1), "x"), "available_at_ms": 1},
    )
    with pytest.raises(RestoreError, match="agent review replay mismatch"):
        replay(config(), [*batches, [head]])


# ------------------------------------------------------------------ 4. restart and event store
def test_restart_with_a_review_in_flight_marks_it_unavailable(tmp_path: Path) -> None:
    bars = two_symbol_bars(12)
    strat = ScriptedStrategy(
        {
            (minute(1), "BTCUSDT"): [record(1, 1, valid_bars=8)],
            (minute(5), "BTCUSDT"): [record(2, 5)],
        }
    )
    client = mock_client(approve() * 2)
    held = HeldReviewer(worker(client, bars))
    journal = MemoryJournal()
    svc = PaperTradingService(gated_config(), journal, strategy=strat, reviewer=held)
    for i in range(3):
        step_minute(svc, bars, i)
    assert svc.session.agents is not None
    assert svc.session.agents.awaiting() == [sid(1)]
    del svc  # the process dies with the review in flight (its outcome never journaled)

    w2 = worker(mock_client(approve()), bars)
    restored = PaperTradingService.restore(gated_config(), journal, strategy=strat, reviewer=w2)
    assert restored.state is ServiceState.RUNNING
    last = minute(2) + MS_PER_MINUTE - 1 + 1_500
    run(restored, bars, list(range(3, 12)), wait=w2, not_before=last)
    w2.close()
    batches = journal.batches()
    done = {
        e.payload["signal_id"]: e.payload for _, e in entries(batches, "agent_review_completed")
    }
    assert done[sid(1)]["state"] == "AI_UNAVAILABLE"
    assert done[sid(1)]["reason"] == INTERRUPTED
    assert done[sid(1)]["agent_gated_admitted"] is False
    assert done[sid(2)]["verdict"] == "APPROVED"  # a new request after the restart is reviewed
    gated = entries(batches, "sim_order_submitted", book=AGENT_GATED)
    assert [e.payload["request"]["request_id"] for _, e in gated] == [sid(2)]
    assert restored.agent_fault is None
    again = replay(gated_config(), batches)
    assert again.ledger_hashes() == restored.ledger_hashes()


def test_event_store_session_restores_and_passes_verify_replay(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    bars = scenario_bars()
    w = worker(mock_client(approve() * 3), bars)
    with EventStore(data / "state.db") as store:
        svc = PaperTradingService.open(
            gated_config(), store, session_id="paper-a", strategy=ps.scenario(), reviewer=w
        )
        run(svc, bars, list(range(ps.N)), wait=w, missing=MISSING)
        w.close()
        live_hashes = svc.ledger_hashes()
        assert set(live_hashes) == {BASELINE, SHADOW, BENCHMARK, AGENT_GATED}
        restored = PaperTradingService.open(gated_config(), store, session_id="paper-a")
        assert restored.ledger_hashes() == live_hashes
        capture_session_end(
            data,
            config=gated_config(),
            session_id="paper-a",
            ledger_hashes=live_hashes,
            fingerprint=svc.session.fingerprint(),
            batches=svc.journal.batches(),
            state="RUNNING",
            written_at_ms=DECISION_MS,
        )
    report = verify_recorded_session(data, gated_config())
    assert report.verdict == "PASS", report.reasons
    assert verify_recorded_session(data, config()).verdict == "INCONCLUSIVE"  # other config


# ------------------------------------------------------------------ 5. never blocks, fails closed
def test_a_slow_review_never_delays_the_bar_loop() -> None:
    bars = two_symbol_bars(8)
    strat = ScriptedStrategy({(minute(1), "BTCUSDT"): [record(1, 1)]})
    client = MockClient.scripted(
        analyst=[MockStep.slow(1.0, ps_analyst())], skeptic=[MockStep.valid(ps_skeptic())]
    )
    w = worker(client, bars, per_call_timeout_s=5.0)
    svc, journal = new_service(gated_config(), strat, w)
    durations = []
    for i in range(4):
        t0 = time.monotonic()
        step_minute(svc, bars, i)
        durations.append(time.monotonic() - t0)
    assert max(durations) < 0.5, durations  # the poll that launched the review did not wait
    assert w.in_flight == 1
    batches = journal.batches()
    assert entries(batches, "sim_order_submitted", book=BASELINE)
    assert entries(batches, "sim_order_filled", book=BASELINE, role="entry")  # baseline went on
    assert not entries(batches, "agent_review_completed")
    assert w.wait_idle(10.0)
    run(svc, bars, list(range(4, 8)))
    w.close()
    batches = journal.batches()
    [(_, done)] = entries(batches, "agent_review_completed")
    assert done.payload["verdict"] == "APPROVED"
    assert done.payload["latency_ms"] >= 900
    assert entries(batches, "sim_order_submitted", book=AGENT_GATED)


def ps_analyst() -> dict[str, Any]:
    from paper_agent_helpers import analyst

    return analyst()


def ps_skeptic() -> dict[str, Any]:
    from agent_helpers import skeptic_payload

    return skeptic_payload(("minor",))


def test_gate_deadline_turns_a_hung_client_into_ai_unavailable() -> None:
    bars = two_symbol_bars(6)
    strat = ScriptedStrategy({(minute(1), "BTCUSDT"): [record(1, 1)]})
    client = MockClient.scripted(analyst=[MockStep.slow(3.0, ps_analyst())] * 2)
    w = worker(client, bars, per_call_timeout_s=0.2)
    svc, journal = new_service(gated_config(), strat, w)
    run(svc, bars, list(range(6)), wait=w)
    w.close()
    [(_, done)] = entries(journal.batches(), "agent_review_completed")
    assert done.payload["state"] == "AI_UNAVAILABLE"
    assert "timed out" in done.payload["reason"]
    assert not entries(journal.batches(), "sim_order_submitted", book=AGENT_GATED)
    assert svc.state is ServiceState.RUNNING


class BadReviewer:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.ids: list[str] = []

    def submit(self, signal: SignalRecord) -> None:
        self.ids.append(signal.signal_id)

    def completed(self) -> list[tuple[str, dict[str, Any]]]:
        if self.mode == "raises":
            raise RuntimeError("reviewer broke")
        if not self.ids:
            return []
        s = self.ids.pop(0)
        payload = unavailable_payload(s, "x")
        if self.mode == "malformed":
            payload["verdict"] = "APPROVED"  # a verdict on a failure state
        else:  # "unknown": an outcome for a signal nobody asked about
            s = payload["signal_id"] = sid(42)
        return [(s, payload)]

    def snapshot(self) -> dict[str, Any]:
        return {}

    def close(self) -> None:
        pass


@pytest.mark.parametrize("mode", ["raises", "malformed", "unknown"])
def test_a_broken_reviewer_stops_agent_gated_only(mode: str) -> None:
    svc, journal = new_service(gated_config(), ps.scenario(), BadReviewer(mode))
    run(svc, scenario_bars(), list(range(ps.N)), missing=MISSING)
    assert svc.state is ServiceState.RUNNING, svc.fault
    assert svc.agent_fault is not None
    batches = journal.batches()
    assert not entries(batches, "agent_review_completed")
    assert not entries(batches, "sim_order_submitted", book=AGENT_GATED)
    hashes = svc.ledger_hashes()
    assert {k: hashes[k] for k in GOLDEN_LEDGERS} == GOLDEN_LEDGERS
    assert svc.status()["agents"]["fault"] == svc.agent_fault
    assert replay(gated_config(), batches).ledger_hashes() == hashes


def test_without_a_reviewer_every_review_is_unavailable() -> None:
    svc, journal = run_scenario(gated_config(), None)
    done = entries(journal.batches(), "agent_review_completed")
    assert [e.payload["state"] for _, e in done] == ["AI_UNAVAILABLE"] * 3
    assert {e.payload["reason"] for _, e in done} == {"no agent reviewer"}
    hashes = svc.ledger_hashes()
    assert {k: hashes[k] for k in GOLDEN_LEDGERS} == GOLDEN_LEDGERS


def test_journal_failure_on_an_outcome_faults_like_any_batch() -> None:
    def fail(_: int, batch: Any) -> bool:
        return bool(batch[0].payload.get("kind") == REVIEW_KIND)

    w = worker(mock_client(approve() * 3), scenario_bars())
    svc, journal = new_service(gated_config(), ps.scenario(), w, MemoryJournal(fail_when=fail))
    run(svc, scenario_bars(), list(range(4)), wait=w)
    w.close()
    assert svc.state is ServiceState.FAULTED  # in-memory state is no longer durable
    assert svc.fault is not None
    assert svc.fault.startswith("journal_failed")
    # what was durable restores exactly; the lost outcome's review is interrupted (fail closed)
    restored = PaperTradingService.restore(gated_config(), journal)
    assert restored.session.agents is not None
    assert restored.session.agents.awaiting() == [sid(1)]


# ------------------------------------------------------------------ status and secrets
def test_status_exposes_coverage_only_for_an_agent_gated_session() -> None:
    w = worker(mock_client(veto() * 3), scenario_bars())
    svc, _ = run_scenario(gated_config(), w)
    w.close()
    st = svc.status()
    assert set(st["books"]) == {BASELINE, SHADOW, BENCHMARK, AGENT_GATED}
    agents = st["agents"]
    assert agents["book"] == AGENT_GATED
    assert agents["coverage"]["by_verdict"] == {"VETOED_AGENT": 3}
    assert agents["reviewer"]["gate_counters"] == {"REVIEWED:VETOED_AGENT": 3}
    json.dumps(st, default=str)  # JSON-safe for the published snapshot


def test_entry_kinds_other_than_market_are_mirrored() -> None:
    bars = two_symbol_bars(10)
    rec = record(1, 1, kind=EntryRuleKind.LIMIT, price=999.5, stop=990.0, valid_bars=6)
    strat = ScriptedStrategy({(minute(1), "BTCUSDT"): [rec]})
    w = worker(mock_client(approve()), bars)
    svc, journal = new_service(gated_config(), strat, w)
    run(svc, bars, list(range(10)), wait=w)
    w.close()
    [(_, sub)] = entries(journal.batches(), "sim_order_submitted", book=AGENT_GATED)
    assert sub.payload["type"] == "limit"
    assert sub.payload["price"] == "999.5"
    assert svc.session.lifecycle.status(sid(1)) in (SignalStatus.OPEN, SignalStatus.CONFIRMED)
    assert ETH  # both instruments were delivered


def test_agent_gated_uses_the_baseline_risk_engine_at_submit_and_fill() -> None:
    bars = two_symbol_bars(10)
    strat = ScriptedStrategy({(minute(1), "BTCUSDT"): [record(1, 1)]})
    w = worker(mock_client(approve()), bars)
    svc, journal = new_service(gated_config(), strat, w)
    run(svc, bars, list(range(10)), wait=w)
    w.close()
    books = svc.session.sim.books
    assert books[AGENT_GATED].risk_hook is svc.session.hook
    assert books[AGENT_GATED].config == books[BASELINE].config
    assert books[AGENT_GATED].kill_switch_drawdown == books[BASELINE].kill_switch_drawdown
    checks = [
        (r["stage"], r["allow"])
        for b in journal.batches()
        for r in b[0].payload.get("risk", [])
        if r["book"] == AGENT_GATED
    ]
    assert checks == [("submit", True), ("fill", True)]  # recorded, so replay recomputes them


def test_an_outcome_is_applied_only_at_a_release_after_it_was_available() -> None:
    # the live service journals an outcome at the start of the poll that releases the next
    # bars, so release >= availability always holds there; this drives the rule directly
    bars = two_symbol_bars(10)
    strat = ScriptedStrategy({(minute(1), "BTCUSDT"): [record(1, 1, valid_bars=8)]})
    held = HeldReviewer(worker(mock_client(approve()), bars))
    svc, journal = new_service(gated_config(), strat, held)
    for i in range(2):
        step_minute(svc, bars, i)
    payload = held.results[sid(1)]
    held.order.clear()  # the service never sees it: journaled by hand below
    later = minute(5) + 30_000
    journal.append(svc.session.agent_review(payload, later))
    for i in range(2, 10):
        step_minute(svc, bars, i)
    batches = journal.batches()
    [(adm_i, adm)] = entries(batches, str(EventType.AGENT_GATED_ADMISSION))
    assert adm.payload["released_at_ms"] >= later
    head = batches[adm_i][0].payload
    assert head["bar"]["open_time_ms"] == minute(5)  # released at close(5) + 1.5 s
    assert replay(gated_config(), batches).ledger_hashes() == svc.ledger_hashes()
