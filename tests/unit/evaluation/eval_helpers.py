"""Evaluation-export test kit: a real hash-chained event store holding what the app records --
an app session with a monitoring interval (coverage), demo-detector WATCH signals (DEMO), and two
deterministic paper sessions driven through the production journal (`PaperTradingService.open`):
``paper`` (spot, the lifecycle scenario of `test_paper_session`, with a process restart in the
middle) and ``paper-2`` (a USD-M short left open plus a spot long, so short equity and
unrealized P&L are exercised). No network."""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sim"))

from paper_helpers import (
    BTC,
    ETH,
    ScriptedStrategy,
    config,
    drive,
    live,
    record,
    two_symbol_bars,
)
from risk_helpers import snapshot

from tsclaude.contracts.market import Bar, Market
from tsclaude.contracts.signals import Side, SignalStatus
from tsclaude.contracts.timeutil import MS_PER_MINUTE, ManualClock
from tsclaude.marketdata.bars import BarClosed
from tsclaude.runtime.sessions import SessionManager
from tsclaude.signals.demo import DemoDetector, DemoDetectorConfig
from tsclaude.sim.orders import InstrumentSpec
from tsclaude.sim.paper import Confirm, PaperConfig, PaperTradingService
from tsclaude.sim.testing import T0, minute, series, usdm_spec
from tsclaude.store.eventstore import EventStore

N = 20
BTC_ROWS = {5: (1000, 1035, 999, 1030), 9: (1000, 1015, 999, 1010)}
ETH_ROWS = {7: (1000, 1001, 985, 988)}
RESTART_AT = 11  # minutes 0..10 before the restart, 11..19 after
USDM_BTC = (Market.USDM, "BTCUSDT")
N2 = 10
RESET_AFTER = N + 8  # paper-2: minutes N..N+7, operator reset, then N+8..N+9


def scenario() -> ScriptedStrategy:
    """The lifecycle scenario of `test_paper_session` (TP, stop, veto with shadow outcome)."""
    from paper_helpers import Invalidate, sid

    from tsclaude.contracts.signals import EntryRuleKind

    m = minute
    return ScriptedStrategy(
        {
            (m(1), "BTCUSDT"): [record(1, 1)],
            (m(2), "ETHUSDT"): [
                record(2, 2, symbol="ETHUSDT", stage=SignalStatus.WATCH, valid_bars=6)
            ],
            (m(4), "ETHUSDT"): [Confirm(sid(2))],
            (m(6), "BTCUSDT"): [record(3, 6, tp=1012.0)],
            (m(8), "BTCUSDT"): [record(4, 8, stage=SignalStatus.WATCH, valid_bars=3)],
            (m(10), "BTCUSDT"): [
                record(5, 10, kind=EntryRuleKind.LIMIT, price=995.0, stop=985.0, valid_bars=4)
            ],
            (m(12), "BTCUSDT"): [Invalidate(sid(5))],
        }
    )


def config2() -> PaperConfig:
    from decimal import Decimal as D

    eth = InstrumentSpec(Market.SPOT, "ETHUSDT", D("0.1"), D("0.001"), D("5"))
    return config(instruments=(usdm_spec(), eth))


def scenario2() -> ScriptedStrategy:
    m = minute
    return ScriptedStrategy(
        {
            (m(N + 1), "BTCUSDT"): [
                record(21, N + 1, market=Market.USDM, side=Side.SHORT, stop=1010.0, tp=960.0)
            ],
            (m(N + 1), "ETHUSDT"): [record(22, N + 1, symbol="ETHUSDT", tp=1060.0)],
        }
    )


def bars2() -> dict[tuple[Market, str], list[Bar]]:
    btc = {N + 3: (1000, 1001, 994, 995), N + 5: (995, 996, 990, 992)}
    # minute N+5 opens 30 % below the long's stop: the gap fill costs the baseline more than
    # its 10 % drawdown kill switch, which halts it at that minute's mark
    eth = {N + 4: (1000, 1004, 999, 1003), N + 5: (700, 700, 690, 695)}
    flat = (1000, 1001, 999, 1000)
    rows_b = [btc.get(N + i, flat) for i in range(N2)]
    rows_e = [eth.get(N + i, flat) for i in range(N2)]
    return {
        USDM_BTC: [
            live(b) for b in series(rows_b, symbol="BTCUSDT", market=Market.USDM, first_index=N)
        ],
        ETH: [live(b) for b in series(rows_e, symbol="ETHUSDT", first_index=N)],
    }


