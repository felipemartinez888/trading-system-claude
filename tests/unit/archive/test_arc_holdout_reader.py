"""Holdout guard and unlock (ADR-0009, L-07) and the research lake reader (sealed root)."""

from __future__ import annotations

import io
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from arc_helpers import MIN, JsonLinesCodec, ms, rest_klines

from tsclaude.contracts.events import AggregateType, EventType
from tsclaude.contracts.market import BarSource, Market
from tsclaude.contracts.timeutil import ManualClock
from tsclaude.store import holdout_cli
from tsclaude.store.eventstore import EventStore
from tsclaude.store.holdout import (
    HOLDOUT_END_MS,
    HOLDOUT_START_MS,
    EventStoreUnlocks,
    HoldoutAlreadyUnlocked,
    HoldoutApproval,
    HoldoutError,
    HoldoutGuard,
    HoldoutLocked,
    PreregistrationError,
    SealedUnlocks,
    UnlockAuthorization,
    inspect_preregistration,
)
from tsclaude.store.lake import Lake, LakeKind, SealedPartError
from tsclaude.store.lake_reader import LakeReader

SYM = "BTCUSDT"
HASH = "sha256:0123456789abcdef"
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


class TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "user.name=t",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "preregistrations").mkdir(parents=True)
    _git(r, "init", "-q")
    (r / "preregistrations" / "P1.yaml").write_text(
        f"prereg_id: P1\nstrategy_hash: '{HASH}'\nhypothesis: test\n", encoding="utf-8"
    )
    _git(r, "add", "preregistrations/P1.yaml")
    _git(r, "commit", "-q", "-m", "prereg P1")
    return r


@pytest.fixture
def store(tmp_path: Path):  # type: ignore[no-untyped-def]
    s = EventStore(tmp_path / "state.db")
    yield s
    s.close()


def _unlock(
    store: EventStore,
    repo: Path,
    prereg: str = "P1",
    strategy: str = HASH,
    typed: str | None = None,
):  # type: ignore[no-untyped-def]
    phrase = typed if typed is not None else f"UNLOCK HOLDOUT {prereg}"
    return holdout_cli.unlock(
        store,
        repo,
        prereg,
        strategy,
        "validation passed",
        stdin=TTY(phrase + "\n"),
        stdout=io.StringIO(),
        clock=ManualClock(ms(2026, 10, 20)),
    )


# ---------------------------------------------------------------- guard
def test_holdout_constants_match_protocol() -> None:
    assert ms(2026, 1, 1) == HOLDOUT_START_MS
    assert ms(2026, 10, 1) == HOLDOUT_END_MS


def test_guard_refuses_holdout_and_serves_warmup() -> None:
    guard = HoldoutGuard(SealedUnlocks())
    assert not guard.check(ms(2025, 6), HOLDOUT_START_MS, purpose="research").holdout_access
    guard.check(HOLDOUT_END_MS, ms(2026, 11), purpose="research")
    for start, end in [
        (ms(2025, 12, 31), HOLDOUT_START_MS + 1),
        (ms(2026, 3), ms(2026, 4)),
        (ms(2026, 9, 30, 23, 59), HOLDOUT_END_MS),
        (ms(2025, 1), ms(2027, 1)),
    ]:
        with pytest.raises(HoldoutLocked):
            guard.check(start, end, purpose="research", prereg_id="P1", strategy_hash=HASH)
    with pytest.raises(HoldoutError, match="unknown read purpose"):
        guard.check(ms(2024, 1), ms(2024, 2), purpose="peek")
    with pytest.raises(HoldoutError, match=r"HoldoutGuard\.check"):
        HoldoutApproval(
            issuer=object(),
            start_ms=0,
            end_ms=1,
            holdout_access=True,
            prereg_id=None,
            strategy_hash=None,
        )


# ---------------------------------------------------------------- unlock (L-07 a)
def test_made_up_preregistration_cannot_unlock(store: EventStore, repo: Path) -> None:
    for made_up in ("made-up-1", "made-up-2"):
        with pytest.raises(PreregistrationError, match="does not exist"):
            _unlock(store, repo, made_up)
    with pytest.raises(PreregistrationError, match="invalid"):
        inspect_preregistration(repo, "../etc/passwd")
    assert store.read(types=[EventType.HOLDOUT_UNLOCKED]) == []


def test_uncommitted_or_modified_preregistration_is_refused(store: EventStore, repo: Path) -> None:
    (repo / "preregistrations" / "P2.yaml").write_text(
        f"prereg_id: P2\nstrategy_hash: '{HASH}'\n", encoding="utf-8"
    )
    with pytest.raises(PreregistrationError, match="not tracked"):
        _unlock(store, repo, "P2")
    (repo / "preregistrations" / "P1.yaml").write_text(
        f"prereg_id: P1\nstrategy_hash: '{HASH}'\nhypothesis: changed\n", encoding="utf-8"
    )
    with pytest.raises(PreregistrationError, match="uncommitted"):
        _unlock(store, repo, "P1")
    assert store.read(types=[EventType.HOLDOUT_UNLOCKED]) == []


