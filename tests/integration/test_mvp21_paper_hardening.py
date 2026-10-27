"""MVP-2.1 paper runtime hardening (H1-H5): freshness labels, atomic publication, state kept after
stop, fail-closed audit-log recovery, and loss detection with the real market-data `Subscription`.

Fake market data and synthetic bars only; results are provisional (WHEA investigation open).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from m1t2_fakes import make_bar
from test_mvp2_paper import KEYS, POLL, T0, Rig, paper_config, run

from tsclaude.api.paper import _values
from tsclaude.contracts.market import Market
from tsclaude.contracts.timeutil import MS_PER_MINUTE, ManualClock
from tsclaude.marketdata.bars import BarClosed
from tsclaude.marketdata.service import Subscription
from tsclaude.runtime.paper_runtime import (
    AUDIT_FILE,
    PaperActionRefused,
    PaperRuntime,
    PaperSettings,
    PaperState,
)
from tsclaude.store.eventstore import EventStore


def _books_label(rig: Rig) -> dict[str, Any]:
    snap, _ = rig.runtime.published()
    return _values(snap, rig.clock.now_ms(), {"books": snap.get("books")})


# ---------------------------------------------------------------------- H1
def test_h1_restored_books_are_stale_until_a_live_bar_then_age_out(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = Rig(tmp_path)
        await rig.supervisor.start("S1")
        for i in range(3):
            await rig.minute(i)
        await rig.supervisor.stop()
        await rig.supervisor.start("S2")  # restore from the journal
        label = _books_label(rig)
        assert label["provenance"] == "STALE"
        assert label["as_of_ms"] == T0 + 3 * MS_PER_MINUTE - 1  # last processed close
        assert "restored" in label["reason"]
        await rig.minute(3)  # first live bar after the restore
        label = _books_label(rig)
        assert label["provenance"] == "SIMULATED"
        assert label["as_of_ms"] == T0 + 4 * MS_PER_MINUTE - 1
        await rig.idle(T0 + 4 * MS_PER_MINUTE + 90_000 + 120_001)  # no bar for too long
        label = _books_label(rig)
        assert label["provenance"] == "STALE"
        assert "closed" in label["reason"]
        await rig.supervisor.close()

    run(scenario())


def test_h1_fresh_session_without_a_processed_bar_is_never_simulated(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = Rig(tmp_path)
        await rig.supervisor.start("S1")
        label = _books_label(rig)
        assert label["provenance"] == "UNAVAILABLE"  # no bar-backed value, no wall-clock stamp
        await rig.supervisor.close()

    run(scenario())


# ---------------------------------------------------------------------- H2
def test_h2_snapshot_and_time_are_published_together(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = Rig(tmp_path)
        await rig.supervisor.start("S1")
        first = rig.runtime.published()
        assert isinstance(first, tuple)
        assert rig.runtime.published() is first  # one object until the next publication
        rig.clock.advance(5_000)
        await rig.minute(0)
        second = rig.runtime.published()
        assert second is not first
        snap, at = second
        assert at == rig.clock.now_ms()
        assert snap["service"]["batches"] == 2  # the snapshot that goes with that time
        await rig.supervisor.close()

    run(scenario())


# ---------------------------------------------------------------------- H3
def test_h3_awaiting_backfill_reason_survives_monitoring_stop(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = Rig(tmp_path)
        await rig.supervisor.start("S1")
        for i in range(3):
            await rig.minute(i)
        await rig.supervisor.stop()
        await rig.supervisor.start("S2")
        await rig.minute(6)  # minutes 3-5 missing and nothing in the bar store
        assert rig.runtime.state is PaperState.AWAITING_BACKFILL
        await rig.supervisor.stop()
        assert rig.runtime.state is PaperState.IDLE
        assert "AWAITING_BACKFILL" in str(rig.runtime.reason)
        snap, _ = rig.runtime.published()
        previous = snap["previous_run"]
        assert previous["state"] == "AWAITING_BACKFILL"
        assert previous["awaiting_missing"]
        await rig.supervisor.close()

    run(scenario())


def test_h3_failure_states_are_not_erased_by_a_stop(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = Rig(tmp_path)
        await rig.supervisor.start("S1")
        await rig.minute(0)
        rig.paper_sub().dropped = 1  # type: ignore[attr-defined]
        await rig.minute(1)
        assert rig.runtime.state is PaperState.STOPPED_OVERFLOW
        await rig.supervisor.stop()
        assert rig.runtime.state is PaperState.STOPPED_OVERFLOW
        assert rig.runtime.published()[0]["previous_run"]["state"] == "STOPPED_OVERFLOW"
        await rig.supervisor.close()

    run(scenario())


# ---------------------------------------------------------------------- H4
def _record(new: str, previous: str, ts_ms: int, rid: str = "req-h4-0001") -> str:
    return json.dumps(
        {
            "v": 1,
            "ts_ms": ts_ms,
            "request_id": rid,
            "previous_session_id": previous,
            "new_session_id": new,
            "note": "test",
        },
        sort_keys=True,
    )


def test_h4_complete_record_missing_its_newline_is_repaired_when_ownership_is_proven(
    tmp_path: Path,
) -> None:
    audit = tmp_path / AUDIT_FILE
    original = _record("paper-x", "paper", T0 - 1).encode()
    audit.write_bytes(original)  # no trailing newline

    async def scenario() -> None:
        rig = Rig(tmp_path)
        await rig.supervisor.start("S1")
        assert rig.runtime.state is PaperState.RUNNING
        assert rig.runtime.session_id == "paper-x"
        assert rig.runtime.audit_warnings
        await rig.supervisor.close()

    run(scenario())
    data = audit.read_bytes()
    assert data.startswith(original + b"\n")  # original bytes preserved, then terminated
    lines = data.decode().splitlines()
    assert json.loads(lines[1])["kind"] == "repair"


def test_h4_repair_is_refused_when_journal_ownership_is_ambiguous(tmp_path: Path) -> None:
    async def first_run() -> None:
        rig = Rig(tmp_path)
        await rig.supervisor.start("S1")
        for i in range(3):
            await rig.minute(i)
        await rig.supervisor.close()

    run(first_run())
    audit = tmp_path / AUDIT_FILE
    # a switch stamped *before* the "paper" session's last steps: ownership is contradictory
    audit.write_bytes(_record("paper-x", "paper", T0 + MS_PER_MINUTE).encode())
    before = audit.read_bytes()

    async def second_run() -> None:
        rig = Rig(tmp_path)
        await rig.supervisor.start("S2")
        assert rig.runtime.state is PaperState.UNAVAILABLE
        assert "ownership" in str(rig.runtime.reason)
        await rig.supervisor.close()

    run(second_run())
    assert audit.read_bytes() == before  # nothing repaired or rewritten


def test_h4_unparseable_final_fragment_stays_unavailable_until_quarantined(
    tmp_path: Path,
) -> None:
    audit = tmp_path / AUDIT_FILE
    fragment = b'{"v": 1, "new_session_id": "paper-y", "ts_m'
    audit.write_bytes(fragment)

    async def blocked() -> None:
        rig = Rig(tmp_path)
        await rig.supervisor.start("S1")
        assert rig.runtime.state is PaperState.UNAVAILABLE
        assert "incomplete final audit record" in str(rig.runtime.reason)
        with pytest.raises(PaperActionRefused) as refused:
            rig.runtime.request_new_session("try anyway", "req-h4-0002")
        assert refused.value.code == 423
        await rig.supervisor.close()

    run(blocked())
    assert audit.read_bytes() == fragment  # the original bytes are preserved untouched
    quarantine = {
        "v": 1,
        "kind": "quarantine",
        "line": 1,
        "sha256": hashlib.sha256(fragment).hexdigest(),
        "active_session_id": "paper",
        "ts_ms": T0 - 1,
        "note": "operator: fragment inspected, no session was opened from it",
    }
    with audit.open("ab") as fh:  # the documented manual recovery
        fh.write(b"\n" + json.dumps(quarantine).encode() + b"\n")

    async def recovered() -> None:
        rig = Rig(tmp_path)
        await rig.supervisor.start("S2")
        assert rig.runtime.state is PaperState.RUNNING
        assert rig.runtime.session_id == "paper"
        await rig.minute(0)  # the kept session journals after the quarantine ...
        await rig.supervisor.stop()
        await rig.supervisor.start("S3")  # ... and still restores (review B1)
        assert rig.runtime.state is PaperState.RUNNING, rig.runtime.reason
        await rig.supervisor.close()

    run(recovered())
    assert audit.read_bytes().startswith(fragment + b"\n")


def test_h4_malformed_middle_line_and_unknown_journal_session_fail_closed(
    tmp_path: Path,
) -> None:
    audit = tmp_path / AUDIT_FILE
    audit.write_text("not json\n" + _record("paper-x", "paper", T0 - 1) + "\n", encoding="utf-8")

    async def malformed() -> None:
        rig = Rig(tmp_path)
        await rig.supervisor.start("S1")
        assert rig.runtime.state is PaperState.UNAVAILABLE
        assert "not quarantined" in str(rig.runtime.reason)
        await rig.supervisor.close()

    run(malformed())

    # a session journaled under an id the (lost) audit log no longer knows
    other = tmp_path / "other"
    other.mkdir()
    (other / AUDIT_FILE).write_text(_record("paper-z", "paper", T0 - 1) + "\n", encoding="utf-8")

    async def journal_unknown() -> None:
        rig = Rig(other)
        await rig.supervisor.start("S1")
        await rig.minute(0)
        await rig.supervisor.close()
        (other / AUDIT_FILE).rename(other / "lost.jsonl")
        rig2 = Rig(other)
        await rig2.supervisor.start("S2")
        assert rig2.runtime.state is PaperState.UNAVAILABLE
        assert "unknown to the log" in str(rig2.runtime.reason)
        await rig2.supervisor.close()

    run(journal_unknown())


def test_h4_unreadable_audit_log_never_crashes_and_fails_closed(tmp_path: Path) -> None:
    (tmp_path / AUDIT_FILE).mkdir()  # reading it raises an OSError

    async def scenario() -> None:
        rig = Rig(tmp_path)  # construction publishes: must not raise
        snap, _ = rig.runtime.published()
        assert snap["audit_log"]["ok"] is False
        await rig.supervisor.start("S1")
        assert rig.runtime.state is PaperState.UNAVAILABLE
        await rig.supervisor.stop()
        with pytest.raises(PaperActionRefused) as refused:
            rig.runtime.request_new_session("note", "req-h4-0003")
        assert refused.value.code == 423
        await rig.supervisor.close()

    run(scenario())


# ---------------------------------------------------------------------- H5 (real Subscription)
class _Owner:
    """Stands in for `MarketDataService` as the real `Subscription`'s owner; `stop()` mimics the
    service's order: clear `running`, then close every subscription."""

    def __init__(self) -> None:
        self._subscribers: set[Subscription] = set()
        self.running = True

    def subscribe(self, maxsize: int = 10_000) -> Subscription:
        sub = Subscription(self, maxsize)  # type: ignore[arg-type]
        self._subscribers.add(sub)
        return sub

    def stop(self) -> None:
        self.running = False
        for sub in list(self._subscribers):
            sub.close()


