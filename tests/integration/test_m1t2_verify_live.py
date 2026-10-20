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


MISSING = T0 + 5 * MS_PER_MINUTE


def _gap_event(ts: int) -> NewEvent:
    return NewEvent(
        type=EventType.GAP_DETECTED,
        aggregate_type=AggregateType.MARKET_DATA,
        aggregate_id="spot:BTCUSDT",
        ts_ms=ts,
        payload={
            "gap_id": "G1",
            "market": "spot",
            "symbol": "BTCUSDT",
            "interval": "1m",
            "from_ms": MISSING,
            "to_ms": MISSING + MS_PER_MINUTE,
            "minutes": 1,
            "cause": "disconnect",
            "detected_at_ms": ts,
        },
    )


def _seed(path: Path, *, gap: str) -> None:
    """gap: "none" (minute missing, no gap), "open" (missing, gap open), "resolved"."""
    start, stop = T0 + 10_000, T0 + 10 * MS_PER_MINUTE + 20_000
    with EventStore(path) as store, LiveBarStore(path) as bars:
        store.append(_ev(EventType.MONITORING_STARTED, start, {"monitoring_id": "M"}))
        stream = "spot:BTCUSDT:kline_1m"
        changes = (
            (start + 1_000, "HEALTHY"),
            (T0 + 5 * MS_PER_MINUTE, "DISCONNECTED"),
            (T0 + 6 * MS_PER_MINUTE, "HEALTHY"),
        )
        for ts, to in changes:
            store.append(
                _ev(EventType.STREAM_STATE_CHANGED, ts, {"stream": stream, "to": to}, stream)
            )
        for i in range(1, 10):
            m = T0 + i * MS_PER_MINUTE
            if m != MISSING:
                bars.write(_bar(m, BarSource.REST_RECOVERED if i == 6 else BarSource.WS_LIVE), "S")
        if gap in ("open", "resolved"):
            store.append(_gap_event(T0 + 7 * MS_PER_MINUTE))
        if gap == "resolved":
            bars.write(_bar(MISSING, BarSource.REST_RECOVERED), "S")
            store.append(
                NewEvent(
                    type=EventType.GAP_RESOLVED,
                    aggregate_type=AggregateType.MARKET_DATA,
                    aggregate_id="spot:BTCUSDT",
                    ts_ms=T0 + 8 * MS_PER_MINUTE,
                    payload={
                        "gap_id": "G1",
                        "market": "spot",
                        "symbol": "BTCUSDT",
                        "ranges": [[MISSING, MISSING + MS_PER_MINUTE]],
                        "minutes": 1,
                        "resolution": "recovered",
                        "source": "rest_recovered",
                        "remaining_minutes": 0,
                        "fully_resolved": True,
                        "resolved_at_ms": T0 + 8 * MS_PER_MINUTE,
                    },
                )
            )
        store.append(
            _ev(
                EventType.MONITORING_STOPPED, stop, {"monitoring_id": "M", "reason": "app_shutdown"}
            )
        )


FINAL = {
    "connections": [{"group": "spot", "reconnects": 1, "recycles": 0, "crashes": 0, "malformed": 2}]
}


def _report(
    verify: ModuleType, path: Path, *, budget_ms: int = 60_000, exit_code: int = 0
) -> dict[str, Any]:
    samples = [{"clock_offset_ms": v} for v in (3, -2, 7)]
    with EventStore(path) as store, LiveBarStore(path) as bars:
        report: dict[str, Any] = verify.build_report(
            store,
            bars,
            [(Market.SPOT, "BTCUSDT")],
            (T0, T0 + 11 * MS_PER_MINUTE),
            samples,
            FINAL,
            None,
            budget_ms,
            app_exit_code=exit_code,
        )
    return report


def test_report_pass_when_every_minute_is_stored(tmp_path: Path, verify: ModuleType) -> None:
    path = tmp_path / "state.db"
    _seed(path, gap="resolved")
    report = _report(verify, path)
    sym = report["bars"]["spot:BTCUSDT"]
    # minutes fully inside [T0+10 s, T0+10 min+20 s) minus the close allowance: T0+1 .. T0+9 min
    assert sym["expected_minutes"] == 9
    assert sym["stored"] == 9
    assert sym["by_source"] == {"ws_live": 7, "rest_recovered": 2}
    assert report["totals"] == {"reconnects": 1, "recycles": 0, "crashes": 0, "malformed": 2}
    assert report["clock_offset_ms"]["min"] == -2
    assert report["clock_offset_ms"]["max"] == 7
    assert report["gaps"]["detected"] == 1
    assert report["gaps"]["open"] == 0
    uptime = report["stream_uptime"]["spot:BTCUSDT:kline_1m"]
    assert 0.8 < uptime["healthy_ratio"] < 0.95
    assert report["verdict"] == {"pass": True, "failures": []}


def test_report_fails_on_gap_open_beyond_retry_budget(tmp_path: Path, verify: ModuleType) -> None:
    """B2: a still-open gap means recovery failed once the retry budget has passed."""
    path = tmp_path / "state.db"
    _seed(path, gap="open")
    report = _report(verify, path, budget_ms=60_000)  # detected 4 min before the end
    sym = report["bars"]["spot:BTCUSDT"]
    assert sym["missing_with_open_gap"] == 1
    assert sym["missing_unexplained_count"] == 0
    assert report["verdict"]["pass"] is False
    assert any("longer than the retry budget" in f for f in report["verdict"]["failures"])
    (overdue,) = report["gaps"]["open_beyond_retry_budget"]
    assert overdue["cause"] == "disconnect"


def test_report_tolerates_a_gap_still_within_its_retry_budget(
    tmp_path: Path, verify: ModuleType
) -> None:
    path = tmp_path / "state.db"
    _seed(path, gap="open")
    report = _report(verify, path, budget_ms=10 * MS_PER_MINUTE)
    assert report["gaps"]["open_beyond_retry_budget"] == []
    assert report["verdict"] == {"pass": True, "failures": []}


def test_report_fails_on_unexplained_minutes(tmp_path: Path, verify: ModuleType) -> None:
    path = tmp_path / "state.db"
    _seed(path, gap="none")
    report = _report(verify, path)
    sym = report["bars"]["spot:BTCUSDT"]
    assert sym["missing_unexplained_count"] == 1
    assert report["verdict"]["pass"] is False
    assert "1 expected minutes missing without an open gap" in report["verdict"]["failures"]


def test_report_fails_on_app_error_exit(tmp_path: Path, verify: ModuleType) -> None:
    path = tmp_path / "state.db"
    _seed(path, gap="resolved")
    report = _report(verify, path, exit_code=1)
    assert report["verdict"]["pass"] is False
    assert "exited with code 1" in report["verdict"]["failures"][0]


def test_retry_budget_from_config(verify: ModuleType) -> None:
    from tsclaude.marketdata.config import WatermarkConfig

    wm = WatermarkConfig(
        grace_s=5, check_interval_s=1, gap_retry_interval_s=60, gap_retry_max_attempts=5
    )
    assert verify.retry_budget_ms(wm) == 306_000


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
