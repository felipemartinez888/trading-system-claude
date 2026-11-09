from __future__ import annotations

from pathlib import Path

import pytest
from md_helpers import MIN, T0, rest_kline

from tsclaude.contracts.market import Bar, BarSource, Interval, Market
from tsclaude.marketdata.convert import bar_from_rest
from tsclaude.store.eventstore import EventStore
from tsclaude.store.livebars import Checkpoint, LiveBarStore, StoreClosed, WriteOutcome


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


# ---------------------------------------------------------------------- write_many / checkpoints
def _trace_commits(store: LiveBarStore) -> list[str]:
    seen: list[str] = []
    store._conn.set_trace_callback(
        lambda stmt: seen.append(stmt) if stmt.strip().upper().startswith("COMMIT") else None
    )
    return seen


def test_write_many_is_one_commit_for_the_whole_batch(bars: LiveBarStore) -> None:
    commits = _trace_commits(bars)
    batch = [_bar(T0 + i * MIN, BarSource.REST_BACKFILL) for i in range(2_500)]
    results = bars.write_many(batch)
    assert len(commits) == 1  # a per-row write would have committed 2,500 times
    assert [r.outcome for r in results] == [WriteOutcome.INSERTED] * 2_500
    assert bars.count() == 2_500


def test_write_many_outcomes_equal_sequential_writes(tmp_path: Path) -> None:
    seed = [_bar(T0, BarSource.WS_LIVE), _bar(T0 + MIN, BarSource.REST_BACKFILL, close=100.6)]
    incoming = [
        _bar(T0, BarSource.REST_RECOVERED),  # identical market data -> DUPLICATE
        _bar(T0 + MIN, BarSource.REST_RECOVERED, close=100.7),  # differs, stored non-WS -> CONFLICT
        _bar(T0 + MIN, BarSource.WS_LIVE, close=100.8),  # WS replaces a non-WS row -> REPLACED
        _bar(T0 + 2 * MIN, BarSource.REST_BACKFILL),  # new -> INSERTED
    ]
    with LiveBarStore(tmp_path / "a.db") as one, LiveBarStore(tmp_path / "b.db") as many:
        for b in seed:
            one.write(b)
        many.write_many(seed)
        sequential = [one.write(b).outcome for b in incoming]
        batched = [r.outcome for r in many.write_many(incoming)]
        assert batched == sequential
        assert batched == [
            WriteOutcome.DUPLICATE,
            WriteOutcome.CONFLICT,
            WriteOutcome.REPLACED,
            WriteOutcome.INSERTED,
        ]
        assert many.range(Market.SPOT, "BTCUSDT", T0, T0 + 3 * MIN) == one.range(
            Market.SPOT, "BTCUSDT", T0, T0 + 3 * MIN
        )


def _checkpoint(cursor: int, rows: int = 10, status: str = "running") -> Checkpoint:
    return Checkpoint(
        job="startup",
        market=Market.SPOT,
        symbol="BTCUSDT",
        window_from_ms=T0,
        window_to_ms=T0 + 60 * MIN,
        cursor_ms=cursor,
        rows_written=rows,
        chunks_done=1,
        status=status,
        updated_at_ms=T0 + 61 * MIN,
    )


def test_checkpoint_commits_atomically_with_its_rows(bars: LiveBarStore) -> None:
    bars.write_many(
        [_bar(T0 + 59 * MIN, BarSource.REST_BACKFILL)], checkpoint=_checkpoint(T0 + 59 * MIN)
    )
    assert bars.get_checkpoint("startup", Market.SPOT, "BTCUSDT") == _checkpoint(T0 + 59 * MIN)

    # a failing batch stores neither its rows nor its checkpoint
    bad = _bar(T0 + 57 * MIN, BarSource.REST_BACKFILL).model_copy(
        update={"source": BarSource.DERIVED}
    )
    with pytest.raises(ValueError, match="derived"):
        bars.write_many(
            [_bar(T0 + 58 * MIN, BarSource.REST_BACKFILL), bad],
            checkpoint=_checkpoint(T0 + 58 * MIN),
        )
    assert bars.get(Market.SPOT, "BTCUSDT", T0 + 58 * MIN) is None
    cp = bars.get_checkpoint("startup", Market.SPOT, "BTCUSDT")
    assert cp is not None
    assert cp.cursor_ms == T0 + 59 * MIN


def test_rolled_back_batch_leaves_no_trace(
    bars: LiveBarStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: LiveBarStore, cp: Checkpoint) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(LiveBarStore, "_put_checkpoint", boom)
    with pytest.raises(RuntimeError, match="disk full"):
        bars.write_many([_bar(T0, BarSource.REST_BACKFILL)], checkpoint=_checkpoint(T0))
    assert bars.count() == 0
    monkeypatch.undo()
    assert bars.get_checkpoint("startup", Market.SPOT, "BTCUSDT") is None


def test_checkpoint_survives_reopen_and_set_checkpoint_updates_status(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with LiveBarStore(path) as store:
        store.set_checkpoint(_checkpoint(T0 + 30 * MIN))
        store.set_checkpoint(_checkpoint(T0 + 30 * MIN, status="cancelled"))
    with LiveBarStore(path) as store:
        cp = store.get_checkpoint("startup", Market.SPOT, "BTCUSDT")
        assert cp is not None
        assert (cp.status, cp.cursor_ms, cp.schema_version) == ("cancelled", T0 + 30 * MIN, 1)
        assert store.get_checkpoint("history", Market.SPOT, "BTCUSDT") is None


def test_closed_store_refuses_writes(tmp_path: Path) -> None:
    store = LiveBarStore(tmp_path / "state.db")
    store.close()
    store.close()  # idempotent
    assert store.closed
    with pytest.raises(StoreClosed):
        store.write_many([_bar(T0, BarSource.REST_BACKFILL)])
