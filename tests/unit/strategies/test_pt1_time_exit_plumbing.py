"""PT-1: a `bracket_reference` strategy with a time exit is accepted by the paper adapter, its
CONFIRMED intents carry the value, and the Paper `EntryRequest` equals the backtest one.
Synthetic bars only (no lake, no network)."""

from __future__ import annotations

import dataclasses
from typing import Any

from test_live_adapter import COSTS, PARAMS, TRADE_FROM, series

from tsclaude.contracts.signals import SignalRecord, SignalStatus
from tsclaude.research.source import StrategySource
from tsclaude.sim.orders import EntryRequest
from tsclaude.strategies import BracketReference
from tsclaude.strategies.live_adapter import ReferenceLiveStrategy

TIME_EXIT = 3
TIMED = dataclasses.replace(PARAMS, time_exit_bars=TIME_EXIT)
BOOKS = ("baseline", "shadow")  # what Paper passes (`session.signal_books`)


def backtest_requests(params: Any) -> tuple[StrategySource, list[EntryRequest]]:
    bars = series()
    src = StrategySource(
        BracketReference(params), COSTS, trade_from_ms=TRADE_FROM, flat_from_ms=2**62
    )
    out: list[EntryRequest] = []
    for b in bars:
        out.extend(r for r in src.on_bar(b, None) if isinstance(r, EntryRequest))  # type: ignore[arg-type]
    return src, out


def live_intents(params: Any) -> list[SignalRecord]:
    adapter = ReferenceLiveStrategy(BracketReference(params), COSTS, trade_from_ms=TRADE_FROM)
    bars = series()
    adapter.warm_up([b for b in bars if b.close_time_ms < TRADE_FROM])
    out: list[SignalRecord] = []
    for b in bars:
        out.extend(adapter.on_bar(b, None))  # type: ignore[arg-type]
    return out


def comparable(req: EntryRequest) -> dict[str, object]:
    """`to_payload()` minus the identifiers (backtest `ref:...` ids vs the live ULIDs) and books."""
    d = req.to_payload()
    for k in ("request_id", "signal_id", "books"):
        d.pop(k)
    return d


def test_adapter_accepts_a_time_exit_strategy_and_intents_carry_it() -> None:
    # previously: ValueError("paper reference strategy: time exits are not supported")
    adapter = ReferenceLiveStrategy(BracketReference(TIMED), COSTS, trade_from_ms=0)
    assert adapter.strategy.params.time_exit_bars == TIME_EXIT
    intents = live_intents(TIMED)
    assert len(intents) >= 5  # not vacuous
    assert all(i.stage is SignalStatus.CONFIRMED for i in intents)
    assert {i.time_exit_bars for i in intents} == {TIME_EXIT}
    assert all(i.model_dump(mode="json")["time_exit_bars"] == TIME_EXIT for i in intents)


def test_a_pure_bracket_strategy_still_emits_no_time_exit() -> None:
    intents = live_intents(PARAMS)
    assert intents
    assert all(i.time_exit_bars is None for i in intents)
    assert all("time_exit_bars" not in i.model_dump(mode="json") for i in intents)  # old bytes


def test_paper_entry_request_equals_the_backtest_entry_request() -> None:
    """Parity: same bars, same time exit -> equal `EntryRequest` (identifiers aside)."""
    src, requests = backtest_requests(TIMED)
    intents = live_intents(TIMED)
    confirmed = [r for r in src.records if r.proposal.stage is SignalStatus.CONFIRMED]
    assert len(requests) == len(intents) == len(confirmed) >= 5
    for rec, intent, req in zip(confirmed, intents, requests, strict=True):
        assert intent.decision_time_ms == rec.decision_ms
        paper_req = EntryRequest.from_signal(intent, books=BOOKS)  # exactly Paper's call
        assert paper_req.time_exit_bars == req.time_exit_bars == TIME_EXIT
        assert comparable(paper_req) == comparable(req)


def test_intent_and_backtest_request_share_the_proposal_value() -> None:
    src, requests = backtest_requests(TIMED)
    proposals = [r.proposal.time_exit_bars for r in src.records if r.request_id is not None]
    assert proposals == [r.time_exit_bars for r in requests] == [TIME_EXIT] * len(requests)
    assert [i.time_exit_bars for i in live_intents(TIMED)] == proposals
