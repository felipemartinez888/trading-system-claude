"""PT-1: the time exit travels strategy -> `SignalRecord` -> `EntryRequest` -> Paper book.

Covers the contract field (replay compatibility), `EntryRequest.from_signal`, Paper end to end
against the backtest book (same inputs, same fill) and unchanged risk verdicts. Synthetic bars only.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal as D
from typing import Any

import pytest
from paper_helpers import (
    Confirm,
    ScriptedStrategy,
    config,
    drive,
    new_service,
    record,
    sid,
    two_symbol_bars,
)
from risk_helpers import check, request, view

from tsclaude.contracts.events import canonical_json
from tsclaude.contracts.signals import SignalRecord, SignalStatus
from tsclaude.risk.config import RiskConfig
from tsclaude.risk.engine import RiskEngine
from tsclaude.sim.costs import CostModel
from tsclaude.sim.fills import ExitReason
from tsclaude.sim.orders import EntryRequest
from tsclaude.sim.testing import T0, minute, run_book, series

# Golden numbers of test_sim_golden.test_g7: spot long, entry at the open of bar 1, time_exit_bars=3
# -> due at the open of bar 4 (see that docstring for the hand computation).
ROWS = [
    (1000, 1001, 999, 1000),  # bar 0: decision at its close
    (1000, 1005, 998, 1002),  # bar 1: entry at the open
    (1002, 1003, 1001, 1002),
    (1002, 1004, 1001, 1003),
    (1004, 1005, 1003, 1004),  # bar 4: time exit at the open
    (1004, 1005, 1003, 1004),
    (1004, 1005, 1003, 1004),
    (1004, 1005, 1003, 1004),
]
N = len(ROWS)
#: `paper_helpers.drive` polls 1.5 s after each close; the backtest reproduces that delivery delay
POLL_DELAY_MS = 1_500


def with_time_exit(rec: SignalRecord, bars: int | None) -> SignalRecord:
    return SignalRecord.model_validate({**rec.model_dump(), "time_exit_bars": bars})


def base_record(**kw: Any) -> SignalRecord:
    return record(1, 0, tp=1030.0, **kw)  # R:R 3 passes the risk gate; TP/SL never touched


# ------------------------------------------------------------------ (a) contract
def test_old_payload_without_the_key_parses_and_dumps_without_it() -> None:
    rec = base_record()
    payload = rec.model_dump(mode="json")
    assert "time_exit_bars" not in payload  # None -> omitted: old bytes unchanged
    assert "time_exit_bars" not in rec.model_dump()
    parsed = SignalRecord.model_validate(payload)
    assert parsed.time_exit_bars is None
    assert canonical_json(parsed.model_dump(mode="json")) == canonical_json(payload)


def test_explicit_none_is_omitted_and_a_value_round_trips() -> None:
    none_rec = with_time_exit(base_record(), None)
    assert "time_exit_bars" not in none_rec.model_dump(mode="json")
    assert canonical_json(none_rec.model_dump(mode="json")) == canonical_json(
        base_record().model_dump(mode="json")
    )
    rec = with_time_exit(base_record(), 3)
    payload = rec.model_dump(mode="json")
    assert payload["time_exit_bars"] == 3
    again = SignalRecord.model_validate(payload)
    assert again.time_exit_bars == 3
    assert again == rec
    assert canonical_json(again.model_dump(mode="json")) == canonical_json(payload)


@pytest.mark.parametrize("bad", [0, -1])
def test_time_exit_below_one_is_rejected(bad: int) -> None:
    with pytest.raises(ValueError, match="time_exit_bars"):
        with_time_exit(base_record(), bad)


def test_time_exit_of_exactly_one_is_accepted() -> None:
    """Boundary: 1 is the smallest legal value (kills the `< 1` -> `<= 1` mutant)."""
    rec = with_time_exit(base_record(), 1)
    assert rec.time_exit_bars == 1
    assert rec.model_dump(mode="json")["time_exit_bars"] == 1
    assert SignalRecord.model_validate(rec.model_dump(mode="json")) == rec
    assert EntryRequest.from_signal(rec).time_exit_bars == 1


@pytest.mark.parametrize("mode", ["validation", "serialization"])
def test_json_schema_generation_with_the_omitting_serializer(mode: Any) -> None:
    """The wrap model_serializer must not break schema generation (OpenAPI / docs tooling)."""
    schema = SignalRecord.model_json_schema(mode=mode)  # must not raise
    assert schema["type"] == "object"
    if mode == "validation":
        assert "time_exit_bars" in schema["properties"]
        assert "time_exit_bars" not in schema.get("required", [])  # optional, default None
        assert "signal_id" in schema["required"]  # sanity: `required` is populated
    # serialization mode: the wrap serializer is annotated `dict[str, Any]`, so pydantic emits a
    # generic object schema without `properties` (known and harmless: only generation is asserted)


# ------------------------------------------------------------------ (b) from_signal
def test_from_signal_carries_the_records_value() -> None:
    rec = with_time_exit(base_record(), 3)
    assert EntryRequest.from_signal(rec).time_exit_bars == 3
    assert EntryRequest.from_signal(rec, time_exit_bars=3).time_exit_bars == 3  # agreeing: fine
    assert EntryRequest.from_signal(base_record()).time_exit_bars is None


def test_from_signal_conflicting_argument_raises() -> None:
    rec = with_time_exit(base_record(), 3)
    with pytest.raises(ValueError, match="conflicts"):
        EntryRequest.from_signal(rec, time_exit_bars=4)


def test_from_signal_argument_only_is_unchanged() -> None:
    rec = base_record()
    req = EntryRequest.from_signal(rec, time_exit_bars=3)
    assert req.time_exit_bars == 3
    plain = EntryRequest.from_signal(rec)
    assert dataclasses.replace(plain, time_exit_bars=3) == req  # nothing else differs


def test_time_exit_is_in_the_request_payload() -> None:
    req = EntryRequest.from_signal(with_time_exit(base_record(), 3))
    assert req.to_payload()["time_exit_bars"] == 3


# ------------------------------------------------------------------ (e) Paper end to end
def run_paper(rec: SignalRecord) -> Any:
    strat = ScriptedStrategy({(minute(0), "BTCUSDT"): [rec]})
    svc, _ = new_service(strat)
    drive(svc, two_symbol_bars(N, btc=dict(enumerate(ROWS))), list(range(N)))
    return svc


def price_fill(t: Any) -> tuple[Any, ...]:
    """`fill` without the bar timestamps."""
    return (t.entry_price, t.qty, t.exit_price, t.exit_reason, t.fees, t.net_pnl)


def run_flat(rec: SignalRecord) -> Any:
    """Like `run_paper` but on flat bars only (fill independent of the entry bar)."""
    svc, _ = new_service(ScriptedStrategy({(minute(0), "BTCUSDT"): [rec]}))
    drive(svc, two_symbol_bars(N), list(range(N)))
    return svc


def backtest_trades(cfg: Any, req: EntryRequest) -> list[Any]:
    book, _ = run_book(
        series(ROWS),
        {(T0, "BTCUSDT"): [req]},
        specs=list(cfg.instruments),
        cost_model=cfg.cost_model,
        equity=cfg.starting_equity,
        decision_latency_ms=POLL_DELAY_MS,
        config=cfg.baseline_config(),
    )
    return list(book.trades)


def fill(t: Any) -> tuple[Any, ...]:
    return (
        t.entry_bar_open_ms,
        t.entry_price,
        t.qty,
        t.exit_bar_open_ms,
        t.exit_price,
        t.exit_reason,
        t.fees,
        t.net_pnl,
        t.r_multiple,
    )


def test_paper_closes_a_confirmed_time_exit_signal_at_the_due_bar() -> None:
    rec = with_time_exit(base_record(), 3)
    svc = run_paper(rec)
    assert svc.fault is None
    state = svc.session.lifecycle.get(sid(1))
    assert state is not None
    assert state.status is SignalStatus.CLOSED, (state.status, state.reason, state.history)
    trades = svc.session.sim.books["baseline"].trades
    assert len(trades) == 1
    (t,) = trades
    assert t.exit_reason is ExitReason.TIME_EXIT
    # decision at the close of bar 0, delivered by the poll 1.5 s after it, i.e. after bar 1
    # opened: the earliest execution is the open of bar 2 (observation latency, SIMULATION.md)
    assert t.entry_bar_open_ms == minute(2)
    assert t.exit_bar_open_ms == minute(5)  # entry bar + 3 strategy bars, at the open
    state = svc.session.lifecycle.get(sid(1))
    assert state is not None
    assert state.status is SignalStatus.CLOSED
    assert state.outcome is not None
    assert state.outcome["exit_reason"] == "time_exit"
    # equal to the backtest book for the same inputs (the backtest call style: explicit argument)
    expected = backtest_trades(config(), EntryRequest.from_signal(base_record(), time_exit_bars=3))
    assert [fill(x) for x in trades] == [fill(x) for x in expected]
    assert len(expected) == 1
    assert expected[0].exit_reason is ExitReason.TIME_EXIT
    # hand-computed (as test_sim_golden.test_g7): entry ceil_tick(1002 x 1.0003 = 1002.3006) =
    # 1002.4 at bar 2's open; exit floor_tick(1004 x 0.9997 = 1003.6988) = 1003.6 at bar 5's open
    assert t.entry_price == D("1002.4")
    assert t.exit_price == D("1003.6")
    for book in svc.session.sim.books.values():
        assert book.ledger.reconcile().ok


def test_watch_then_confirm_enters_and_time_exits_with_the_same_fill() -> None:
    """WATCH -> Confirm rebuilds the record from `state.record` (paper.py `_translate`) before
    `EntryRequest.from_signal`. Mutant covered: the lifecycle state (create / transition) dropping
    `time_exit_bars` from the stored record -> the confirmed request has no time exit, the position
    never closes and this test fails (on the `state.record` assertion and on the TIME_EXIT one)."""
    watch = with_time_exit(base_record(stage=SignalStatus.WATCH, valid_bars=6), 3)
    assert watch.stage is SignalStatus.WATCH
    # confirmed one bar after creation (decision at bar 1's close) -> entry one bar later than the
    # signal confirmed at creation; flat bars make the fill independent of that shift
    strat = ScriptedStrategy(
        {(minute(0), "BTCUSDT"): [watch], (minute(1), "BTCUSDT"): [Confirm(sid(1))]}
    )
    svc, _ = new_service(strat)
    drive(svc, two_symbol_bars(N), list(range(N)))
    assert svc.fault is None
    state = svc.session.lifecycle.get(sid(1))
    assert state is not None
    assert state.record["time_exit_bars"] == 3  # the stored lifecycle record keeps the key
    assert state.status is SignalStatus.CLOSED, (state.status, state.reason, state.history)
    assert state.outcome is not None
    assert state.outcome["exit_reason"] == "time_exit"
    (t,) = svc.session.sim.books["baseline"].trades
    assert t.exit_reason is ExitReason.TIME_EXIT
    assert t.exit_bar_open_ms - t.entry_bar_open_ms == 3 * (minute(1) - minute(0))

    ref = run_flat(with_time_exit(base_record(), 3))  # CONFIRMED at creation
    (r,) = ref.session.sim.books["baseline"].trades
    assert r.entry_bar_open_ms < t.entry_bar_open_ms  # the WATCH path really entered later
    assert r.exit_bar_open_ms - r.entry_bar_open_ms == 3 * (minute(1) - minute(0))
    assert price_fill(t) == price_fill(r)
    for book in svc.session.sim.books.values():
        assert book.ledger.reconcile().ok


def test_without_the_time_exit_the_position_stays_open() -> None:
    """Discrimination: the TIME_EXIT above is caused by the value, not by anything else."""
    svc = run_paper(base_record())
    book = svc.session.sim.books["baseline"]
    assert book.trades == []
    assert book.open_positions == 1


def test_mutation_dropping_the_value_in_from_signal_fails_the_end_to_end_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = EntryRequest.from_signal.__func__  # type: ignore[attr-defined]

    def dropping(cls: type[EntryRequest], rec: SignalRecord, **kw: Any) -> EntryRequest:
        return dataclasses.replace(original(cls, rec, **kw), time_exit_bars=None)

    monkeypatch.setattr(EntryRequest, "from_signal", classmethod(dropping))
    svc = run_paper(with_time_exit(base_record(), 3))
    book = svc.session.sim.books["baseline"]
    # the assertions of the end-to-end test would fail: no TIME_EXIT trade
    assert [t for t in book.trades if t.exit_reason is ExitReason.TIME_EXIT] == []
    assert book.open_positions == 1


def test_time_exit_run_is_deterministic() -> None:
    rec = with_time_exit(base_record(), 3)
    a, b = run_paper(rec), run_paper(rec)
    assert [fill(t) for t in a.session.sim.books["baseline"].trades] == [
        fill(t) for t in b.session.sim.books["baseline"].trades
    ]


# ------------------------------------------------------------------ (f) risk unchanged
def _engine() -> RiskEngine:
    return RiskEngine(RiskConfig(), CostModel())


@pytest.mark.parametrize(
    "overrides",
    [
        {},  # passes
        {"take_profit_price": D("1002")},  # reward:risk fails
        {"stop_price": D("999.9")},  # stop too tight for the cost model
    ],
)
def test_risk_verdict_is_identical_with_and_without_a_time_exit(overrides: dict[str, Any]) -> None:
    engine = _engine()
    plain = engine.evaluate(check(request=request(**overrides)), view())
    timed = engine.evaluate(check(request=request(time_exit_bars=3, **overrides)), view())
    assert plain == timed
    # the execution-stage check too
    plain_x = engine.evaluate(check("execute", request=request(**overrides)), view())
    timed_x = engine.evaluate(
        check("execute", request=request(time_exit_bars=3, **overrides)), view()
    )
    assert plain_x == timed_x


def test_paper_veto_is_identical_with_and_without_a_time_exit() -> None:
    def run(bars: int | None) -> tuple[Any, Any]:
        rec = with_time_exit(record(1, 0, tp=1002.0), bars)  # R:R far below the minimum
        svc = run_paper(rec)
        state = svc.session.lifecycle.get(sid(1))
        assert state is not None
        return state.status, state.reason

    plain, timed = run(None), run(3)
    assert plain == timed
    assert plain[0] is SignalStatus.VETOED
