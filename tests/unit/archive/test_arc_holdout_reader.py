"""Holdout guard (ADR-0009) and the research lake reader."""

from __future__ import annotations

from pathlib import Path

import pytest
from arc_helpers import MIN, ms, rest_klines

from tsclaude.contracts.events import EventType
from tsclaude.contracts.market import BarSource, Market
from tsclaude.store.eventstore import EventStore
from tsclaude.store.holdout import (
    HOLDOUT_END_MS,
    HOLDOUT_START_MS,
    EventStoreUnlocks,
    HoldoutAlreadyUnlocked,
    HoldoutContractMissing,
    HoldoutError,
    HoldoutGuard,
    HoldoutLocked,
    SealedUnlocks,
    UnlockRecord,
)
from tsclaude.store.lake import Lake, LakeKind
from tsclaude.store.lake_reader import LakeReader

SYM = "BTCUSDT"


class MemoryUnlocks:
    """Same semantics as EventStoreUnlocks (one unlock per prereg), without the event store."""

    def __init__(self) -> None:
        self.records: dict[str, UnlockRecord] = {}

    def find(self, prereg_id: str) -> UnlockRecord | None:
        return self.records.get(prereg_id)

    def unlock(
        self, prereg_id: str, strategy_hash: str, *, reason: str, now_ms: int
    ) -> UnlockRecord:
        if prereg_id in self.records:
            raise HoldoutAlreadyUnlocked(prereg_id)
        rec = UnlockRecord(prereg_id, strategy_hash, now_ms, reason)
        self.records[prereg_id] = rec
        return rec


def _row(k, source: str = "archive"):  # type: ignore[no-untyped-def]
    return {
        "open_time_ms": k.open_time_ms,
        "close_time_ms": k.close_time_ms,
        "open": k.open,
        "high": k.high,
        "low": k.low,
        "close": k.close,
        "volume": k.volume,
        "quote_volume": k.quote_volume,
        "trades": k.trades,
        "taker_buy_volume": k.taker_buy_volume,
        "taker_buy_quote_volume": k.taker_buy_quote_volume,
        "source": source,
        "ingested_at_ms": 1,
    }


def _dataset(
    lake: Lake, start: int, end: int, source: str = "archive", tweak: dict[int, float] | None = None
) -> str:
    rows = []
    for k in rest_klines(start, end):
        r = _row(k, source)
        if tweak and k.open_time_ms in tweak:
            r["close"] = tweak[k.open_time_ms]
            r["high"] = max(r["high"], r["close"])
        rows.append(r)
    parts = lake.write_parts(Market.SPOT, LakeKind.BARS_1M, SYM, rows)
    dsid, _ = lake.commit_dataset(parts, {"market": "spot", "kind": "bars_1m", "symbol": SYM}, None)
    return dsid


def test_holdout_constants_match_protocol() -> None:
    assert ms(2026, 1, 1) == HOLDOUT_START_MS
    assert ms(2026, 10, 1) == HOLDOUT_END_MS  # last holdout day 2026-09-30


def test_guard_refuses_holdout_and_serves_warmup() -> None:
    guard = HoldoutGuard(MemoryUnlocks())
    guard.check(ms(2025, 6), HOLDOUT_START_MS, purpose="research")  # warm-up up to the start
    guard.check(HOLDOUT_END_MS, ms(2026, 11), purpose="research")  # after the holdout
    for start, end in [
        (ms(2025, 12, 31), HOLDOUT_START_MS + 1),  # one ms into the holdout
        (ms(2026, 3), ms(2026, 4)),
        (ms(2026, 9, 30, 23, 59), HOLDOUT_END_MS),
        (ms(2025, 1), ms(2027, 1)),  # spanning it
    ]:
        with pytest.raises(HoldoutLocked):
            guard.check(start, end, purpose="research")
    with pytest.raises(HoldoutError, match="unknown read purpose"):
        guard.check(ms(2024, 1), ms(2024, 2), purpose="peek")


def test_unlock_exactly_once_per_prereg_and_only_for_its_strategy() -> None:
    guard = HoldoutGuard(MemoryUnlocks())
    with pytest.raises(HoldoutLocked):
        guard.check(
            ms(2026, 2), ms(2026, 3), purpose="research", prereg_id="P1", strategy_hash="S1"
        )
    guard.unlock("P1", "S1", reason="validation passed", now_ms=ms(2026, 10, 20))
    guard.check(ms(2026, 2), ms(2026, 3), purpose="research", prereg_id="P1", strategy_hash="S1")
    with pytest.raises(HoldoutLocked, match="not S2"):
        guard.check(
            ms(2026, 2), ms(2026, 3), purpose="research", prereg_id="P1", strategy_hash="S2"
        )
    with pytest.raises(HoldoutAlreadyUnlocked):
        guard.unlock("P1", "S2", reason="tweaked", now_ms=ms(2026, 10, 21))
    with pytest.raises(HoldoutLocked):
        guard.check(
            ms(2026, 2), ms(2026, 3), purpose="research", prereg_id="P2", strategy_hash="S1"
        )
    with pytest.raises(ValueError, match="required"):
        guard.unlock("P3", "S3", reason="", now_ms=0)


