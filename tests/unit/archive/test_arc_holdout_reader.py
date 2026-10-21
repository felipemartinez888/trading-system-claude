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
        EventStoreUnlocks(store, repo).unlock("P1", now_ms=1)  # type: ignore[arg-type]
    auth = holdout_cli.authorize(
        repo, "P1", HASH, "r", stdin=TTY("UNLOCK HOLDOUT P1\n"), stdout=io.StringIO()
    )
    auth.consume()
    with pytest.raises(HoldoutError, match="already used"):
        EventStoreUnlocks(store, repo).unlock(auth, now_ms=1)


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
    guard = HoldoutGuard(EventStoreUnlocks(store, repo))
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
                EventStoreUnlocks(s, repo).unlock(auth, now_ms=ms(2026, 10, 20))
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

    reader = LakeReader(lake, HoldoutGuard(EventStoreUnlocks(store, repo)))
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
        lake.read_sealed_part(parts[1], warm, prereg_id=None, strategy_hash=None)  # type: ignore[arg-type]


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


# ---------------------------------------------------------------- re-review R1..R3
def _bars_through_holdout(lake: Lake) -> str:
    return _dataset(lake, ms(2025, 12, 31, 23, 30), ms(2026, 1, 1, 0, 30))


def _read_holdout(reader: LakeReader):  # type: ignore[no-untyped-def]
    return reader.read_bars(
        Market.SPOT,
        SYM,
        ms(2026, 1, 1),
        ms(2026, 1, 1, 0, 30),
        purpose="research",
        prereg_id="P1",
        strategy_hash=HASH,
    )


def test_r1_a_raw_event_without_claim_or_repo_evidence_is_not_trusted(
    lake: Lake, store: EventStore, repo: Path
) -> None:
    from tsclaude.contracts.events import NewEvent

    _bars_through_holdout(lake)
    evidence = inspect_preregistration(repo, "P1")
    store.append(
        NewEvent(
            type=EventType.HOLDOUT_UNLOCKED,
            aggregate_type=AggregateType.RESEARCH,
            aggregate_id="prereg:P1",
            ts_ms=1,
            payload={
                "prereg_id": "P1",
                "strategy_hash": HASH,
                "unlocked_at_ms": 1,
                "reason": "forged",
                "prereg_sha256": evidence.sha256,
                "git_head": evidence.git_head,
                "git_blob": evidence.git_blob,
            },
        )
    )
    unlocks = EventStoreUnlocks(store, repo)
    assert unlocks.find("P1") is None  # no claim row: the event alone is not an unlock
    with pytest.raises(HoldoutLocked):
        _read_holdout(LakeReader(lake, HoldoutGuard(unlocks)))


def test_r1_b_forged_authorization_cannot_unlock(store: EventStore, repo: Path) -> None:
    from tsclaude.store.holdout import PreregEvidence

    fake = PreregEvidence("FAKE", HASH, "preregistrations/FAKE.yaml", "0" * 64, "0" * 40, "0" * 40)
    auth = UnlockAuthorization(issuer=holdout_cli.ISSUER, evidence=fake, reason="peek")
    with pytest.raises(PreregistrationError):
        EventStoreUnlocks(store, repo).unlock(auth, now_ms=1)
    real = inspect_preregistration(repo, "P1")
    tampered = PreregEvidence(
        real.prereg_id,
        "sha256:ffffffffffffffff",
        real.path,
        real.sha256,
        real.git_head,
        real.git_blob,
    )
    auth2 = UnlockAuthorization(issuer=holdout_cli.ISSUER, evidence=tampered, reason="peek")
    with pytest.raises(PreregistrationError, match="does not match"):
        EventStoreUnlocks(store, repo).unlock(auth2, now_ms=1)
    assert store.read(types=[EventType.HOLDOUT_UNLOCKED]) == []


def test_r1_c_recorded_unlock_must_verify_against_git(store: EventStore, repo: Path) -> None:
    from tsclaude.store.holdout import verify_recorded_unlock

    _unlock(store, repo)
    (event,) = store.read(types=[EventType.HOLDOUT_UNLOCKED])
    good = dict(event.payload)
    assert verify_recorded_unlock(repo, good) is None
    assert "does not exist" in str(verify_recorded_unlock(repo, {**good, "git_blob": "1" * 40}))
    assert "SHA-256" in str(verify_recorded_unlock(repo, {**good, "prereg_sha256": "2" * 64}))
    assert "declare" in str(
        verify_recorded_unlock(repo, {**good, "strategy_hash": "sha256:eeeeeeeeeeeeeeee"})
    )
    # a commit that is not an ancestor of HEAD (e.g. on an abandoned branch)
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "preregistrations" / "P1.yaml").write_text(
        f"prereg_id: P1\nstrategy_hash: '{HASH}'\nnote: side\n", encoding="utf-8"
    )
    _git(repo, "commit", "-q", "-am", "side")
    side_head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    _git(repo, "checkout", "-q", "-")
    side_blob = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", f"{side_head}:preregistrations/P1.yaml"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    import hashlib

    side_sha = hashlib.sha256(
        subprocess.run(
            ["git", "-C", str(repo), "cat-file", "blob", side_blob], check=True, capture_output=True
        ).stdout
    ).hexdigest()
    off = {**good, "git_head": side_head, "git_blob": side_blob, "prereg_sha256": side_sha}
    assert "ancestor" in str(verify_recorded_unlock(repo, off))
    # a valid unlock is still found
    assert EventStoreUnlocks(store, repo).find("P1") is not None


