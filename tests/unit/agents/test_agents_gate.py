"""Every agent gate path with the scripted mock client (M5 gate, mock part).

valid / invalid JSON / schema-invalid / timeout / slow (deadline) / API down / client crash /
over budget / cache / circuit breaker / mode off / not-requested cases / fail-safe.
No network and no API key anywhere.
"""

from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path

import pytest
from agent_helpers import (
    DECISION_MS,
    REF,
    SYMBOL,
    analyst_payload,
    make_bars,
    make_gate,
    make_inputs,
    make_signal,
    skeptic_payload,
    valid_pair,
)

from tsclaude.agents.breaker import BreakerState
from tsclaude.agents.budget import MS_PER_DAY, CallOutcome, CostBasis, read_ledger
from tsclaude.agents.client import MockClient, MockStep, Usage
from tsclaude.agents.config import AgentMode, load_config
from tsclaude.agents.gate import CACHE_FILE, LEDGER_FILE, AgentGate
from tsclaude.agents.packet import PacketInputs
from tsclaude.contracts.signals import AgentReviewState, AgentVerdict, RunMode, SignalStatus
from tsclaude.contracts.timeutil import ManualClock

S = AgentReviewState


def _inputs() -> PacketInputs:
    return make_inputs()


def _review(gate, **kw):  # type: ignore[no-untyped-def]
    kw.setdefault("risk_passed", True)
    kw.setdefault("inputs", _inputs)
    return gate.review(kw.pop("signal", make_signal()), **kw)


# ---------------------------------------------------------------------- mode off


def test_mode_off_makes_zero_calls_and_builds_nothing(tmp_path: Path) -> None:
    client = MockClient.scripted(*valid_pair())
    gate = make_gate(client, mode=AgentMode.OFF, data_dir=tmp_path)
    called = []

    def thunk() -> PacketInputs:
        called.append(1)
        return make_inputs()

    for _ in range(5):
        out = _review(gate, inputs=thunk)
        assert out.state is S.NOT_REQUESTED
        assert out.verdict is None
        assert out.admission.baseline
        assert not out.admission.agent_gated
        assert out.advisory_status(SignalStatus.CONFIRMED) is SignalStatus.CONFIRMED
    assert client.calls == []
    assert called == []
    assert gate.client is None
    assert list(tmp_path.iterdir()) == []  # no ledger, no cache
    assert gate.snapshot()["mode"] == "off"


def test_invalid_config_runs_as_off_even_with_a_client(tmp_path: Path) -> None:
    client = MockClient.scripted(*valid_pair())
    loaded = load_config(tmp_path / "missing.yaml")
    gate = AgentGate(loaded, client=client, clock=ManualClock(DECISION_MS))
    assert _review(gate).state is S.NOT_REQUESTED
    assert client.calls == []
    assert gate.snapshot()["config_errors"]


# ---------------------------------------------------------------------- valid


def test_valid_review_approves_and_records_cost_and_latency(tmp_path: Path) -> None:
    analyst, skeptic = valid_pair()
    client = MockClient.scripted(analyst, skeptic)
    gate = make_gate(client, data_dir=tmp_path)
    out = _review(gate)
    assert out.state is S.REVIEWED
    assert out.verdict is AgentVerdict.APPROVED
    assert out.calls == 2
    assert not out.cache_hit
    assert out.admission.agent_gated
    assert out.admission.baseline
    assert [c.role for c in client.calls] == ["analyst", "skeptic"]
    assert "ANALYST ASSESSMENT" in client.calls[1].user  # skeptic sees the thesis
    review = out.review
    assert review is not None
    assert review.indicative is False
    assert review.analyst_prompt.startswith("analyst-v1@")
    assert review.cost_usd > 0
    assert out.latency_ms >= 0
    entries = read_ledger(tmp_path / LEDGER_FILE)
    assert [(e.role, e.outcome, e.cost_basis) for e in entries] == [
        ("analyst", CallOutcome.OK, CostBasis.MEASURED),
        ("skeptic", CallOutcome.OK, CostBasis.MEASURED),
    ]
    assert sum(e.cost_usd for e in entries) == review.cost_usd


