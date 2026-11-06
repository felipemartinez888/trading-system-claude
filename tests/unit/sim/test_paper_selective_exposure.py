"""M3-PB.5: the runtime backfill completeness check is selective by exposure.

Only an instrument with an open position or a resting entry must have every downtime bar; a flat
instrument's missing bars never block (they cannot change any ledger), and an exposed
instrument's missing bar always refuses the backfill (fail closed, nothing appended). The
"minute before the first live minute" check refuses a backfill that could not have closed the
downtime when nothing is exposed. The OD-2 harness exposes every instrument (benchmark book), so
none of this was covered (auditor NB-3, mutants A5/A8).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from paper_helpers import (
    BTC,
    ETH,
    ScriptedStrategy,
    config,
    drive,
    new_service,
    record,
    two_symbol_bars,
)

from tsclaude.runtime.paper_runtime import PaperRuntime
from tsclaude.sim.journal import MemoryJournal
from tsclaude.sim.paper import PaperTradingService, ServiceState
from tsclaude.sim.testing import minute

M = 60_000
CFG = config(benchmark=False)
SCRIPT = {(minute(2), "BTCUSDT"): [record(1, 2)]}  # BTC exposed through the downtime, ETH flat
NOW = minute(10)
CALLS: dict[int, ScriptedStrategy] = {}  # the restored service's strategy, for no-execution checks


def _blocked(script: dict | None) -> tuple[PaperTradingService, MemoryJournal, dict]:
    bars = two_symbol_bars(14)
    journal = MemoryJournal()
    first, _ = new_service(ScriptedStrategy(script or {}), journal=journal, cfg=CFG)
    drive(first, bars, list(range(6)))
    strategy = ScriptedStrategy(script or {})
    svc = PaperTradingService.restore(CFG, journal, strategy=strategy)
    CALLS[id(svc)] = strategy
    drive(svc, bars, [9])
    assert svc.state is ServiceState.AWAITING_BACKFILL
    return svc, journal, bars


def _attempt(svc: PaperTradingService, bars: dict, lost: set[tuple[str, int]]) -> dict:
    def store(market, symbol, start, end):
        key = BTC if symbol == "BTCUSDT" else ETH
        return [
            b
            for b in bars[key]
            if start <= b.open_time_ms < end
            and (symbol, (b.open_time_ms - minute(0)) // M) not in lost
        ]

    rt = SimpleNamespace(
        settings=SimpleNamespace(backfill_from_live_bars=True), _bars=store, backfill=None
    )
    PaperRuntime._attempt_backfill(rt, svc, NOW)  # type: ignore[arg-type]
    return rt.backfill


def test_a_flat_instruments_missing_bars_do_not_block() -> None:
    svc, _, bars = _blocked(SCRIPT)
    out = _attempt(svc, bars, {("ETHUSDT", m) for m in (6, 7, 8)})
    assert out["ok"] is True
    assert out["problems"] == []
    assert out["processed"] == 3  # BTC 6-8 only; ETH is flat and nothing is invented for it
    svc.poll(NOW)
    assert svc.state is ServiceState.RUNNING
    assert svc.fault is None


@pytest.mark.parametrize("gone", [6, 7, 8])
def test_an_exposed_instruments_missing_bar_refuses_and_appends_nothing(gone: int) -> None:
    svc, journal, bars = _blocked(SCRIPT)
    before = len(journal.batches())
    calls = list(CALLS[id(svc)].calls)
    out = _attempt(svc, bars, {("BTCUSDT", gone)})
    assert out["ok"] is False
    assert any("BTC" in p and "missing" in p for p in out["problems"])
    assert len(journal.batches()) == before
    assert CALLS[id(svc)].calls == calls  # the strategy was not run
    assert svc.state is ServiceState.AWAITING_BACKFILL


def test_with_nothing_exposed_the_minute_before_the_first_live_one_is_required() -> None:
    svc, journal, bars = _blocked(None)
    before = len(journal.batches())
    calls = list(CALLS[id(svc)].calls)
    out = _attempt(svc, bars, {(s, 8) for s in ("BTCUSDT", "ETHUSDT")})
    assert out["ok"] is False
    assert any("minute before the first live" in p for p in out["problems"])
    assert len(journal.batches()) == before
    assert CALLS[id(svc)].calls == calls  # the strategy was not run
    # control: one instrument holding that minute is enough when nothing is exposed
    svc, _, bars = _blocked(None)
    assert _attempt(svc, bars, {("ETHUSDT", 8)})["ok"] is True