def test_strategy_hash_phrase_and_terminal_are_required(store: EventStore, repo: Path) -> None:
    with pytest.raises(HoldoutError, match="declares strategy_hash"):
        _unlock(store, repo, strategy="sha256:ffffffffffffffff")
    with pytest.raises(HoldoutError, match="confirmation phrase"):
        _unlock(store, repo, typed="yes")
    with pytest.raises(HoldoutError, match="interactive"):
        holdout_cli.unlock(
            store, repo, "P1", HASH, "r", stdin=io.StringIO("x\n"), stdout=io.StringIO()
        )
    assert store.read(types=[EventType.HOLDOUT_UNLOCKED]) == []


def test_library_unlock_needs_cli_authorization(store: EventStore, repo: Path) -> None:
    evidence = inspect_preregistration(repo, "P1")
    with pytest.raises(HoldoutError, match="unlock-holdout"):
        UnlockAuthorization(issuer=object(), evidence=evidence, reason="r")
    with pytest.raises(HoldoutError, match="UnlockAuthorization"):
        EventStoreUnlocks(store).unlock("P1", now_ms=1)  # type: ignore[arg-type]
    auth = holdout_cli.authorize(
        repo, "P1", HASH, "r", stdin=TTY("UNLOCK HOLDOUT P1\n"), stdout=io.StringIO()
    )
    auth.consume()
    with pytest.raises(HoldoutError, match="already used"):
        EventStoreUnlocks(store).unlock(auth, now_ms=1)


def test_unlock_once_records_hashes_and_opens_only_that_strategy(
    store: EventStore, repo: Path
) -> None:
    record = _unlock(store, repo)
    assert record.strategy_hash == HASH
    (event,) = store.read(types=[EventType.HOLDOUT_UNLOCKED])
    assert event.aggregate_type is AggregateType.RESEARCH
    assert event.payload["prereg_sha256"] == inspect_preregistration(repo, "P1").sha256
    assert len(event.payload["git_head"]) == 40
    assert event.payload["prereg_path"] == "preregistrations/P1.yaml"
    guard = HoldoutGuard(EventStoreUnlocks(store))
    approval = guard.check(
        ms(2026, 2), ms(2026, 3), purpose="research", prereg_id="P1", strategy_hash=HASH
    )
    assert approval.holdout_access
    with pytest.raises(HoldoutLocked, match="not sha256:ff"):
        guard.check(
            ms(2026, 2),
            ms(2026, 3),
            purpose="research",
            prereg_id="P1",
            strategy_hash="sha256:ffffffffffffffff",
        )
    with pytest.raises(HoldoutAlreadyUnlocked):
        _unlock(store, repo)
    assert len(store.read(types=[EventType.HOLDOUT_UNLOCKED])) == 1
    assert store.verify().ok


def test_concurrent_unlocks_cannot_both_succeed(tmp_path: Path, repo: Path) -> None:
    db = tmp_path / "race.db"
    EventStore(db).close()
    auths = [
        holdout_cli.authorize(
            repo, "P1", HASH, f"r{i}", stdin=TTY("UNLOCK HOLDOUT P1\n"), stdout=io.StringIO()
        )
        for i in range(4)
    ]
    results: list[str] = []
    barrier = threading.Barrier(len(auths))

    def worker(auth: UnlockAuthorization) -> None:
        with EventStore(db) as s:
            barrier.wait()
            try:
                EventStoreUnlocks(s).unlock(auth, now_ms=ms(2026, 10, 20))
                results.append("ok")
            except HoldoutAlreadyUnlocked:
                results.append("refused")

    threads = [threading.Thread(target=worker, args=(a,)) for a in auths]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["ok", "refused", "refused", "refused"]
    with EventStore(db) as s:
        assert len(s.read(types=[EventType.HOLDOUT_UNLOCKED])) == 1


# ---------------------------------------------------------------- reader and sealed root
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
    lake: Lake,
    start: int,
    end: int,
    source: str = "archive",
    tweak: dict[int, float] | None = None,
    kind: LakeKind = LakeKind.BARS_1M,
) -> str:
    step = 86_400_000 if kind is LakeKind.KLINES_1D else MIN
    rows = []
    for k in rest_klines(start, end, step):
        r = _row(k, source)
        if tweak and k.open_time_ms in tweak:
            r["close"] = tweak[k.open_time_ms]
            r["high"] = max(r["high"], r["close"])
        rows.append(r)
    parts = lake.write_parts(Market.SPOT, kind, SYM, rows)
    dsid, _ = lake.commit_dataset(parts, {"market": "spot", "kind": str(kind), "symbol": SYM}, None)
    lake.refresh()
    return dsid