def test_sealed_registry_never_unlocks() -> None:
    guard = HoldoutGuard(SealedUnlocks())
    with pytest.raises(HoldoutError):
        guard.unlock("P1", "S1", reason="x", now_ms=0)


@pytest.mark.skipif(hasattr(EventType, "HOLDOUT_UNLOCKED"), reason="contract change applied")
def test_event_store_unlocks_fail_closed_until_contract_exists(tmp_path: Path) -> None:
    with EventStore(tmp_path / "state.db") as store:
        unlocks = EventStoreUnlocks(store)
        assert unlocks.find("P1") is None
        with pytest.raises(HoldoutContractMissing):
            HoldoutGuard(unlocks).unlock("P1", "S1", reason="x", now_ms=1)
        with pytest.raises(HoldoutLocked):
            HoldoutGuard(unlocks).check(
                ms(2026, 2), ms(2026, 3), purpose="research", prereg_id="P1", strategy_hash="S1"
            )


@pytest.mark.skipif(not hasattr(EventType, "HOLDOUT_UNLOCKED"), reason="needs contract change")
def test_event_store_unlocks_once_and_survive_restart(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with EventStore(db) as store:
        guard = HoldoutGuard(EventStoreUnlocks(store))
        guard.unlock("P1", "S1", reason="validation passed", now_ms=ms(2026, 10, 20))
        with pytest.raises(HoldoutAlreadyUnlocked):
            guard.unlock("P1", "S2", reason="again", now_ms=ms(2026, 10, 21))
        assert store.verify().ok
    with EventStore(db) as store:
        guard = HoldoutGuard(EventStoreUnlocks(store))
        guard.check(
            ms(2026, 2), ms(2026, 3), purpose="research", prereg_id="P1", strategy_hash="S1"
        )


def test_reader_returns_bars_with_source_and_missing(lake: Lake) -> None:
    dsid = _dataset(lake, ms(2025, 12, 31, 23, 50), HOLDOUT_START_MS)
    reader = LakeReader(lake, HoldoutGuard(MemoryUnlocks()))
    res = reader.read_bars(
        Market.SPOT, SYM, ms(2025, 12, 31, 23, 40), HOLDOUT_START_MS, purpose="research"
    )
    assert len(res.items) == 10
    assert all(b.source is BarSource.ARCHIVE for b in res.items)
    assert res.dataset_ids == (dsid,)
    assert res.missing == [(ms(2025, 12, 31, 23, 40), ms(2025, 12, 31, 23, 50))]
    with pytest.raises(HoldoutLocked):
        reader.read_bars(
            Market.SPOT, SYM, ms(2025, 12, 31, 23, 40), HOLDOUT_START_MS + MIN, purpose="research"
        )


def test_reader_guard_runs_before_any_io(lake: Lake) -> None:
    reader = LakeReader(lake, HoldoutGuard(MemoryUnlocks()))
    with pytest.raises(HoldoutLocked):
        reader.read_funding(SYM, ms(2026, 1), ms(2026, 2), purpose="research")
    with pytest.raises(HoldoutLocked):
        reader.read_mark_price_1m(SYM, ms(2026, 1), ms(2026, 2), purpose="research")


def test_overlapping_datasets_merge_conflicts_and_pinning(lake: Lake) -> None:
    t0 = ms(2025, 3)
    a = _dataset(lake, t0, t0 + 10 * MIN, "rest_backfill", tweak={t0 + 5 * MIN: 999.0})
    b = _dataset(lake, t0 + 5 * MIN, t0 + 20 * MIN, "archive")
    reader = LakeReader(lake, HoldoutGuard(MemoryUnlocks()))
    res = reader.read_bars(Market.SPOT, SYM, t0, t0 + 20 * MIN, purpose="research")
    assert [x.open_time_ms for x in res.items] == [t0 + i * MIN for i in range(20)]
    assert set(res.dataset_ids) == {a, b}
    assert len(res.conflicts) == 1  # minute 5 differs; minutes 6..9 are identical in both
    assert res.conflicts[0]["kept_source"] == "archive"
    kept = next(x for x in res.items if x.open_time_ms == t0 + 5 * MIN)
    assert kept.source is BarSource.ARCHIVE
    assert kept.close != 999.0
    pinned = reader.read_bars(
        Market.SPOT, SYM, t0, t0 + 20 * MIN, purpose="research", dataset_ids=[a]
    )
    assert len(pinned.items) == 10
    assert pinned.conflicts == []
    assert pinned.missing == [(t0 + 10 * MIN, t0 + 20 * MIN)]
