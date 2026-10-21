"""Paper-session test kit: a scripted live strategy, signal records, a driver that feeds a
`PaperTradingService` the way the market-data subscription would (bars per symbol, out of order
across symbols, snapshots before each close)."""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal as D
from pathlib import Path

from tsclaude.contracts.market import Bar, BarSource, Interval, Market
from tsclaude.contracts.signals import (
    Condition,
    EntryRule,
    EntryRuleKind,
    RunMode,
    Side,
    SignalRecord,
    SignalStatus,
)
from tsclaude.contracts.timeutil import MS_PER_MINUTE
from tsclaude.marketdata.bars import BarClosed
from tsclaude.sim.engine import SimView
from tsclaude.sim.journal import MemoryJournal
from tsclaude.sim.orders import InstrumentSpec
from tsclaude.sim.paper import Confirm, Intent, Invalidate, PaperConfig, PaperTradingService
from tsclaude.sim.testing import minute, series

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "risk"))
from risk_helpers import snapshot

BTC = (Market.SPOT, "BTCUSDT")
ETH = (Market.SPOT, "ETHUSDT")


def sid(n: int) -> str:
    return f"SIG{n:023d}"


def record(
    n: int,
    bar_index: int,
    *,
    symbol: str = "BTCUSDT",
    market: Market = Market.SPOT,
    side: Side = Side.LONG,
    stage: SignalStatus = SignalStatus.CONFIRMED,
    ref: float = 1000.0,
    stop: float = 990.0,
    tp: float | None = 1030.0,
    kind: EntryRuleKind = EntryRuleKind.MARKET_NEXT_OPEN,
    price: float | None = None,
    valid_bars: int = 5,
) -> SignalRecord:
    decision = minute(bar_index) + MS_PER_MINUTE - 1
    return SignalRecord(
        signal_id=sid(n),
        run_mode=RunMode.LIVE,
        market=market,
        symbol=symbol,
        timeframe=Interval.M1,
        strategy_id="paper_test",
        strategy_version="1",
        params_hash="0" * 16,
        prereg_id=None,
        evidence_grade="X",
        side=side,
        stage=stage,
        decision_time_ms=decision,
        created_at_ms=decision,
        snapshot_id="snap",
        entry_conditions=(
            Condition(name="test", operator="==", threshold=1, observed=1, satisfied=True),
        ),
        entry_rule=EntryRule(
            kind=kind, price=price, valid_until_ms=decision + valid_bars * MS_PER_MINUTE
        ),
        ref_price=ref,
        stop_price=stop,
        stop_basis="test",
        take_profit_price=tp,
        exit_policy="bracket",
        rr_gross=3.0,
        rr_net=2.5,
        est_cost_bps=10.0,
        break_even_win_rate=0.3,
        rationale="test",
        market_conditions={},
    )


@dataclass
class ScriptedStrategy:
    """Emits scripted intents at (bar open, symbol); records every bar it was called for."""

    script: Mapping[tuple[int, str], Sequence[Intent]]
    calls: list[tuple[int, str]] = field(default_factory=list)
    fail_at: set[tuple[int, str]] = field(default_factory=set)

    def on_bar(self, bar: Bar, view: SimView) -> Sequence[Intent]:
        key = (bar.open_time_ms, bar.symbol)
        self.calls.append(key)
        if key in self.fail_at:
            raise RuntimeError("strategy bug")
        return list(self.script.get(key, ()))


def live(b: Bar, source: BarSource = BarSource.WS_LIVE) -> Bar:
    return b.model_copy(update={"source": source})


FLAT = (1000, 1001, 999, 1000)


def two_symbol_bars(
    n: int,
    btc: Mapping[int, tuple[float, float, float, float]] | None = None,
    eth: Mapping[int, tuple[float, float, float, float]] | None = None,
    source: BarSource = BarSource.WS_LIVE,
) -> dict[tuple[Market, str], list[Bar]]:
    btc = btc or {}
    eth = eth or {}
    b = [live(x, source) for x in series([btc.get(i, FLAT) for i in range(n)], symbol="BTCUSDT")]
    e = [live(x, source) for x in series([eth.get(i, FLAT) for i in range(n)], symbol="ETHUSDT")]
    return {BTC: b, ETH: e}


def config(**kw: object) -> PaperConfig:
    specs = (
        InstrumentSpec(Market.SPOT, "BTCUSDT", D("0.1"), D("0.001"), D("5")),
        InstrumentSpec(Market.SPOT, "ETHUSDT", D("0.1"), D("0.001"), D("5")),
    )
    args: dict[str, object] = {"instruments": specs}
    args.update(kw)
    return PaperConfig(**args)  # type: ignore[arg-type]


def drive(
    service: PaperTradingService,
    bars: Mapping[tuple[Market, str], Sequence[Bar]],
    minutes: Sequence[int],
    *,
    missing: set[tuple[int, str]] = frozenset(),  # type: ignore[assignment]
    snapshot_states: Callable[[int], dict | None] | None = None,
) -> None:
    """Deliver each minute like the live subscription: snapshot just before the close, ETH's bar
    before BTC's, then poll shortly after the close (and once more past the settle deadline)."""
    for i in minutes:
        close = minute(i) + MS_PER_MINUTE - 1
        states = None if snapshot_states is None else snapshot_states(i)
        service.on_snapshot(snapshot(close - 200, states=states, symbols=(BTC, ETH)))
        for key in (ETH, BTC):
            if (i, key[1]) in missing:
                continue
            service.on_update(BarClosed(bars[key][i]))
        service.poll(close + 1_500)
    if minutes:
        last_close = minute(minutes[-1]) + MS_PER_MINUTE - 1
        service.poll(last_close + 1_500)


def new_service(
    strategy: ScriptedStrategy | None = None, journal: MemoryJournal | None = None, **kw: object
) -> tuple[PaperTradingService, MemoryJournal]:
    j = journal or MemoryJournal()
    cfg = kw.pop("cfg", None) or config()
    svc = PaperTradingService(cfg, j, strategy=strategy, **kw)  # type: ignore[arg-type]
    return svc, j


__all__ = [
    "BTC",
    "ETH",
    "Confirm",
    "Invalidate",
    "ScriptedStrategy",
    "config",
    "drive",
    "new_service",
    "record",
    "sid",
    "two_symbol_bars",
]