def test_measured_cost_uses_reported_usage() -> None:
    usage = Usage(input_tokens=5000, output_tokens=700, cached_input_tokens=1000)
    client = MockClient.scripted(
        [MockStep.valid(analyst_payload(), usage=usage)],
        [MockStep.valid(skeptic_payload(), usage=Usage(5500, 500))],
    )
    gate = make_gate(client)
    out = _review(gate)
    # analyst: 4000*2 + 1000*2 + 700*10 = 17000 / 1e6; skeptic: 5500*2 + 500*10 = 16000 / 1e6
    assert out.review is not None
    assert out.review.cost_usd == Decimal("0.033")


def test_veto_and_downgrade_are_advice_only() -> None:
    signal = make_signal()
    before = signal.model_dump()
    client = MockClient.scripted(*valid_pair(severities=["fatal"]))
    out = _review(make_gate(client), signal=signal)
    assert out.verdict is AgentVerdict.VETOED_AGENT
    assert out.advisory_status(SignalStatus.CONFIRMED) is SignalStatus.VETOED
    assert out.admission.baseline
    assert not out.admission.agent_gated
    assert signal.model_dump() == before  # price, stop, TP untouched

    client = MockClient.scripted(*valid_pair(stance="oppose", scores=(0, 0, -1, 0)))
    out = _review(make_gate(client), signal=signal)
    assert out.verdict is AgentVerdict.DOWNGRADED
    assert out.advisory_status(SignalStatus.CONFIRMED) is SignalStatus.CONFIRMED
    assert not out.admission.agent_gated


def test_what_reaches_the_client_is_masked() -> None:
    client = MockClient.scripted(*valid_pair())
    _review(make_gate(client))
    for call in client.calls:
        text = call.system + call.user
        assert SYMBOL not in text
        assert SYMBOL.lower() not in text.lower()
        assert "2026-01-01" not in text
        assert str(DECISION_MS) not in text
        assert f"{REF}" not in text
        assert f"{REF:.2f}" not in text


# ---------------------------------------------------------------------- invalid output


def test_invalid_json_is_output_invalid_without_retry_or_skeptic() -> None:
    client = MockClient.scripted([MockStep.invalid_json(), MockStep.valid(analyst_payload())])
    out = _review(make_gate(client))
    assert out.state is S.AGENT_OUTPUT_INVALID
    assert out.verdict is None
    assert len(client.calls) == 1
    assert out.admission.baseline
    assert not out.admission.agent_gated


def test_schema_invalid_skeptic_trying_to_move_the_stop_is_rejected() -> None:
    bad = skeptic_payload()
    bad["new_stop_atr"] = 0.5
    client = MockClient.scripted([MockStep.valid(analyst_payload())], [MockStep.valid(bad)])
    out = _review(make_gate(client))
    assert out.state is S.AGENT_OUTPUT_INVALID
    assert out.verdict is None
    assert out.calls == 2


def test_invented_number_is_output_invalid() -> None:
    payload = analyst_payload(summary="Price should reach 205.10 within a day.")
    client = MockClient.scripted([MockStep.valid(payload)])
    out = _review(make_gate(client))
    assert out.state is S.AGENT_OUTPUT_INVALID


# ---------------------------------------------------------------------- timeout / API down


def test_timeout_twice_is_ai_unavailable_and_charged_worst_case(tmp_path: Path) -> None:
    client = MockClient.scripted([MockStep.timeout(), MockStep.timeout()])
    out = _review(make_gate(client, data_dir=tmp_path))
    assert out.state is S.AI_UNAVAILABLE
    assert out.verdict is None
    assert out.calls == 2  # one bounded retry
    entries = read_ledger(tmp_path / LEDGER_FILE)
    assert [e.outcome for e in entries] == [CallOutcome.TIMEOUT, CallOutcome.TIMEOUT]
    assert all(e.cost_basis is CostBasis.WORST_CASE and e.cost_usd > 0 for e in entries)


