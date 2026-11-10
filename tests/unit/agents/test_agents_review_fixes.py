"""Regression tests for the independent review of M5 (B1-B3, M1-M3, minor items)."""

from __future__ import annotations

import dataclasses
import json
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from agent_helpers import (
    BAR_MS,
    DECISION_MS,
    PACKET_CFG,
    analyst_payload,
    ledger_pair,
    make_bars,
    make_gate,
    make_inputs,
    make_signal,
    settlements,
    skeptic_payload,
    valid_pair,
    write_ledger,
)

from tsclaude.agents.breaker import BreakerState
from tsclaude.agents.budget import (
    MS_PER_DAY,
    CallOutcome,
    CostBasis,
    CostLedger,
    EntryKind,
    read_ledger,
)
from tsclaude.agents.client import (
    ClientTimeout,
    CompletionRequest,
    CompletionResponse,
    MockClient,
    MockStep,
    StepKind,
    Usage,
)
from tsclaude.agents.config import AgentMode, load_config
from tsclaude.agents.gate import LEDGER_FILE, AgentGate
from tsclaude.agents.packet import PacketError, build_packet
from tsclaude.contracts.signals import AgentReviewState, AgentVerdict, RunMode
from tsclaude.contracts.timeutil import ManualClock

S = AgentReviewState
REPO_CONFIG = Path(__file__).resolve().parents[3] / "configs" / "agents.yaml"
DAY = (DECISION_MS + 1_000) // MS_PER_DAY


def _review(gate: AgentGate, **kw: Any):  # type: ignore[no-untyped-def]
    kw.setdefault("risk_passed", True)
    kw.setdefault("inputs", make_inputs)
    return gate.review(kw.pop("signal", make_signal()), **kw)


# ---------------------------------------------------------------------- B1 strict ledger


def _good_line() -> dict[str, Any]:
    return json.loads(ledger_pair(DECISION_MS, "0.01")[0].to_json())


@pytest.mark.parametrize(
    "mutate",
    [
        {"cost_usd": "-1000"},
        {"cost_usd": "NaN"},
        {"cost_usd": "Infinity"},
        {"cost_usd": "abc"},
        {"cost_usd": 0.01},  # a float, not a decimal string
        {"day": DAY + 5},  # does not match at_ms
        {"input_tokens": "1"},
        {"attempt": True},
        {"latency_ms": 5.0},
        {"at_ms": -1},
        {"outcome": "great"},
        {"kind": "refund"},
        {"cost_basis": "measured"},  # a reservation must be "reserved"
        {"schema": 1},
        {"call_id": ""},
        {"extra": 1},
    ],
)
def test_corrupted_ledger_line_fails_closed(tmp_path: Path, mutate: dict[str, Any]) -> None:
    line = _good_line() | mutate
    (tmp_path / LEDGER_FILE).write_text(json.dumps(line) + "\n", encoding="utf-8")
    client = MockClient.scripted(*valid_pair())
    gate = make_gate(client, data_dir=tmp_path)
    out = _review(gate)
    assert out.state is S.BUDGET_EXHAUSTED
    assert "unreadable" in out.reason
    assert client.calls == []


def test_missing_field_and_bad_pairing_fail_closed(tmp_path: Path) -> None:
    line = _good_line()
    del line["model"]
    cases = [
        json.dumps(line),
        ledger_pair(DECISION_MS, "0.01")[1].to_json(),  # settlement without reservation
        "\n".join(e.to_json() for e in ledger_pair(DECISION_MS, "0.01", settle=False) * 2),
        "\n".join(
            e.to_json()
            for e in [*ledger_pair(DECISION_MS, "0.01"), ledger_pair(DECISION_MS, "0.01")[1]]
        ),
    ]
    for text in cases:
        path = tmp_path / LEDGER_FILE
        path.write_text(text + "\n", encoding="utf-8")
        client = MockClient.scripted(*valid_pair())
        assert _review(make_gate(client, data_dir=tmp_path)).state is S.BUDGET_EXHAUSTED
        assert client.calls == []