def test_holdout_months_live_under_the_sealed_root(
    lake: Lake, store: EventStore, repo: Path
) -> None:
    dsid = _dataset(lake, ms(2025, 12, 31, 23, 50), ms(2026, 1, 1, 0, 10))
    parts = {ip.part.month: ip.part for ip in lake.index()}
    assert parts[12].path.startswith("lake/")
    assert parts[1].path.startswith("lake_sealed/")
    assert parts[1].sealed
    with pytest.raises(SealedPartError):
        lake.read_part(parts[1])
    assert len(lake.read_part(parts[12])) == 10
    assert lake.verify(dsid)["ok"]  # integrity checks still cover sealed parts

    reader = LakeReader(lake, HoldoutGuard(EventStoreUnlocks(store)))
    with pytest.raises(HoldoutLocked):
        reader.read_bars(
            Market.SPOT,
            SYM,
            ms(2025, 12, 31, 23, 50),
            ms(2026, 1, 1, 0, 10),
            purpose="research",
            prereg_id="P1",
            strategy_hash=HASH,
        )
    warmup = reader.read_bars(
        Market.SPOT, SYM, ms(2025, 12, 31), HOLDOUT_START_MS, purpose="research"
    )
    assert len(warmup.items) == 10
    _unlock(store, repo)
    unlocked = reader.read_bars(
        Market.SPOT,
        SYM,
        ms(2025, 12, 31, 23, 50),
        ms(2026, 1, 1, 0, 10),
        purpose="research",
        prereg_id="P1",
        strategy_hash=HASH,
    )
    assert len(unlocked.items) == 20
    # a sealed part cannot be opened with a warm-up approval
    warm = HoldoutGuard(SealedUnlocks()).check(
        ms(2025, 12, 1), HOLDOUT_START_MS, purpose="research"
    )
    with pytest.raises(SealedPartError):
        lake.read_sealed_part(parts[1], warm)


def test_volume_reads_are_guarded(lake: Lake) -> None:
    _dataset(lake, ms(2025, 12, 1), ms(2026, 1, 3), kind=LakeKind.KLINES_1D)
    reader = LakeReader(lake, HoldoutGuard(SealedUnlocks()))
    vols = reader.daily_quote_volumes(Market.SPOT, SYM, ms(2025, 12, 1), HOLDOUT_START_MS)
    assert len(vols) == 31
    with pytest.raises(HoldoutLocked):
        reader.daily_quote_volumes(Market.SPOT, SYM, ms(2025, 12, 1), ms(2026, 1, 3))
    with pytest.raises(HoldoutLocked):
        reader.first_daily_open(Market.SPOT, SYM, ms(2026, 1, 1), ms(2026, 1, 3))


def test_reader_returns_bars_with_source_and_missing(lake: Lake) -> None:
    dsid = _dataset(lake, ms(2025, 12, 31, 23, 50), HOLDOUT_START_MS)
    reader = LakeReader.open(lake.data_dir, HoldoutGuard(SealedUnlocks()), JsonLinesCodec())
    res = reader.read_bars(
        Market.SPOT, SYM, ms(2025, 12, 31, 23, 40), HOLDOUT_START_MS, purpose="research"
    )
    assert len(res.items) == 10
    assert all(b.source is BarSource.ARCHIVE for b in res.items)
    assert res.dataset_ids == (dsid,)
    assert res.missing == [(ms(2025, 12, 31, 23, 40), ms(2025, 12, 31, 23, 50))]


def test_reader_guard_runs_before_any_io(lake: Lake) -> None:
    reader = LakeReader(lake, HoldoutGuard(SealedUnlocks()))
    with pytest.raises(HoldoutLocked):
        reader.read_funding(SYM, ms(2026, 1), ms(2026, 2), purpose="research")
    with pytest.raises(HoldoutLocked):
        reader.read_mark_price_1m(SYM, ms(2026, 1), ms(2026, 2), purpose="research")


def test_overlapping_datasets_merge_conflicts_and_pinning(lake: Lake) -> None:
    t0 = ms(2025, 3)
    a = _dataset(lake, t0, t0 + 10 * MIN, "rest_backfill", tweak={t0 + 5 * MIN: 999.0})
    b = _dataset(lake, t0 + 5 * MIN, t0 + 20 * MIN, "archive")
    reader = LakeReader(lake, HoldoutGuard(SealedUnlocks()))
    res = reader.read_bars(Market.SPOT, SYM, t0, t0 + 20 * MIN, purpose="research")
    assert [x.open_time_ms for x in res.items] == [t0 + i * MIN for i in range(20)]
    assert set(res.dataset_ids) == {a, b}
    assert len(res.conflicts) == 1
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