def drive_keys(
    service: PaperTradingService,
    bars: Mapping[tuple[Market, str], Sequence[Bar]],
    keys: Sequence[tuple[Market, str]],
    indexes: Sequence[int],
    *,
    first: int,
) -> None:
    """Like `paper_helpers.drive` for any instruments; `bars[key][0]` is minute `first`."""
    for i in indexes:
        close = minute(i) + MS_PER_MINUTE - 1
        service.on_snapshot(snapshot(close - 200, symbols=tuple(keys)))
        for key in reversed(keys):
            service.on_update(BarClosed(bars[key][i - first]))
        service.poll(close + 1_500)
    last_close = minute(indexes[-1]) + MS_PER_MINUTE - 1
    service.poll(last_close + 1_500)


def demo_detector(store: EventStore, clock: ManualClock) -> DemoDetector:
    counter = iter(range(1, 1000))
    return DemoDetector(
        DemoDetectorConfig(threshold=0.003),
        append=store.append,
        health=lambda market, symbol: (True, None),
        now_ms=clock.now_ms,
        new_id=lambda ms: f"DEMO{next(counter):022d}",
    )


@dataclass
class Built:
    path: Path
    service: PaperTradingService  # the restored, then continued, ``paper`` session
    service2: PaperTradingService  # ``paper-2``
    clock: ManualClock
    halted_head_seq: int  # chain head while paper-2's baseline was halted (before the reset)


def build_store(path: Path) -> Built:
    """Write the deterministic store described in the module docstring; returns live services
    whose numbers the scorer must reproduce."""
    clock = ManualClock(T0 - 5 * MS_PER_MINUTE)
    bars = two_symbol_bars(N, BTC_ROWS, ETH_ROWS)
    with EventStore(path) as store:
        sessions = SessionManager(store, clock, app_version="test")
        sessions.open()
        sessions.start_monitoring()
        demo = demo_detector(store, clock)
        clock.advance(5 * MS_PER_MINUTE)
        demo.on_bar(
            live(series([(1000, 1010, 999, 1008)], symbol="BTCUSDT")[0]),
            session_id=sessions.session_id,
        )
        svc = PaperTradingService.open(config(), store, strategy=scenario())
        drive(svc, bars, list(range(RESTART_AT)), missing={(13, "ETHUSDT")})
        demo.on_bar(
            live(series([(1000, 1000, 990, 992)], symbol="ETHUSDT")[0]),
            session_id=sessions.session_id,
        )
    # process restart: a new store handle, the session restored from its verified journal
    store = EventStore(path)
    svc = PaperTradingService.open(config(), store, strategy=scenario())
    drive(svc, bars, list(range(RESTART_AT, N)), missing={(13, "ETHUSDT")})
    svc2 = PaperTradingService.open(config2(), store, session_id="paper-2", strategy=scenario2())
    keys = [USDM_BTC, ETH]
    drive_keys(svc2, bars2(), keys, list(range(N, RESET_AFTER)), first=N)
    halted = svc2.session.sim.books["baseline"].halted
    assert halted == "drawdown_kill_switch", halted
    halted_head_seq = store.head[0]
    assert svc2.reset_halt("baseline", "tester", "eval export test", now_ms=minute(RESET_AFTER))
    drive_keys(svc2, bars2(), keys, list(range(RESET_AFTER, N + N2)), first=N)
    clock.advance(30 * MS_PER_MINUTE)
    sessions = SessionManager(store, clock, app_version="test")
    sessions.open()  # detects the unclosed first app session (crash record)
    sessions.start_monitoring()
    sessions.stop_monitoring("user")
    sessions.close("user")
    store.close()
    return Built(path, svc, svc2, clock, halted_head_seq)


__all__ = ["BTC", "ETH", "Built", "build_store", "config"]
