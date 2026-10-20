"""Demo detector: WATCH only, ws_live only, fail closed on unhealthy streams, DEMO-labelled."""

from __future__ import annotations

from pathlib import Path

import pytest

from tsclaude.contracts.events import EventType
from tsclaude.contracts.market import Bar, BarSource, Interval, Market
from tsclaude.contracts.signals import SignalRecord, SignalStatus
from tsclaude.contracts.timeutil import MS_PER_MINUTE, ManualClock
from tsclaude.signals.demo import DemoDetector, DemoDetectorConfig
from tsclaude.store.eventstore import EventStore

T0 = 1_792_411_200_000  # minute aligned


def _bar(open_: float, close: float, source: BarSource = BarSource.WS_LIVE, i: int = 0) -> Bar:
    t = T0 + i * MS_PER_MINUTE
    return Bar(
        market=Market.USDM,
        symbol="ETHUSDT",
        interval=Interval.M1,
        open_time_ms=t,
        close_time_ms=t + MS_PER_MINUTE - 1,
        open=open_,
        high=max(open_, close) + 0.5,
        low=min(open_, close) - 0.5,
        close=close,
        volume=1.0,
        quote_volume=100.0,
        trades=3,
        taker_buy_volume=0.5,
        taker_buy_quote_volume=50.0,
        source=source,
        ingested_at_ms=t + MS_PER_MINUTE,
    )


@pytest.fixture
def store(tmp_path: Path):  # type: ignore[no-untyped-def]
    s = EventStore(tmp_path / "state.db")
    yield s
    s.close()


def _detector(store: EventStore, healthy: bool = True, **cfg: object) -> DemoDetector:
    clock = ManualClock(T0 + 2 * MS_PER_MINUTE)
    return DemoDetector(
        DemoDetectorConfig(**cfg),  # type: ignore[arg-type]
        store.append,
        lambda m, s: (healthy, None if healthy else "kline_1m=STALE"),
        clock.now_ms,
    )


def test_emits_watch_above_threshold_only(store: EventStore) -> None:
    det = _detector(store, threshold=0.005)
    assert det.on_bar(_bar(100, 100.4), session_id="S") is None  # 0.4 %
    event = det.on_bar(_bar(100, 99.0, i=1), session_id="S")  # -1 %
    assert event is not None
    assert event.type is EventType.SIGNAL_CREATED
    record = SignalRecord.model_validate(
        {k: v for k, v in event.payload.items() if k in SignalRecord.model_fields}
    )
    assert record.stage is SignalStatus.WATCH
    assert record.strategy_id == "demo_detector"
    assert record.evidence_grade == "X"
    assert record.run_mode == "LIVE"
    assert record.side == "short"
    assert record.decision_time_ms == T0 + 2 * MS_PER_MINUTE - 1
    assert record.created_at_ms >= record.decision_time_ms
    assert event.payload["demo"] is True
    assert "not_computed" in record.market_conditions


@pytest.mark.parametrize("source", [BarSource.REST_RECOVERED, BarSource.REST_BACKFILL])
def test_never_on_late_or_backfilled_bars(store: EventStore, source: BarSource) -> None:
    det = _detector(store)
    assert det.on_bar(_bar(100, 110, source=source), session_id="S") is None
    assert det.stats.skipped_not_live == 1
    assert store.read() == []


def test_fail_closed_when_streams_unhealthy(store: EventStore) -> None:
    det = _detector(store, healthy=False)
    assert det.on_bar(_bar(100, 110), session_id="S") is None
    assert det.stats.skipped_unhealthy == 1
    assert det.stats.last_skip_reason == "kline_1m=STALE"
    assert store.read() == []


def test_never_confirmed_never_sized(store: EventStore) -> None:
    det = _detector(store, threshold=0.001)
    for i in range(20):
        det.on_bar(_bar(100, 100 + (i % 5 + 1) * (-1) ** i, i=i), session_id="S")
    events = store.read(types=[EventType.SIGNAL_CREATED])
    assert events
    for e in events:
        assert e.payload["stage"] == "WATCH"
        assert "size" not in e.payload
        assert "quantity" not in e.payload
        assert e.payload["entry_rule"]["kind"] == "market_next_open"


def test_disabled_does_nothing(store: EventStore) -> None:
    det = _detector(store, enabled=False)
    assert det.on_bar(_bar(100, 120), session_id="S") is None
    assert det.stats.bars_seen == 0


def test_config_validation() -> None:
    with pytest.raises(ValueError, match="threshold"):
        DemoDetectorConfig(threshold=0)