# ---------------------------------------------------------------------- B2 reserve, then settle


class _InspectingClient:
    """During the call, reads the ledger file as a restarted process would."""

    def __init__(self, path: Path, raise_: BaseException | None = None) -> None:
        self.path = path
        self.raise_ = raise_
        self.seen_spend: Decimal | None = None
        self.calls = 0

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls += 1
        self.seen_spend = CostLedger(self.path).spent_on(DAY)
        if self.raise_ is not None:
            raise self.raise_
        return CompletionResponse("{", Usage(10, 10), request.model)


def test_reservation_is_on_disk_before_the_call(tmp_path: Path) -> None:
    client = _InspectingClient(tmp_path / LEDGER_FILE)
    gate = make_gate(client, data_dir=tmp_path)  # type: ignore[arg-type]
    out = _review(gate)
    assert out.state is S.AGENT_OUTPUT_INVALID
    reservation = read_ledger(tmp_path / LEDGER_FILE)[0]
    assert reservation.kind is EntryKind.RESERVATION
    assert client.seen_spend == reservation.cost_usd > 0  # a crash here keeps the worst case


def test_unsettled_reservation_survives_a_restart(tmp_path: Path) -> None:
    write_ledger(
        tmp_path / LEDGER_FILE,
        ledger_pair(DECISION_MS + 1_000, "0", settle=False, reserved="0.99"),
    )
    client = MockClient.scripted(*valid_pair())
    out = _review(make_gate(client, data_dir=tmp_path))  # 0.99 held; one review does not fit
    assert out.state is S.BUDGET_EXHAUSTED
    assert client.calls == []


@pytest.mark.parametrize("exc", [SystemExit(3), KeyboardInterrupt(), GeneratorExit()])
def test_base_exception_in_the_worker_is_a_charged_failure(
    tmp_path: Path, exc: BaseException
) -> None:
    client = _InspectingClient(tmp_path / LEDGER_FILE, raise_=exc)
    gate = make_gate(client, data_dir=tmp_path)  # type: ignore[arg-type]
    out = _review(gate)  # must not raise
    assert out.state is S.AI_UNAVAILABLE
    assert out.verdict is None
    assert client.calls == 1  # no retry
    (settled,) = settlements(tmp_path / LEDGER_FILE)
    assert settled.outcome is CallOutcome.ERROR
    assert settled.cost_basis is CostBasis.WORST_CASE


# ---------------------------------------------------------------------- B3 mutant killers


def test_unexpected_client_error_is_charged_the_reservation(tmp_path: Path) -> None:
    client = MockClient.scripted([MockStep.crash()])
    _review(make_gate(client, data_dir=tmp_path))
    reservation, settled = read_ledger(tmp_path / LEDGER_FILE)
    assert settled.cost_usd == reservation.cost_usd > 0


