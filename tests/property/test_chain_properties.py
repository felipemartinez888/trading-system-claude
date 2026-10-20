from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tsclaude.contracts.events import AggregateType, EventType, NewEvent
from tsclaude.store.eventstore import EventStore

json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**53), max_value=2**53),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=20),
)
payloads = st.dictionaries(
    st.text(min_size=1, max_size=8),
    st.recursive(
        json_scalars,
        lambda c: st.lists(c, max_size=3) | st.dictionaries(st.text(max_size=5), c, max_size=3),
        max_leaves=8,
    ),
    max_size=5,
)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(payloads, min_size=1, max_size=15))
def test_any_payload_sequence_roundtrips_and_verifies(batch: list[dict[str, object]]) -> None:
    with tempfile.TemporaryDirectory() as d, EventStore(Path(d) / "s.db") as store:
        written = store.append_many(
            [
                NewEvent(
                    type=EventType.STREAM_STATE_CHANGED,
                    aggregate_type=AggregateType.STREAM,
                    aggregate_id="x",
                    ts_ms=i,
                    payload=p,
                )
                for i, p in enumerate(batch)
            ]
        )
        assert store.verify().ok
        assert [e.payload for e in store.read()] == [e.payload for e in written]


@settings(max_examples=30, deadline=None)
@given(n=st.integers(min_value=2, max_value=10), data=st.data())
def test_any_single_mutation_is_detected(n: int, data: st.DataObject) -> None:
    target = data.draw(st.integers(min_value=1, max_value=n))
    column = data.draw(st.sampled_from(["ts_ms", "aggregate_id", "payload", "type"]))
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "s.db"
        with EventStore(path) as store:
            store.append_many(
                [
                    NewEvent(
                        type=EventType.STREAM_STATE_CHANGED,
                        aggregate_type=AggregateType.STREAM,
                        aggregate_id="x",
                        ts_ms=i,
                        payload={"i": i},
                    )
                    for i in range(n)
                ]
            )
        new_value = {
            "ts_ms": 10_000,
            "aggregate_id": "tampered",
            "payload": '{"i":-1}',
            "type": EventType.GAP_DETECTED.value,
        }[column]
        conn = sqlite3.connect(path)
        conn.execute("DROP TRIGGER events_no_update")
        conn.execute(f"UPDATE events SET {column}=? WHERE seq=?", (new_value, target))
        conn.commit()
        conn.close()
        with EventStore(path) as store:
            report = store.verify()
        assert not report.ok
        assert report.first_bad_seq == target