def test_timeout_then_success_is_reviewed() -> None:
    client = MockClient.scripted(
        [MockStep.timeout(), MockStep.valid(analyst_payload())], [MockStep.valid(skeptic_payload())]
    )
    out = _review(make_gate(client))
    assert out.state is S.REVIEWED
    assert out.calls == 3


def test_slow_client_is_cut_off_by_the_gate_deadline() -> None:
    client = MockClient.scripted(
        [MockStep.slow(1.0, analyst_payload()), MockStep.slow(1.0, analyst_payload())]
    )
    gate = make_gate(client, per_call_timeout_s=0.05)
    t0 = time.monotonic()
    out = _review(gate)
    assert out.state is S.AI_UNAVAILABLE
    assert "timed out" in out.reason
    assert time.monotonic() - t0 < 0.9  # did not wait for the slow replies


def test_api_down_is_ai_unavailable_and_not_billed(tmp_path: Path) -> None:
    client = MockClient.scripted([MockStep.api_down(), MockStep.api_down()])
    out = _review(make_gate(client, data_dir=tmp_path))
    assert out.state is S.AI_UNAVAILABLE
    assert out.calls == 2
    entries = read_ledger(tmp_path / LEDGER_FILE)
    assert all(e.outcome is CallOutcome.API_DOWN and e.cost_usd == 0 for e in entries)


def test_skeptic_down_after_valid_analyst_is_ai_unavailable() -> None:
    client = MockClient.scripted(
        [MockStep.valid(analyst_payload())], [MockStep.api_down(), MockStep.api_down()]
    )
    out = _review(make_gate(client))
    assert out.state is S.AI_UNAVAILABLE
    assert out.verdict is None
    assert out.calls == 3


def test_client_crash_is_ai_unavailable_without_retry() -> None:
    client = MockClient.scripted([MockStep.crash(), MockStep.valid(analyst_payload())])
    out = _review(make_gate(client))
    assert out.state is S.AI_UNAVAILABLE
    assert out.calls == 1


# ---------------------------------------------------------------------- budget


def test_cap_too_small_for_one_review_makes_no_call() -> None:
    client = MockClient.scripted(*valid_pair())
    out = _review(make_gate(client, daily_cap_usd=Decimal("0.001")))
    assert out.state is S.BUDGET_EXHAUSTED
    assert client.calls == []
    assert not out.admission.agent_gated
    assert out.admission.baseline


