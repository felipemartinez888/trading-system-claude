from __future__ import annotations

from pathlib import Path

import pytest
from md_helpers import MIN, T0, rest_kline

from tsclaude.contracts.market import Bar, BarSource, Interval, Market
from tsclaude.marketdata.convert import bar_from_rest
from tsclaude.store.eventstore import EventStore
from tsclaude.store.livebars import LiveBarStore, WriteOutcome


def _bar(open_ms: int, source: BarSource = BarSource.WS_LIVE, close: float = 100.5) -> Bar:
    src = BarSource.REST_RECOVERED if source is BarSource.WS_LIVE else source
    b = bar_from_rest(Market.SPOT, "BTCUSDT", rest_kline(open_ms, close=close), src, T0 + 1)
    return b.model_copy(update={"source": source}) if source is BarSource.WS_LIVE else b


def test_insert_get_range_last(bars: LiveBarStore) -> None:
    for i in range(3):
        assert bars.write(_bar(T0 + i * MIN), "S1").outcome is WriteOutcome.INSERTED
    got = bars.get(Market.SPOT, "BTCUSDT", T0)
    assert got is not None
    assert got.observed_session_id == "S1"
    assert got.bar == _bar(T0)
    assert [s.bar.open_time_ms for s in bars.range(Market.SPOT, "BTCUSDT", T0, T0 + 2 * MIN)] == [
        T0,
        T0 + MIN,
    ]
    last = bars.last(Market.SPOT, "BTCUSDT")
    assert last is not None
    assert last.bar.open_time_ms == T0 + 2 * MIN
    assert bars.first_open_time(Market.SPOT, "BTCUSDT") == T0
    assert bars.count() == 3
    assert bars.count(Market.USDM) == 0
    assert bars.last(Market.USDM, "BTCUSDT") is None


def test_identical_duplicate_is_noop_even_with_other_source(bars: LiveBarStore) -> None:
    bars.write(_bar(T0), "S1")
    again = bars.write(_bar(T0, BarSource.REST_RECOVERED), None)
    assert again.outcome is WriteOutcome.DUPLICATE
    stored = bars.get(Market.SPOT, "BTCUSDT", T0)
    assert stored is not None
    assert stored.bar.source is BarSource.WS_LIVE  # first provenance kept
    assert stored.observed_session_id == "S1"
    assert bars.count() == 1


def test_conflict_keeps_stored_unless_ws_replaces_rest(bars: LiveBarStore) -> None:
    bars.write(_bar(T0), "S1")
    res = bars.write(_bar(T0, BarSource.REST_RECOVERED, close=100.7), None)
    assert res.outcome is WriteOutcome.CONFLICT
    assert res.existing is not None
    assert res.existing.bar.close == 100.5
    stored = bars.get(Market.SPOT, "BTCUSDT", T0)
    assert stored is not None
    assert stored.bar.close == 100.5

    bars.write(_bar(T0 + MIN, BarSource.REST_RECOVERED), None)
    res2 = bars.write(_bar(T0 + MIN, BarSource.WS_LIVE, close=100.9), "S2")
    assert res2.outcome is WriteOutcome.REPLACED
    stored2 = bars.get(Market.SPOT, "BTCUSDT", T0 + MIN)
    assert stored2 is not None
    assert stored2.bar.close == 100.9
    assert stored2.bar.source is BarSource.WS_LIVE
    assert stored2.observed_session_id == "S2"


def test_rejects_non_1m_and_derived(bars: LiveBarStore) -> None:
    derived = _bar(T0).model_copy(update={"source": BarSource.DERIVED})
    with pytest.raises(ValueError, match="derived"):
        bars.write(derived)
    five = Bar.model_validate(
        {**_bar(T0).model_dump(), "interval": Interval.M5, "close_time_ms": T0 + 5 * MIN - 1}
    )
    with pytest.raises(ValueError, match="1m"):
        bars.write(five)


def test_shares_state_db_with_event_store(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with EventStore(db) as events, LiveBarStore(db) as lb:
        lb.write(_bar(T0), None)
        assert events.verify().ok
    with LiveBarStore(db) as reopened:
        assert reopened.count() == 1