def _runtime(tmp_path: Path, **settings: Any) -> PaperRuntime:
    return PaperRuntime(
        PaperSettings(enabled=True, poll_s=POLL, **settings),
        EventStore(tmp_path / "state.db"),
        ManualClock(T0 + MS_PER_MINUTE + 1_500),
        config_factory=paper_config,
        audit_path=tmp_path / AUDIT_FILE,
    )


async def _start(rt: PaperRuntime, owner: _Owner, sub: Subscription) -> None:
    await rt.start(sub, snapshot=lambda: None, market_running=lambda: owner.running)


def test_h5_real_subscription_closed_by_service_stop_is_detected_in_one_poll(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        owner, rt = _Owner(), _runtime(tmp_path)
        sub = owner.subscribe()
        await _start(rt, owner, sub)
        for m, s in KEYS:
            sub._put(BarClosed(make_bar(m, s, T0)))
        await asyncio.sleep(POLL * 10)  # several normal timeouts while running: no loss
        assert rt.state is PaperState.RUNNING
        owner.stop()
        await asyncio.sleep(POLL * 3)
        assert rt.state is PaperState.SUBSCRIPTION_LOST
        assert "market data stopped" in str(rt.reason)
        await rt.stop()

    run(scenario())


def test_h5_real_subscription_overflow_stops_paper(tmp_path: Path) -> None:
    async def scenario() -> None:
        owner, rt = _Owner(), _runtime(tmp_path)
        sub = owner.subscribe(maxsize=1)
        await _start(rt, owner, sub)
        bar = make_bar(Market.SPOT, "BTCUSDT", T0)
        sub._put(BarClosed(bar))
        sub._put(BarClosed(bar))  # queue full: the real class counts a drop
        assert sub.dropped == 1
        await asyncio.sleep(POLL * 5)
        assert rt.state is PaperState.STOPPED_OVERFLOW
        await rt.stop()

    run(scenario())


def test_h5_foreign_close_while_running_is_caught_by_the_watchdog(tmp_path: Path) -> None:
    async def scenario() -> None:
        owner, rt = _Owner(), _runtime(tmp_path, silence_s=POLL * 5)
        sub = owner.subscribe()
        await _start(rt, owner, sub)
        sub.close()  # nobody in production does this while the service runs
        await asyncio.sleep(POLL * 20)
        assert rt.state is PaperState.SUBSCRIPTION_LOST
        assert "no market-data update" in str(rt.reason)
        await rt.stop()

    run(scenario())


# ---------------------------------------------------------------------- review and audit fixes
def test_quarantine_without_a_timestamp_is_not_accepted(tmp_path: Path) -> None:
    audit = tmp_path / AUDIT_FILE
    fragment = b'{"v": 1, "new_session_id": "pap'
    q = {
        "v": 1,
        "kind": "quarantine",
        "line": 1,
        "active_session_id": "paper",
        "note": "x",
        "sha256": hashlib.sha256(fragment).hexdigest(),
    }  # no ts_ms
    audit.write_bytes(fragment + b"\n" + json.dumps(q).encode() + b"\n")

    async def scenario() -> None:
        rig = Rig(tmp_path)
        await rig.supervisor.start("S1")
        assert rig.runtime.state is PaperState.UNAVAILABLE
        await rig.supervisor.close()

    run(scenario())


def test_new_session_is_refused_while_the_consumer_is_alive_awaiting_backfill(
    tmp_path: Path,
) -> None:
    """Audit B1: in AWAITING_BACKFILL the old session can still resume; no switch until stopped."""

    async def scenario() -> None:
        rig = Rig(tmp_path)
        await rig.supervisor.start("S1")
        for i in range(3):
            await rig.minute(i)
        await rig.supervisor.stop()
        await rig.supervisor.start("S2")
        await rig.minute(6)
        assert rig.runtime.state is PaperState.AWAITING_BACKFILL
        before = (tmp_path / AUDIT_FILE).exists()
        with pytest.raises(PaperActionRefused) as refused:
            rig.runtime.request_new_session("switch while waiting", "req-aud-0001")
        assert refused.value.code == 409
        assert (tmp_path / AUDIT_FILE).exists() == before  # nothing written
        await rig.supervisor.close()

    run(scenario())


def test_ownership_uses_the_store_sequence_recorded_with_the_switch(tmp_path: Path) -> None:
    """Audit N3: a step of the superseded session written after the switch is caught by the
    journal sequence even when its bar close (exchange time) predates the switch's wall time."""
    from tsclaude.contracts.events import AggregateType, EventType, NewEvent

    async def scenario() -> None:
        rig = Rig(tmp_path)
        await rig.supervisor.start("S1")
        await rig.minute(0)
        await rig.supervisor.stop()
        rig.clock.advance(3_600_000)
        record = rig.runtime.request_new_session("clean start", "req-seq-0001")
        assert record["store_seq"] == rig.store.head[0]
        # a late "paper" step whose bar closed long before the switch wall time
        rig.store.append(
            NewEvent(
                type=EventType.PAPER_STEP,
                aggregate_type=AggregateType.SIM,
                aggregate_id="paper",
                ts_ms=T0,
                payload={"kind": "probe"},
            )
        )
        await rig.supervisor.start("S2")
        assert rig.runtime.state is PaperState.UNAVAILABLE
        assert "after it was superseded" in str(rig.runtime.reason)
        await rig.supervisor.close()

    run(scenario())


def test_absurd_audit_line_never_crashes_the_app(tmp_path: Path) -> None:
    """Review N1: a line Python's json refuses with ValueError (huge integer) is just invalid."""
    (tmp_path / AUDIT_FILE).write_text('{"x": ' + "9" * 5000 + "}\n", encoding="utf-8")

    async def scenario() -> None:
        rig = Rig(tmp_path)  # construction publishes: must not raise
        assert rig.runtime.published()[0]["audit_log"]["ok"] is False
        await rig.supervisor.start("S1")
        assert rig.runtime.state is PaperState.UNAVAILABLE
        await rig.supervisor.close()

    run(scenario())


def test_whitespace_tail_is_repaired_and_does_not_block_new_sessions(tmp_path: Path) -> None:
    """Review N3."""
    audit = tmp_path / AUDIT_FILE
    audit.write_bytes(b"  ")

    async def scenario() -> None:
        rig = Rig(tmp_path)
        await rig.supervisor.start("S1")
        assert rig.runtime.state is PaperState.RUNNING
        await rig.supervisor.stop()
        record = rig.runtime.request_new_session("after repair", "req-ws-0001")
        assert record["created"] is True
        await rig.supervisor.close()

    run(scenario())
    assert audit.read_bytes().startswith(b"  \n")


def test_a_second_stop_keeps_the_previous_run(tmp_path: Path) -> None:
    """Review N5."""

    async def scenario() -> None:
        rig = Rig(tmp_path)
        await rig.supervisor.start("S1")
        for i in range(3):
            await rig.minute(i)
        await rig.supervisor.stop()
        await rig.supervisor.start("S2")
        await rig.minute(6)
        await rig.supervisor.stop()
        await rig.supervisor.stop()
        await rig.supervisor.close()
        assert rig.runtime.published()[0]["previous_run"]["state"] == "AWAITING_BACKFILL"

    run(scenario())
