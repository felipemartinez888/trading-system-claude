"""`scripts/verify_live.py`: report logic on synthetic events, and the unattended run path."""

from __future__ import annotations

import importlib.util
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from m1t2_fakes import FakeFactory

from tsclaude.api import server
from tsclaude.api.process import bind_loopback
from tsclaude.contracts.events import AggregateType, EventType, NewEvent
from tsclaude.contracts.market import Bar, BarSource, Interval, Market
from tsclaude.contracts.timeutil import MS_PER_MINUTE
from tsclaude.runtime.settings import Settings
from tsclaude.store.eventstore import EventStore
from tsclaude.store.livebars import LiveBarStore

T0 = 1_792_411_200_000  # minute aligned
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify_live.py"


@pytest.fixture(scope="module")
def verify() -> ModuleType:
    spec = importlib.util.spec_from_file_location("m1t2_verify_live", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["m1t2_verify_live"] = module
    spec.loader.exec_module(module)
    return module


def _ev(t: EventType, ts: int, payload: dict[str, Any], agg: str = "S") -> NewEvent:
    return NewEvent(
        type=t, aggregate_type=AggregateType.SESSION, aggregate_id=agg, ts_ms=ts, payload=payload
    )


def _bar(open_ms: int, source: BarSource = BarSource.WS_LIVE) -> Bar:
    return Bar(
        market=Market.SPOT,
        symbol="BTCUSDT",
        interval=Interval.M1,
        open_time_ms=open_ms,
        close_time_ms=open_ms + MS_PER_MINUTE - 1,
        open=1,
        high=2,
        low=0.5,
        close=1.5,
        volume=1,
        quote_volume=1,
        trades=1,
        taker_buy_volume=0.5,
        taker_buy_quote_volume=0.5,
        source=source,
        ingested_at_ms=open_ms + MS_PER_MINUTE,
    )


def _seed(path: Path, *, gap_for_missing: bool) -> None:
    start, stop = T0 + 10_000, T0 + 10 * MS_PER_MINUTE + 20_000
    with EventStore(path) as store, LiveBarStore(path) as bars:
        store.append(_ev(EventType.MONITORING_STARTED, start, {"monitoring_id": "M"}))
        stream = "spot:BTCUSDT:kline_1m"
        for ts, to in ((start + 1_000, "HEALTHY"), (T0 + 5 * MS_PER_MINUTE, "DISCONNECTED")):
            store.append(
                _ev(EventType.STREAM_STATE_CHANGED, ts, {"stream": stream, "to": to}, stream)
            )
        store.append(
            _ev(
                EventType.STREAM_STATE_CHANGED,
                T0 + 6 * MS_PER_MINUTE,
                {"stream": stream, "to": "HEALTHY"},
                stream,
            )
        )
        missing = T0 + 5 * MS_PER_MINUTE
        for i in range(1, 10):
            m = T0 + i * MS_PER_MINUTE
            if m != missing:
                bars.write(_bar(m, BarSource.REST_RECOVERED if i == 6 else BarSource.WS_LIVE), "S")
        if gap_for_missing:
            store.append(
                NewEvent(
                    type=EventType.GAP_DETECTED,
                    aggregate_type=AggregateType.MARKET_DATA,
                    aggregate_id="spot:BTCUSDT",
                    ts_ms=T0 + 7 * MS_PER_MINUTE,
                    payload={
                        "gap_id": "G1",
                        "market": "spot",
                        "symbol": "BTCUSDT",
                        "interval": "1m",
                        "from_ms": missing,
                        "to_ms": missing + MS_PER_MINUTE,
                        "minutes": 1,
                        "cause": "disconnect",
                        "detected_at_ms": T0 + 7 * MS_PER_MINUTE,
                    },
                )
            )
        store.append(
            _ev(
                EventType.MONITORING_STOPPED, stop, {"monitoring_id": "M", "reason": "app_shutdown"}
            )
        )


@pytest.mark.parametrize("gap_for_missing", [True, False])
def test_report(tmp_path: Path, verify: ModuleType, gap_for_missing: bool) -> None:
    path = tmp_path / "state.db"
    _seed(path, gap_for_missing=gap_for_missing)
    samples = [{"clock_offset_ms": v} for v in (3, -2, 7)]
    final = {
        "connections": [
            {"group": "spot", "reconnects": 1, "recycles": 0, "crashes": 0, "malformed": 2}
        ]
    }
    with EventStore(path) as store, LiveBarStore(path) as bars:
        report = verify.build_report(
            store,
            bars,
            [(Market.SPOT, "BTCUSDT")],
            (T0, T0 + 11 * MS_PER_MINUTE),
            samples,
            final,
            None,
        )
    sym = report["bars"]["spot:BTCUSDT"]
    # minutes fully inside [T0+10 s, T0+10 min+20 s) minus the close allowance: T0+1 .. T0+9 min
    assert sym["expected_minutes"] == 9
    assert sym["stored"] == 8
    assert sym["by_source"] == {"ws_live": 7, "rest_recovered": 1}
    assert report["totals"] == {"reconnects": 1, "recycles": 0, "crashes": 0, "malformed": 2}
    assert report["clock_offset_ms"]["min"] == -2
    assert report["clock_offset_ms"]["max"] == 7
    uptime = report["stream_uptime"]["spot:BTCUSDT:kline_1m"]
    assert 0.8 < uptime["healthy_ratio"] < 0.95
    if gap_for_missing:
        assert sym["missing_unexplained"] == []
        assert report["gaps"]["open_by_cause"] == {"disconnect": 1}
        assert report["verdict"] == {"pass": True, "failures": []}
    else:
        assert len(sym["missing_unexplained"]) == 1
        assert report["verdict"]["pass"] is False
        assert "without an open gap" in report["verdict"]["failures"][0]


# ---------------------------------------------------------------------- unattended run
@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    for handler in root.handlers[:]:
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)