def test_budget_exhausts_within_the_day_and_resets_next_utc_day(tmp_path: Path) -> None:
    clock = ManualClock(DECISION_MS + 1_000)
    # each review costs 0.03 measured (within the reservation bounds); the cap admits a few
    usage = Usage(3_000, 900)
    steps = [MockStep.valid(analyst_payload(), usage=usage) for _ in range(50)]
    skept = [MockStep.valid(skeptic_payload(), usage=usage) for _ in range(50)]
    client = MockClient.scripted(steps, skept)
    gate = make_gate(client, clock=clock, data_dir=tmp_path, daily_cap_usd=Decimal("0.25"))
    states = []
    for i in range(10):
        states.append(_review(gate, reevaluate=True).state)
        assert gate.budget.spent_today() <= Decimal("0.25"), i
    assert S.REVIEWED in states
    assert states[-1] is S.BUDGET_EXHAUSTED
    calls_at_cap = len(client.calls)
    assert _review(gate, reevaluate=True).state is S.BUDGET_EXHAUSTED
    assert len(client.calls) == calls_at_cap  # no call once exhausted

    # a restart does not reset the day's spend
    restarted = make_gate(client, clock=clock, data_dir=tmp_path, daily_cap_usd=Decimal("0.25"))
    assert _review(restarted, reevaluate=True).state is S.BUDGET_EXHAUSTED

    clock.set((DECISION_MS // MS_PER_DAY + 1) * MS_PER_DAY)  # next UTC day
    assert _review(restarted, reevaluate=True).state is S.REVIEWED


def test_unreadable_ledger_blocks_all_calls(tmp_path: Path) -> None:
    (tmp_path / LEDGER_FILE).write_text("{not a ledger line\n", encoding="utf-8")
    client = MockClient.scripted(*valid_pair())
    gate = make_gate(client, data_dir=tmp_path)
    out = _review(gate)
    assert out.state is S.BUDGET_EXHAUSTED
    assert "unreadable" in out.reason
    assert client.calls == []


# ---------------------------------------------------------------------- cache


def test_cache_hit_makes_no_call_and_reevaluation_adds_a_record(tmp_path: Path) -> None:
    a, s = valid_pair()
    a2, s2 = valid_pair(severities=["fatal"])
    client = MockClient.scripted(a + a2, s + s2)
    gate = make_gate(client, data_dir=tmp_path)
    first = _review(gate)
    again = _review(gate)
    assert again.cache_hit
    assert again.calls == 0
    assert len(client.calls) == 2
    assert again.verdict is first.verdict
    assert again.review == first.review

    # a restarted gate serves the persisted review without calling
    restarted = make_gate(client, data_dir=tmp_path)
    assert _review(restarted).cache_hit
    assert len(client.calls) == 2

    redo = _review(restarted, reevaluate=True)
    assert not redo.cache_hit
    assert redo.verdict is AgentVerdict.VETOED_AGENT
    assert len(client.calls) == 4
    assert len((tmp_path / CACHE_FILE).read_text().splitlines()) == 2
    assert _review(restarted).verdict is AgentVerdict.VETOED_AGENT  # newest record served


def test_different_packet_or_model_is_a_cache_miss() -> None:
    client = MockClient.scripted(*[x * 2 for x in valid_pair()])
    gate = make_gate(client)
    _review(gate)
    out = _review(gate, inputs=lambda: make_inputs(spread_bps=1.1))
    assert not out.cache_hit
    assert len(client.calls) == 4


def test_failures_are_not_cached() -> None:
    client = MockClient.scripted([MockStep.invalid_json(), *valid_pair()[0]], valid_pair()[1])
    gate = make_gate(client)
    assert _review(gate).state is S.AGENT_OUTPUT_INVALID
    assert _review(gate).state is S.REVIEWED


# ---------------------------------------------------------------------- circuit breaker


def test_breaker_opens_after_consecutive_failures_and_recovers() -> None:
    clock = ManualClock(DECISION_MS + 1_000)
    client = MockClient.scripted([MockStep.invalid_json()] * 3 + valid_pair()[0], valid_pair()[1])
    gate = make_gate(client, clock=clock, breaker_failure_threshold=3, breaker_cooldown_s=60)
    for _ in range(3):
        assert _review(gate).state is S.AGENT_OUTPUT_INVALID
    assert gate.breaker.state is BreakerState.OPEN
    out = _review(gate)
    assert out.state is S.AI_UNAVAILABLE
    assert "breaker" in out.reason
    assert len(client.calls) == 3  # refused without calling

    clock.advance(60_000)
    assert gate.breaker.state is BreakerState.HALF_OPEN
    assert _review(gate).state is S.REVIEWED  # the single trial succeeds
    assert gate.breaker.state is BreakerState.CLOSED


def test_failed_half_open_trial_reopens() -> None:
    clock = ManualClock(DECISION_MS + 1_000)
    client = MockClient.scripted([MockStep.invalid_json()] * 2)
    gate = make_gate(client, clock=clock, breaker_failure_threshold=1, breaker_cooldown_s=10)
    assert _review(gate).state is S.AGENT_OUTPUT_INVALID
    clock.advance(10_000)
    assert _review(gate).state is S.AGENT_OUTPUT_INVALID
    assert gate.breaker.state is BreakerState.OPEN


def test_cache_hits_are_served_even_with_the_breaker_open() -> None:
    client = MockClient.scripted(*valid_pair())
    gate = make_gate(client, breaker_failure_threshold=1)
    _review(gate)
    gate.breaker.record_failure()
    assert gate.breaker.state is BreakerState.OPEN
    assert _review(gate).cache_hit


# ---------------------------------------------------------------------- not requested / fail-safe


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"risk_passed": False}, "risk-blocked"),
        ({"signal": make_signal(stage=SignalStatus.WATCH)}, "CONFIRMED"),
        ({"status": SignalStatus.VETOED}, "CONFIRMED"),
        ({"status": SignalStatus.OPEN}, "CONFIRMED"),
    ],
)
def test_only_risk_passed_confirmed_signals_are_reviewed(
    kw: dict[str, object], reason: str
) -> None:
    client = MockClient.scripted(*valid_pair())
    out = _review(make_gate(client), **kw)
    assert out.state is S.NOT_REQUESTED
    assert reason in out.reason
    assert client.calls == []
    if kw.get("risk_passed") is False:
        assert not out.admission.baseline
        assert not out.admission.agent_gated