def test_r2_approvals_are_immutable_and_never_trusted(
    lake: Lake, store: EventStore, repo: Path
) -> None:
    _bars_through_holdout(lake)
    guard = HoldoutGuard(SealedUnlocks())
    warm = guard.check(ms(2025, 12, 1), HOLDOUT_START_MS, purpose="research")
    with pytest.raises(AttributeError):
        warm.holdout_access = True  # type: ignore[misc]
    with pytest.raises(AttributeError):
        object.__setattr__(warm, "nonexistent", 1)
    sealed = next(ip.part for ip in lake.index() if ip.part.sealed)
    with pytest.raises(SealedPartError):
        lake.read_sealed_part(sealed, warm, prereg_id="P1", strategy_hash=HASH)  # type: ignore[arg-type]
    with pytest.raises(HoldoutLocked):
        lake.read_sealed_part(sealed, guard, prereg_id="P1", strategy_hash=HASH)

    class Yes:
        def find(self, prereg_id: str):  # type: ignore[no-untyped-def]
            from tsclaude.store.holdout import UnlockRecord

            return UnlockRecord(prereg_id, HASH, 0, "fake")

    with pytest.raises(HoldoutError, match="accepts"):
        HoldoutGuard(Yes())  # type: ignore[arg-type]


def _tamper(lake: Lake, dsid: str, month: int, new_path: str) -> None:
    import json

    mpath = lake.imports_dir / dsid / "manifest.json"
    m = json.loads(mpath.read_text())
    for d in m["parts"]:
        if d["month"] == month:
            d["path"] = new_path
            d["year"], d["month"] = 2025, 12  # claims to be December 2025
            d["first_time_ms"], d["last_time_ms"] = (
                ms(2025, 12, 31, 23, 30),
                ms(2025, 12, 31, 23, 59),
            )
    mpath.write_text(json.dumps(m))
    lake.refresh()


def _sealed_part_path(lake: Lake) -> str:
    return next(ip.part.path for ip in lake.index() if ip.part.month == 1)


@pytest.mark.parametrize("variant", ["dotdot", "symlink", "case", "copied_into_lake"])
def test_r3_path_tricks_never_serve_holdout_rows(lake: Lake, variant: str) -> None:
    dsid = _bars_through_holdout(lake)
    sealed_rel = _sealed_part_path(lake)
    assert sealed_rel.startswith("lake_sealed/")
    target = lake.data_dir / sealed_rel
    if variant == "dotdot":
        new = "lake/../" + sealed_rel
    elif variant == "symlink":
        link = lake.data_dir / "lake" / "market=spot" / "link.jsonl"
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("symlinks not permitted here")
        new = "lake/market=spot/link.jsonl"
    elif variant == "case":
        upper = lake.data_dir / ("LAKE_SEALED" + sealed_rel[len("lake_sealed") :])
        upper.parent.mkdir(parents=True, exist_ok=True)
        if not upper.exists():  # case-sensitive FS; on Windows it already is the sealed file
            shutil.copyfile(target, upper)
        new = "LAKE_SEALED" + sealed_rel[len("lake_sealed") :]
    else:
        inside = lake.data_dir / "lake" / "market=spot" / "copy.jsonl"
        inside.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(target, inside)
        new = "lake/market=spot/copy.jsonl"
    _tamper(lake, dsid, 1, new)
    part = next(ip.part for ip in lake.index() if ip.part.path == new)
    assert not part.sealed  # the tampered manifest claims a non-holdout month
    with pytest.raises(SealedPartError):
        lake.read_part(part)
    reader = LakeReader(lake, HoldoutGuard(SealedUnlocks()))
    with pytest.raises((SealedPartError, HoldoutLocked)):
        reader.read_bars(
            Market.SPOT, SYM, ms(2025, 12, 31, 23, 30), HOLDOUT_START_MS, purpose="research"
        )


def test_r3_sealed_decided_by_time_not_path() -> None:
    from tsclaude.store.lake import PartInfo

    p = PartInfo(
        "spot", "bars_1m", SYM, 2026, 3, "lake/x.jsonl", "h", "f", 1, ms(2026, 3, 1), ms(2026, 3, 1)
    )
    assert p.sealed
    q = PartInfo(
        "spot",
        "bars_1m",
        SYM,
        2025,
        12,
        "lake_sealed/x.jsonl",
        "h",
        "f",
        1,
        ms(2025, 12, 1),
        ms(2025, 12, 31, 23, 59),
    )
    assert not q.sealed
