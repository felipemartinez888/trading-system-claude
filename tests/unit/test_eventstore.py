from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tsclaude.contracts.events import GENESIS_HASH, AggregateType, EventType, NewEvent
from tsclaude.store.eventstore import EventStore


def _ev(i: int, payload: dict[str, object] | None = None) -> NewEvent:
    return NewEvent(
        type=EventType.STREAM_STATE_CHANGED,
        aggregate_type=AggregateType.STREAM,
        aggregate_id="spot:BTCUSDT",
        ts_ms=1_000 + i,
        payload=payload if payload is not None else {"i": i, "state": "HEALTHY"},
    )


@pytest.fixture
def store(tmp_path: Path) -> EventStore:
    s = EventStore(tmp_path / "state.db")
    yield s
    s.close()


def test_empty_store_verifies(store: EventStore) -> None:
    report = store.verify()
    assert report.ok
    assert report.events_checked == 0
    assert store.head == (0, GENESIS_HASH)


def test_append_links_chain(store: EventStore) -> None:
    a = store.append(_ev(1))
    b = store.append(_ev(2))
    assert (a.seq, b.seq) == (1, 2)
    assert a.prev_hash == GENESIS_HASH
    assert b.prev_hash == a.hash
    assert store.verify().ok


def test_append_many_is_atomic_and_ordered(store: EventStore) -> None:
    events = store.append_many([_ev(i) for i in range(5)])
    assert [e.seq for e in events] == [1, 2, 3, 4, 5]
    # NaN is rejected by canonical JSON -> nothing at all is written
    with pytest.raises(ValueError, match="JSON compliant"):
        store.append_many([_ev(6), _ev(7, {"x": float("nan")})])
    assert store.count() == 5
    assert store.verify().ok


def test_events_are_append_only_at_db_level(store: EventStore) -> None:
    store.append(_ev(1))
    conn = sqlite3.connect(store.path)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE events SET payload='{}' WHERE seq=1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM events WHERE seq=1")
    conn.close()


def test_verify_detects_tampering(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with EventStore(path) as s:
        s.append_many([_ev(i) for i in range(3)])
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER events_no_update")  # an attacker bypassing the triggers
    conn.execute("""UPDATE events SET payload='{"i":99,"state":"HEALTHY"}' WHERE seq=2""")
    conn.commit()
    conn.close()
    with EventStore(path) as s:
        report = s.verify()
    assert not report.ok
    assert report.first_bad_seq == 2
    assert report.reason == "hash mismatch"


def test_verify_detects_non_canonical_payload(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with EventStore(path) as s:
        s.append(_ev(1))
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER events_no_update")
    conn.execute("""UPDATE events SET payload='{"state": "HEALTHY", "i": 1}' WHERE seq=1""")
    conn.commit()
    conn.close()
    with EventStore(path) as s:
        assert s.verify().reason == "payload is not canonical"


def test_reopen_continues_chain(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with EventStore(path) as s:
        first = s.append(_ev(1))
    with EventStore(path) as s:
        second = s.append(_ev(2))
        assert second.prev_hash == first.hash
        assert s.verify().ok


def test_read_filters(store: EventStore) -> None:
    store.append_many([_ev(i) for i in range(3)])
    store.append(
        NewEvent(
            type=EventType.SESSION_STARTED,
            aggregate_type=AggregateType.SESSION,
            aggregate_id="s1",
            ts_ms=5,
            payload={"session_id": "s1"},
        )
    )
    assert len(store.read(types=[EventType.SESSION_STARTED])) == 1
    assert len(store.read(aggregate=(AggregateType.STREAM, "spot:BTCUSDT"))) == 3
    assert store.read(limit=1, descending=True)[0].seq == 4
    assert [e.seq for e in store.read(after_seq=2)] == [3, 4]


def test_runtime_state_is_mutable_and_outside_chain(store: EventStore) -> None:
    store.set_state("heartbeat", "a")
    store.set_state("heartbeat", "b")
    assert store.get_state("heartbeat") == "b"
    assert store.count() == 0
