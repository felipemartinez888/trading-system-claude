"""Regression tests for the M5 re-review (N1 ledger exceptions, N3 surviving mutants,
N4 bounded Windows lock)."""

from __future__ import annotations

import json
import sys
import types
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from agent_helpers import (
    DECISION_MS,
    ledger_pair,
    make_gate,
    make_inputs,
    make_signal,
    valid_pair,
)

from tsclaude.agents import budget as budget_mod
from tsclaude.agents.breaker import BreakerState
from tsclaude.agents.budget import (
    MS_PER_DAY,
    CostLedger,
    EntryKind,
    LedgerUnreadable,
    read_ledger,
)
from tsclaude.agents.client import MockClient, MockStep
from tsclaude.agents.gate import LEDGER_FILE, AgentGate
from tsclaude.contracts.signals import AgentReviewState
from tsclaude.contracts.timeutil import ManualClock

S = AgentReviewState


def _review(gate: AgentGate, **kw: Any):  # type: ignore[no-untyped-def]
    kw.setdefault("risk_passed", True)
    kw.setdefault("inputs", make_inputs)
    return gate.review(kw.pop("signal", make_signal()), **kw)


def _good_json() -> str:
    return ledger_pair(DECISION_MS, "0.01")[0].to_json()


# ---------------------------------------------------------------------- N1


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps(json.loads(_good_json()) | {"cost_usd": "1E+40"}).encode(),
        b"\xff\xfe not utf-8\n",
        ("[" * 200_000).encode(),
    ],
    ids=["decimal-overflow", "invalid-utf8", "deep-nesting"],
)
def test_pathological_ledger_lines_refuse_instead_of_raising(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / LEDGER_FILE
    path.write_bytes(raw)
    with pytest.raises(LedgerUnreadable):
        read_ledger(path)

    client = MockClient.scripted(*valid_pair())
    gate = make_gate(client, data_dir=tmp_path)  # constructing the gate must not crash
    assert gate.snapshot() is not None
    out = _review(gate)
    assert out.state is S.BUDGET_EXHAUSTED
    assert "unreadable" in out.reason
    assert client.calls == []


def test_decimal_overflow_case_is_a_decimal_overflow() -> None:
    # guard the parametrization above: "1E+40" parses but cannot be quantized to 1e-8
    from decimal import InvalidOperation

    with pytest.raises(InvalidOperation):
        budget_mod.q(Decimal("1E+40"))


# ---------------------------------------------------------------------- N3 surviving mutants


def test_settlement_on_another_day_than_its_reservation_fails_closed(tmp_path: Path) -> None:
    reservation = ledger_pair(DECISION_MS, "0.01", settle=False)[0]
    settlement = ledger_pair(DECISION_MS + MS_PER_DAY, "0.01")[1]  # day matches its own at_ms
    path = tmp_path / LEDGER_FILE
    path.write_text(reservation.to_json() + "\n" + settlement.to_json() + "\n", encoding="utf-8")
    with pytest.raises(LedgerUnreadable):
        read_ledger(path)
    client = MockClient.scripted(*valid_pair())
    assert _review(make_gate(client, data_dir=tmp_path)).state is S.BUDGET_EXHAUSTED
    assert client.calls == []


def test_both_calls_must_fit_before_the_first_is_made(tmp_path: Path) -> None:
    # measure the analyst call's own worst-case reservation on a normal run
    probe = tmp_path / "probe"
    assert _review(make_gate(MockClient.scripted(*valid_pair()), data_dir=probe)).state is (
        S.REVIEWED
    )
    analyst_reserve = next(
        e.cost_usd
        for e in read_ledger(probe / LEDGER_FILE)
        if e.kind is EntryKind.RESERVATION and e.role == "analyst"
    )
    # room for the analyst alone, not for analyst + skeptic
    cap = analyst_reserve * Decimal("1.5")
    client = MockClient.scripted(*valid_pair())
    gate = make_gate(client, data_dir=tmp_path / "run", daily_cap_usd=cap)
    out = _review(gate)
    assert out.state is S.BUDGET_EXHAUSTED
    assert client.calls == []  # no half-paid review
    assert read_ledger(tmp_path / "run" / LEDGER_FILE) == []


def test_budget_refusal_during_half_open_trial_releases_the_trial() -> None:
    clock = ManualClock(DECISION_MS + 1_000)
    client = MockClient.scripted(
        [MockStep.invalid_json(), *valid_pair()[0] * 2], valid_pair()[1] * 2
    )
    gate = make_gate(client, clock=clock, breaker_failure_threshold=1, breaker_cooldown_s=10)
    assert _review(gate).state is S.AGENT_OUTPUT_INVALID
    clock.advance(10_000)
    assert gate.breaker.state is BreakerState.HALF_OPEN

    real_cap = gate.budget.cap
    gate.budget.cap = Decimal(0)
    calls_before = len(client.calls)
    assert _review(gate).state is S.BUDGET_EXHAUSTED
    assert len(client.calls) == calls_before

    gate.budget.cap = real_cap
    out = _review(gate)
    assert out.state is S.REVIEWED, out.reason  # not "circuit breaker open"
    assert gate.breaker.state is BreakerState.CLOSED


# ---------------------------------------------------------------------- N4 Windows lock


def test_windows_lock_retries_are_bounded_and_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[int] = []

    def locking(fd: int, mode: int, n: int) -> None:
        attempts.append(mode)
        raise OSError("permanent")

    fake = types.ModuleType("msvcrt")
    fake.LK_LOCK = 1  # type: ignore[attr-defined]
    fake.LK_UNLCK = 0  # type: ignore[attr-defined]
    fake.locking = locking  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setattr(budget_mod, "sys", types.SimpleNamespace(platform="win32"))

    with pytest.raises(LedgerUnreadable), budget_mod._os_file_lock(tmp_path / "x.lock"):
        pass
    assert len(attempts) == budget_mod._WIN_LOCK_ATTEMPTS

    ledger = CostLedger(tmp_path / LEDGER_FILE)  # construction records the error, no raise
    assert ledger.error is not None
    assert "lock unavailable" in ledger.error