def test_unattended_run_autostarts_samples_and_quits(tmp_path: Path, verify: ModuleType) -> None:
    sock = bind_loopback("127.0.0.1", 0)
    port = int(sock.getsockname()[1])
    sock.close()
    settings = Settings(data_dir=tmp_path / "data", port=port, open_browser=False)
    observer = verify.Observer(sample_interval_s=0.2)
    factory = FakeFactory()
    code = server.run(
        settings,
        server.RunOptions(
            autostart_monitoring=True, run_for_s=0.9, observer=observer, service_factory=factory
        ),
    )
    assert code == server.EXIT_OK
    assert observer.start_error is None
    assert len(observer.samples) >= 3
    assert observer.samples[0]["running"] is True
    assert observer.final is not None
    assert observer.final["connections"][0]["group"] == "spot"
    with EventStore(settings.state_db_path) as store:
        types = [e.type for e in store.read()]
        stopped = store.read(types=[EventType.SESSION_STOPPED])
    assert types[1:3] == [EventType.MONITORING_STARTED, EventType.MONITORING_STOPPED]
    assert stopped[-1].payload["reason"] == "run_complete"


def test_unattended_run_reports_start_failure(tmp_path: Path, verify: ModuleType) -> None:
    sock = bind_loopback("127.0.0.1", 0)
    port = int(sock.getsockname()[1])
    sock.close()
    settings = Settings(data_dir=tmp_path / "data", port=port, open_browser=False)
    observer = verify.Observer(sample_interval_s=0.2)
    code = server.run(
        settings,
        server.RunOptions(
            autostart_monitoring=True,
            run_for_s=0.3,
            observer=observer,
            service_factory=FakeFactory(fail_start="no route to api.binance.com"),
        ),
    )
    assert code == server.EXIT_OK  # the app itself shut down cleanly
    assert "no route to api.binance.com" in observer.start_error
    with EventStore(settings.state_db_path) as store:
        assert EventType.MONITORING_STARTED not in [e.type for e in store.read()]