class _OtherProcessSpends:
    """First call times out after another process (second ledger instance, same file) spent
    almost the whole cap: the retry must be refused by the per-attempt reservation."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls = 0

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls += 1
        other = CostLedger(self.path)
        for e in ledger_pair(DECISION_MS + 1_000, "0.97", call_id=f"other-{self.calls}"):
            if e.kind is EntryKind.RESERVATION:
                assert other.try_reserve(e, Decimal(1))[0]
            else:
                other.settle(e)
        raise ClientTimeout("slow")


def test_retry_is_refused_when_the_budget_ran_out_meanwhile(tmp_path: Path) -> None:
    client = _OtherProcessSpends(tmp_path / LEDGER_FILE)
    gate = make_gate(client, data_dir=tmp_path)  # type: ignore[arg-type]
    out = _review(gate)
    assert out.state is S.BUDGET_EXHAUSTED
    assert client.calls == 1  # the retry was never sent
    assert gate.budget.spent_today() <= Decimal(1)


def _steady_client(n: int, delay_s: float = 0.0) -> MockClient:
    """`n` valid replies per role, each with usage inside the reservation bounds."""
    usage = Usage(3_000, 900)
    kind = StepKind.SLOW if delay_s else StepKind.TEXT
    a = [MockStep(kind, json.dumps(analyst_payload()), delay_s, usage) for _ in range(n)]
    s = [MockStep(kind, json.dumps(skeptic_payload()), delay_s, usage) for _ in range(n)]
    return MockClient.scripted(a, s)


def test_concurrent_reviews_on_one_and_two_gates_never_exceed_the_cap(tmp_path: Path) -> None:
    clock = ManualClock(DECISION_MS + 1_000)
    cap = Decimal("0.25")
    client = _steady_client(200, delay_s=0.002)
    gates = [make_gate(client, clock=clock, data_dir=tmp_path, daily_cap_usd=cap) for _ in range(2)]
    states: list[AgentReviewState] = []
    lock = threading.Lock()

    def worker(gate: AgentGate) -> None:
        for _ in range(6):
            st = _review(gate, reevaluate=True).state
            with lock:
                states.append(st)

    threads = [threading.Thread(target=worker, args=(gates[i % 2],)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    spent = CostLedger(tmp_path / LEDGER_FILE).spent_on(DAY)
    assert spent <= cap
    assert S.REVIEWED in states
    assert S.BUDGET_EXHAUSTED in states


def test_two_gates_on_one_ledger_share_one_cap(tmp_path: Path) -> None:
    """M2: the second gate re-reads the file; before the fix it saw only its own spend."""
    clock = ManualClock(DECISION_MS + 1_000)
    cap = Decimal("0.25")
    a = make_gate(_steady_client(50), clock=clock, data_dir=tmp_path, daily_cap_usd=cap)
    b = make_gate(_steady_client(50), clock=clock, data_dir=tmp_path, daily_cap_usd=cap)
    while _review(a, reevaluate=True).state is S.REVIEWED:
        pass
    assert _review(b, reevaluate=True).state is S.BUDGET_EXHAUSTED
    assert CostLedger(tmp_path / LEDGER_FILE).spent_on(DAY) <= cap


# ---------------------------------------------------------------------- M1 cache and run modes


def test_live_review_is_not_served_to_a_replay_signal() -> None:
    client = MockClient.scripted(*[x * 2 for x in valid_pair()])
    gate = make_gate(client)
    live = _review(gate)
    replay = _review(gate, signal=make_signal(run_mode=RunMode.REPLAY))
    assert live.review is not None
    assert not live.review.indicative
    assert not replay.cache_hit
    assert replay.review is not None
    assert replay.review.indicative
    assert len(client.calls) == 4


def test_research_review_is_not_served_to_a_live_signal(tmp_path: Path) -> None:
    research = make_gate(
        MockClient.scripted(*valid_pair()),
        mode=AgentMode.RESEARCH,
        data_dir=tmp_path,
    )
    assert (
        _review(research, signal=make_signal(run_mode=RunMode.REPLAY)).verdict
        is AgentVerdict.APPROVED
    )
    client = MockClient.scripted(*valid_pair(severities=["fatal"]))
    live = _review(make_gate(client, data_dir=tmp_path))
    assert not live.cache_hit
    assert len(client.calls) == 2
    assert live.verdict is AgentVerdict.VETOED_AGENT


# ---------------------------------------------------------------------- M3 half-open trial


def test_exception_during_half_open_trial_reopens_and_allows_the_next_trial() -> None:
    clock = ManualClock(DECISION_MS + 1_000)
    client = MockClient.scripted(
        [MockStep.invalid_json(), *valid_pair()[0] * 2], valid_pair()[1] * 2
    )
    gate = make_gate(client, clock=clock, breaker_failure_threshold=1, breaker_cooldown_s=10)
    assert _review(gate).state is S.AGENT_OUTPUT_INVALID
    clock.advance(10_000)
    assert gate.breaker.state is BreakerState.HALF_OPEN

    real_put = gate.cache.put
    gate.cache.put = lambda review: (_ for _ in ()).throw(RuntimeError("disk full"))  # type: ignore[method-assign]
    out = _review(gate)
    assert out.state is S.AI_UNAVAILABLE
    assert gate.breaker.state is BreakerState.OPEN  # re-opened, not stuck in flight

    gate.cache.put = real_put  # type: ignore[method-assign]
    clock.advance(10_000)
    assert _review(gate).state is S.REVIEWED
    assert gate.breaker.state is BreakerState.CLOSED


# ---------------------------------------------------------------------- minor items


def test_zero_pricing_is_rejected_unless_off(tmp_path: Path) -> None:
    text = REPO_CONFIG.read_text(encoding="utf-8").replace(
        'output_per_mtok: "10"', 'output_per_mtok: "0"'
    )
    p = tmp_path / "a.yaml"
    p.write_text(text, encoding="utf-8")
    assert load_config(p).ok  # mode off: prices unused
    p.write_text(text.replace('mode: "off"', 'mode: "confirmed"'), encoding="utf-8")
    loaded = load_config(p)
    assert not loaded.ok
    assert loaded.config.mode.value == "off"


def test_duplicate_yaml_keys_force_off(tmp_path: Path) -> None:
    text = REPO_CONFIG.read_text(encoding="utf-8").replace('mode: "off"', 'mode: "confirmed"')
    p = tmp_path / "a.yaml"
    p.write_text(text + '\ndaily_cap_usd: "50"\n', encoding="utf-8")
    loaded = load_config(p)
    assert not loaded.ok
    assert loaded.config.mode.value == "off"
    assert any("duplicate key" in e for e in loaded.errors)


def test_over_reservation_is_recorded_in_full_and_flagged(tmp_path: Path) -> None:
    huge = Usage(10_000_000, 1_000)  # impossible within the bounds: a mis-reporting client
    client = MockClient.scripted(
        [MockStep.valid(analyst_payload(), usage=huge)], [MockStep.valid(skeptic_payload())]
    )
    gate = make_gate(client, data_dir=tmp_path, daily_cap_usd=Decimal(50))
    _review(gate)
    reservation, settled = read_ledger(tmp_path / LEDGER_FILE)[:2]
    assert settled.cost_usd > reservation.cost_usd  # not clamped down
    assert gate.snapshot()["over_reservation_calls"] == 1


def test_stale_bars_are_refused() -> None:
    stale = make_bars(end_ms=DECISION_MS - BAR_MS)
    with pytest.raises(PacketError, match="stale"):
        build_packet(make_inputs(bars=stale), PACKET_CFG)
    almost = [
        dataclasses.replace(b, close_time_ms=b.close_time_ms - BAR_MS + 1) for b in make_bars()
    ]
    build_packet(make_inputs(bars=almost), PACKET_CFG)  # within one interval: accepted


def test_date_like_labels_are_dropped() -> None:
    packet = build_packet(make_inputs(features={"regime": "range_2026"}), PACKET_CFG)
    assert "feat.regime" not in packet.fields
    assert "feat.regime" in packet.masked


def test_reservation_waits_for_the_os_file_lock(tmp_path: Path) -> None:
    """Another process holding the ledger lock blocks the reservation (and so the call)."""
    from tsclaude.agents.budget import _os_file_lock

    client = MockClient.scripted(*valid_pair())
    gate = make_gate(client, data_dir=tmp_path)
    ledger = tmp_path / LEDGER_FILE
    result: list[AgentReviewState] = []
    with _os_file_lock(ledger.with_name(ledger.name + ".lock")):
        t = threading.Thread(target=lambda: result.append(_review(gate).state))
        t.start()
        t.join(0.3)
        assert t.is_alive()  # blocked on the lock
        assert client.calls == []
    t.join(10)
    assert result == [S.REVIEWED]