def test_research_mode_skips_live_and_labels_replay_indicative() -> None:
    client = MockClient.scripted(*valid_pair())
    gate = make_gate(client, mode=AgentMode.RESEARCH)
    assert _review(gate).state is S.NOT_REQUESTED
    assert client.calls == []
    out = _review(gate, signal=make_signal(run_mode=RunMode.REPLAY))
    assert out.state is S.REVIEWED
    assert out.review is not None
    assert out.review.indicative


def test_packet_refusal_is_ai_unavailable_without_calls() -> None:
    client = MockClient.scripted(*valid_pair())
    future = make_bars(end_ms=DECISION_MS + 15 * 60_000)
    out = _review(make_gate(client), inputs=lambda: make_inputs(bars=future))
    assert out.state is S.AI_UNAVAILABLE
    assert "packet" in out.reason
    assert client.calls == []


def test_any_internal_error_is_ai_unavailable_never_approval() -> None:
    def broken() -> PacketInputs:
        raise KeyError("feature store exploded")

    client = MockClient.scripted(*valid_pair())
    out = _review(make_gate(client), inputs=broken)
    assert out.state is S.AI_UNAVAILABLE
    assert out.verdict is None
    assert client.calls == []


def test_no_client_in_confirmed_mode_is_ai_unavailable() -> None:
    out = _review(make_gate(None))
    assert out.state is S.AI_UNAVAILABLE
    assert out.verdict is None


def test_unscripted_extra_call_fails_closed() -> None:
    client = MockClient.scripted([MockStep.valid(analyst_payload())], [])
    out = _review(make_gate(client))
    assert out.state is S.AI_UNAVAILABLE


def test_property_no_failure_path_ever_yields_a_verdict() -> None:
    steps = [
        MockStep.invalid_json,
        MockStep.timeout,
        MockStep.api_down,
        MockStep.crash,
        lambda: MockStep.raw('{"stance": "support"}'),
        lambda: MockStep.raw("null"),
    ]
    for make_a in steps:
        for make_s in [*steps, lambda: MockStep.valid(skeptic_payload())]:
            client = MockClient.scripted([make_a(), make_a()], [make_s(), make_s()])
            out = _review(make_gate(client))
            assert out.state is not S.REVIEWED
            assert out.verdict is None
            assert not out.admission.agent_gated
            assert out.advisory_status(SignalStatus.CONFIRMED) is SignalStatus.CONFIRMED
    for make_s in steps:
        client = MockClient.scripted([MockStep.valid(analyst_payload())], [make_s(), make_s()])
        out = _review(make_gate(client))
        assert out.verdict is None
        assert not out.admission.agent_gated


def test_snapshot_reports_budget_breaker_and_counters() -> None:
    client = MockClient.scripted(*valid_pair())
    gate = make_gate(client)
    _review(gate)
    snap = gate.snapshot()
    assert snap["mode"] == "confirmed"
    assert snap["breaker"] == "CLOSED"
    assert snap["calls_today"] == 2
    assert snap["cached_reviews"] == 1
    assert snap["counters"] == {"REVIEWED:APPROVED": 1}
    assert Decimal(snap["spent_today_usd"]) + Decimal(snap["remaining_today_usd"]) == Decimal(1)
