"""Regression tests for d17_promote.py on small synthetic lakes only.

No real data, no network; the synthetic 2026-01 part lives in lake_sealed and is never decoded by the
tool (only stat()-ed and referenced by path). Covers the first adversarial review (B1-B3, M1), the
conditions F1-F8 of the second review, N1-N5 of the third one and N7 of the fourth one.
"""

# ruff: noqa: E501
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import d17_promote as d17

from tsclaude.api.process import InstanceLock
from tsclaude.contracts.market import Market
from tsclaude.store.holdout import HoldoutGuard, SealedUnlocks
from tsclaude.store.lake import Lake, LakeKind
from tsclaude.store.lake_reader import LakeReader

MIN = 60_000
FEB = 1738368000000  # 2025-02-01
APR = 1743465600000  # 2025-04-01
JAN26 = 1767225600000  # 2026-01-01 (sealed holdout period)
SYMS = ("XRPUSDT", "SOLUSDT")
GAP = 77
ZIP = b"fake zip"


class Crash(Exception):
    pass


def rows(
    start: int, n: int, skip: frozenset[int] = frozenset(), ing: int = 1, bump: float = 0.0
) -> list[dict[str, Any]]:
    return [
        {
            "open_time_ms": start + i * MIN,
            "close_time_ms": start + i * MIN + MIN - 1,
            "open": 1.0 + i * 1e-4 + bump,
            "high": 2.0,
            "low": 0.5,
            "close": 1.5,
            "volume": 10.0,
            "quote_volume": 15.0,
            "trades": 3,
            "taker_buy_volume": 5.0,
            "taker_buy_quote_volume": 7.0,
            "source": "archive",
            "ingested_at_ms": ing,
        }
        for i in range(n)
        if i not in skip
    ]


def meta(sym: str) -> dict[str, Any]:
    return {
        "market": "spot",
        "kind": "bars_1m",
        "symbol": sym,
        "interval": "1m",
        "tool": "t",
        "created_at": "x",
        "created_at_ms": 1,
        "request": {},
        "sources": [
            {
                "role": "data",
                "cache_path": f"data/spot/monthly/klines/{sym}/1m/{sym}-1m-2025-04.zip",
                "sha256": hashlib.sha256(ZIP).hexdigest(),
                "verified": True,
            }
        ],
    }


def add_cache(d: Path, sym: str) -> None:
    p = d / f"archive_cache/data/spot/monthly/klines/{sym}/1m/{sym}-1m-2025-04.zip"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(ZIP)


@pytest.fixture
def lakes(tmp_path: Path) -> tuple[Path, Path]:
    return make_lakes(tmp_path, catalog=True)


def make_lakes(tmp_path: Path, catalog: bool) -> tuple[Path, Path]:
    """catalog=False mirrors the real canonical data folder, which has no catalog.duckdb."""
    canon, src = tmp_path / "canon", tmp_path / "src"
    for sym in SYMS:
        with Lake(canon).begin() as st:
            st.write_parts("spot", LakeKind.BARS_1M, sym, rows(FEB, 100))
            st.write_parts("spot", LakeKind.BARS_1M, sym, rows(APR, 200, skip=frozenset({GAP})))
            st.write_parts("spot", LakeKind.BARS_1M, sym, rows(JAN26, 50))
            assert st.commit(meta(sym), {"summary": {}}) is not None
        with Lake(src).begin() as st:
            st.write_parts("spot", LakeKind.BARS_1M, sym, rows(APR, 200, ing=2))
            assert (
                st.commit(
                    meta(sym), {"summary": {"classification_ok": True, "missing_interior": 0}}
                )
                is not None
            )
        add_cache(canon, sym)
        add_cache(src, sym)
    if catalog:
        Lake(canon).build_catalog()
    return canon, src


def changed(pre: dict[str, Any], canon: Path) -> list[str]:
    post = d17.inventory(canon)
    return sorted({k for k, v in pre.items() if post.get(k) != v} | (set(post) - set(pre)))


def item(e: Path, n: int = 0) -> dict[str, Any]:
    return dict(d17.jread(e)[0]["items"][n])


def fault_at(monkeypatch: pytest.MonkeyPatch, stage: str, nth: int = 1) -> None:
    seen = {"n": 0}

    def hook(s: str) -> None:
        if s == stage:
            seen["n"] += 1
            if seen["n"] == nth:
                raise Crash(stage)

    monkeypatch.setattr(d17, "_fault", hook)


def no_faults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(d17, "_fault", lambda s: None)


def read(canon: Path, sym: str, pin: list[str] | None = None) -> tuple[int, int, int, int]:
    r = LakeReader.open(canon, HoldoutGuard(SealedUnlocks())).read_bars(
        Market.SPOT, sym, APR, APR + 200 * MIN, purpose="research", dataset_ids=pin
    )
    t = [b.open_time_ms for b in r.items]
    return len(t), len(set(t)), len(r.missing), len(r.conflicts)


def ids(canon: Path) -> set[str]:
    return set(Lake(canon).dataset_ids())


def state(e: Path) -> str:
    return str(d17.do_status(e)["state"])


# --------------------------------------------------------------------------------------- happy path
def test_plan_is_read_only_and_finds_one_minute_per_symbol(
    lakes: tuple[Path, Path], tmp_path: Path
) -> None:
    canon, src = lakes
    pre = d17.inventory(canon)
    plan = d17.do_plan(canon, src, tmp_path / "e")
    assert {i["symbol"]: i["added_minutes_ms"] for i in plan["items"]} == {
        s: [APR + GAP * MIN] for s in SYMS
    }
    assert d17.inventory(canon) == pre


def test_apply_complete_preserves_everything_and_publishes_no_monthly_dataset(
    lakes: tuple[Path, Path], tmp_path: Path
) -> None:
    canon, src = lakes
    pre, old_ids = d17.inventory(canon), ids(canon)
    d17.do_apply(canon, src, tmp_path / "e", "TEST")
    assert state(tmp_path / "e") == "COMPLETE"
    assert d17.do_status(tmp_path / "e")["accepted"] is True
    post = d17.inventory(canon)
    assert [k for k, v in pre.items() if post.get(k) != v] == [
        "catalog.duckdb"
    ]  # nothing else touched
    new = ids(canon) - old_ids
    assert len(new) == 2  # exactly the two composites
    src_ids = set(Lake(src).dataset_ids())
    assert not (src_ids & ids(canon))  # M1: monthly datasets are evidence only
    assert sorted(p.name for p in (tmp_path / "e" / "source_evidence").iterdir()) == sorted(src_ids)
    for sym in SYMS:
        assert read(canon, sym) == (200, 200, 0, 0)  # no duplicate minute, gap closed, no conflict
        comp = next(i for i in new if Lake(canon).manifest(i)["symbol"] == sym)
        assert read(canon, sym, [comp]) == (200, 200, 0, 0)
        old = next(i for i in old_ids if Lake(canon).manifest(i)["symbol"] == sym)
        assert read(canon, sym, [old]) == (199, 199, 1, 0)  # historical pin unchanged
        assert Lake(canon).verify(old)["ok"]
        m = Lake(canon).manifest(comp)
        assert m["supersedes"]["dataset_id"] == old
        assert m["sealed_parts"] == 1
        assert "qa_summary" not in m


# ------------------------------------------------------------------------ visibility and recovery
def test_after_parts_published_nothing_is_visible(
    lakes: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canon, src = lakes
    old_ids = ids(canon)
    fault_at(monkeypatch, "after_part_publish", nth=2)
    with pytest.raises(Crash):
        d17.do_apply(canon, src, tmp_path / "e", "TEST")
    assert ids(canon) == old_ids
    assert all(read(canon, s) == (199, 199, 1, 0) for s in SYMS)
    assert state(tmp_path / "e") == "IN_PROGRESS"


def test_crash_between_composites_is_incomplete_then_recovers_idempotently(
    lakes: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canon, src = lakes
    fault_at(monkeypatch, "between_composites")
    with pytest.raises(Crash):
        d17.do_apply(canon, src, tmp_path / "e", "TEST")
    st = d17.do_status(tmp_path / "e")
    assert st["state"] == "IN_PROGRESS"
    assert st["accepted"] is False  # fail-closed acceptance boundary
    assert read(canon, SYMS[0]) == (200, 200, 0, 0)  # the documented two-symbol window
    assert read(canon, SYMS[1]) == (199, 199, 1, 0)
    with pytest.raises(d17.Refused):  # a second apply cannot pile on top
        d17.do_apply(canon, src, tmp_path / "e2", "TEST")
    no_faults(monkeypatch)
    assert d17.do_recover(canon, tmp_path / "e") == "COMPLETE"
    snap = d17.inventory(canon)
    assert d17.do_recover(canon, tmp_path / "e") == "COMPLETE"
    assert d17.inventory(canon) == snap
    assert all(read(canon, s) == (200, 200, 0, 0) for s in SYMS)


FORWARD_STAGES = [
    "before_part_publish",
    "after_part_rename",
    "after_part_publish",
    "between_composites",
    "after_composite_commit",
    "before_data_committed",
    "before_catalog",
    "after_catalog",
]


@pytest.mark.parametrize("catalog", [True, False], ids=["catalog", "no_catalog"])
@pytest.mark.parametrize("stage", FORWARD_STAGES)
def test_crash_at_stage_recovers_to_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, catalog: bool
) -> None:
    canon, src = make_lakes(tmp_path, catalog)
    pre = d17.inventory(canon)
    fault_at(monkeypatch, stage)
    with pytest.raises(Crash):
        d17.do_apply(canon, src, tmp_path / "e", "TEST")
    assert state(tmp_path / "e") != "COMPLETE"
    assert d17.do_status(tmp_path / "e")["accepted"] is False
    assert not (tmp_path / "e" / "COMPLETE.json").exists()
    no_faults(monkeypatch)
    assert d17.do_recover(canon, tmp_path / "e") == "COMPLETE"
    touched = [k for k in changed(pre, canon) if not k.startswith(("lake/", "imports/"))]
    assert touched == (["catalog.duckdb"] if catalog else [])  # no catalog created if none existed
    assert (canon / "catalog.duckdb").exists() is catalog
    assert not (canon / ".staging").exists()
    assert all(read(canon, s) == (200, 200, 0, 0) for s in SYMS)


@pytest.mark.parametrize("catalog", [True, False], ids=["catalog", "no_catalog"])
@pytest.mark.parametrize("stage", FORWARD_STAGES)
def test_crash_at_stage_rolls_back_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, catalog: bool
) -> None:
    canon, src = make_lakes(tmp_path, catalog)
    pre = d17.inventory(canon)
    fault_at(monkeypatch, stage)
    with pytest.raises(Crash):
        d17.do_apply(canon, src, tmp_path / "e", "TEST")
    no_faults(monkeypatch)
    assert d17.do_rollback(canon, tmp_path / "e") == "ROLLED_BACK"
    assert d17.inventory(canon) == pre
    assert not (canon / ".staging").exists()
    assert not any((canon / "imports").glob(f"{item(tmp_path / 'e')['composite_id']}*"))
    with pytest.raises(d17.Refused, match="rollback"):
        d17.do_recover(canon, tmp_path / "e")


def test_no_catalog_lake_apply_creates_no_catalog_and_rolls_back_exactly(
    tmp_path: Path,
) -> None:
    canon, src = make_lakes(tmp_path, catalog=False)
    pre = d17.inventory(canon)
    d17.do_apply(canon, src, tmp_path / "e", "TEST")
    assert state(tmp_path / "e") == "COMPLETE"
    assert not (canon / "catalog.duckdb").exists()
    assert [k for k, v in pre.items() if d17.inventory(canon).get(k) != v] == []
    assert not (tmp_path / "e" / "backup" / "catalog.duckdb").exists()
    assert d17.do_rollback(canon, tmp_path / "e") == "ROLLED_BACK"
    assert d17.inventory(canon) == pre
    assert not (canon / "catalog.duckdb").exists()


# ------------------------------------------------- F1: power loss leaves our own object torn
def test_zero_filled_part_after_power_loss_is_redone_by_recover(
    lakes: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canon, src = lakes
    fault_at(monkeypatch, "after_part_rename")
    with pytest.raises(Crash):
        d17.do_apply(canon, src, tmp_path / "e", "TEST")
    p = canon / item(tmp_path / "e")["part_path"]
    p.write_bytes(b"\0" * p.stat().st_size)  # the rename survived, the data blocks did not
    no_faults(monkeypatch)
    assert d17.do_recover(canon, tmp_path / "e") == "COMPLETE"
    assert d17.sha(p) == item(tmp_path / "e")["part_sha256"]
    assert all(read(canon, s) == (200, 200, 0, 0) for s in SYMS)


def test_zero_filled_part_after_power_loss_is_removed_by_rollback(
    lakes: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canon, src = lakes
    pre = d17.inventory(canon)
    fault_at(monkeypatch, "after_part_rename")
    with pytest.raises(Crash):
        d17.do_apply(canon, src, tmp_path / "e", "TEST")
    p = canon / item(tmp_path / "e")["part_path"]
    p.write_bytes(b"")
    no_faults(monkeypatch)
    assert d17.do_rollback(canon, tmp_path / "e") == "ROLLED_BACK"
    assert d17.inventory(canon) == pre


@pytest.mark.parametrize("action", ["recover", "rollback"])
def test_empty_composite_manifest_and_commit_debris_after_power_loss(
    lakes: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    canon, src = lakes
    pre = d17.inventory(canon)
    fault_at(monkeypatch, "after_composite_commit")
    with pytest.raises(Crash):
        d17.do_apply(canon, src, tmp_path / "e", "TEST")
    mf = canon / item(tmp_path / "e")["composite_path"]
    mf.write_bytes(b"")  # os.replace survived, the manifest bytes did not
    (mf.parent / ".manifest.json.1234.abcd.tmp").write_text("{")  # a killed commit_dataset
    no_faults(monkeypatch)
    if action == "recover":
        assert d17.do_recover(canon, tmp_path / "e") == "COMPLETE"
        assert json.loads(mf.read_text())["d17_op_id"] == d17.jread(tmp_path / "e")[0]["op_id"]
        assert not list(mf.parent.glob("*.tmp"))
        assert all(read(canon, s) == (200, 200, 0, 0) for s in SYMS)
    else:
        assert d17.do_rollback(canon, tmp_path / "e") == "ROLLED_BACK"
        assert d17.inventory(canon) == pre
        assert not mf.parent.exists()


def _foreign_part_copy(tmp_path: Path, sym: str) -> Path:
    """The same content (same content hash, same path) written by another import: other file bytes."""
    other = tmp_path / "other"
    with Lake(other).begin() as st:
        st.write_parts("spot", LakeKind.BARS_1M, sym, rows(APR, 200, ing=3))
        st.commit(meta(sym), {"summary": {}})
    return other


def test_valid_foreign_part_at_our_path_is_refused_not_deleted(
    lakes: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canon, src = lakes
    fault_at(monkeypatch, "before_part_publish")
    with pytest.raises(Crash):
        d17.do_apply(canon, src, tmp_path / "e", "TEST")
    it = item(tmp_path / "e")
    other = _foreign_part_copy(tmp_path, it["symbol"])
    (canon / it["part_path"]).parent.mkdir(parents=True, exist_ok=True)
    (canon / it["part_path"]).write_bytes((other / it["part_path"]).read_bytes())
    assert d17.sha(canon / it["part_path"]) != it["part_sha256"]
    no_faults(monkeypatch)
    before = d17.inventory(canon)
    for op in (d17.do_rollback, d17.do_recover):
        with pytest.raises(d17.Refused, match="not provably"):
            op(canon, tmp_path / "e")
        assert d17.inventory(canon) == before


# ---------------------------------------------------- F2: each ownership condition is necessary
def test_part_present_without_our_intent_is_refused(
    lakes: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canon, src = lakes
    fault_at(monkeypatch, "before_part_publish")  # item 0 announced, item 1 never announced
    with pytest.raises(Crash):
        d17.do_apply(canon, src, tmp_path / "e", "TEST")
    it = item(tmp_path / "e", 1)
    (canon / it["part_path"]).write_bytes(
        Path(it["source_part_abs"]).read_bytes()
    )  # identical bytes
    no_faults(monkeypatch)
    before = d17.inventory(canon)
    with pytest.raises(d17.Refused, match="not provably"):
        d17.do_rollback(canon, tmp_path / "e")
    assert d17.inventory(canon) == before
    with pytest.raises(d17.Refused, match="not provably"):
        d17.do_recover(canon, tmp_path / "e")
    assert (canon / it["part_path"]).exists()


def test_composite_present_without_our_intent_is_refused(
    lakes: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canon, src = lakes
    fault_at(monkeypatch, "between_composites")  # composite 0 done, composite 1 never announced
    with pytest.raises(Crash):
        d17.do_apply(canon, src, tmp_path / "e", "TEST")
    e = tmp_path / "e"
    it0, it1 = item(e, 0), item(e, 1)
    m = json.loads((canon / it0["composite_path"]).read_text())
    m["dataset_id"] = it1["composite_id"]  # passes every other check: our op_id, the planned id
    (canon / it1["composite_path"]).parent.mkdir(parents=True)
    (canon / it1["composite_path"]).write_text(json.dumps(m))
    no_faults(monkeypatch)
    before = d17.inventory(canon)
    for op in (d17.do_rollback, d17.do_recover):
        with pytest.raises(d17.Refused, match="not provably"):
            op(canon, e)
        assert d17.inventory(canon) == before


def test_composite_with_another_op_id_is_refused(
    lakes: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canon, src = lakes
    fault_at(monkeypatch, "after_composite_commit")  # announced, written, no `done` yet
    with pytest.raises(Crash):
        d17.do_apply(canon, src, tmp_path / "e", "TEST")
    mf = canon / item(tmp_path / "e")["composite_path"]
    m = json.loads(mf.read_text())
    m["d17_op_id"] = "0" * 32
    mf.write_text(json.dumps(m))
    no_faults(monkeypatch)
    before = d17.inventory(canon)
    for op in (d17.do_rollback, d17.do_recover):
        with pytest.raises(d17.Refused, match="not provably"):
            op(canon, tmp_path / "e")
        assert d17.inventory(canon) == before


def test_composite_edited_after_done_is_refused(lakes: tuple[Path, Path], tmp_path: Path) -> None:
    canon, src = lakes
    d17.do_apply(canon, src, tmp_path / "e", "TEST")
    mf = canon / item(tmp_path / "e")["composite_path"]
    m = json.loads(mf.read_text())
    m["note"] = "edited later by someone else"  # same op_id and id, other bytes
    mf.write_text(json.dumps(m))
    before = d17.inventory(canon)
    with pytest.raises(d17.Refused, match="not provably"):
        d17.do_rollback(canon, tmp_path / "e")
    assert d17.inventory(canon) == before


@pytest.mark.parametrize("key", ["part_path", "composite_path"])
def test_owned_path_recorded_as_pre_existing_is_refused(
    lakes: tuple[Path, Path], tmp_path: Path, key: str
) -> None:
    canon, src = lakes
    d17.do_apply(canon, src, tmp_path / "e", "TEST")
    inv = tmp_path / "e" / "inventory_pre.json"
    pre = json.loads(inv.read_text())
    path = item(tmp_path / "e")[key]
    pre[path] = d17.sha(canon / path)  # the evidence says this file existed before the operation
    inv.write_text(json.dumps(pre))
    before = d17.inventory(canon)
    with pytest.raises(d17.Refused, match="not provably"):
        d17.do_rollback(canon, tmp_path / "e")
    assert d17.inventory(canon) == before


def test_rollback_keeps_a_foreign_file_inside_a_directory_it_created(
    lakes: tuple[Path, Path], tmp_path: Path
) -> None:
    canon, src = lakes
    pre = d17.inventory(canon)
    d17.do_apply(canon, src, tmp_path / "e", "TEST")
    it = item(tmp_path / "e")
    assert it["composite_dirs_new"]  # imports/<composite id>/ was created by the operation
    foreign = (canon / it["composite_path"]).parent / "notes.txt"
    foreign.write_text("another process")
    assert d17.do_rollback(canon, tmp_path / "e") == "ROLLED_BACK"
    assert foreign.read_text() == "another process"  # the directory is kept, not removed
    assert not (canon / it["composite_path"]).exists()
    assert changed(pre, canon) == [foreign.relative_to(canon).as_posix()]


def test_a_racing_import_with_other_values_blocks_acceptance(
    lakes: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canon, src = lakes

    def racing_import(stage: str) -> None:  # `tsclaude data import` does not take the lock
        if stage == "before_data_committed":
            with Lake(canon).begin() as st:
                st.write_parts("spot", LakeKind.BARS_1M, "XRPUSDT", rows(APR, 200, bump=0.2))
                st.commit(meta("XRPUSDT"), {"summary": {}})

    monkeypatch.setattr(d17, "_fault", racing_import)
    with pytest.raises(d17.Refused, match="reader postcondition"):
        d17.do_apply(canon, src, tmp_path / "e", "TEST")
    assert state(tmp_path / "e") == "IN_PROGRESS"
    assert d17.do_status(tmp_path / "e")["accepted"] is False


def test_bytes_are_fsynced_before_they_are_published_or_journaled(
    lakes: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canon, src = lakes
    events: list[tuple[str, str]] = []
    real_fsync, real_rename, real_jappend = d17._fsync_file, os.rename, d17.jappend

    def fsync(p: Path) -> None:
        events.append(("fsync", Path(p).name))
        real_fsync(p)

    def rename(a: Any, b: Any) -> None:
        events.append(("rename", Path(a).name))
        real_rename(a, b)

    def jappend(out: Path, rec: dict[str, Any]) -> None:
        if rec.get("op") == "done":
            events.append(("done", Path(rec["path"]).name))
        if rec.get("op") == "begin":
            events.append(("begin", ""))
        real_jappend(out, rec)

    real_durable = d17._write_durable

    def durable(p: Path, text: str) -> None:
        real_durable(p, text)
        events.append(("durable", p.name))

    monkeypatch.setattr(d17, "_write_durable", durable)

    monkeypatch.setattr(d17, "_fsync_file", fsync)
    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(d17, "jappend", jappend)
    d17.do_apply(canon, src, tmp_path / "e", "TEST")
    for n in range(len(SYMS)):
        it = item(tmp_path / "e", n)
        part = Path(it["part_path"]).name
        i = events.index(("rename", part))
        assert events[i - 1] == ("fsync", part)
        assert events[i + 1] == ("done", part)
        j = [k for k, ev in enumerate(events) if ev == ("done", "manifest.json")][n]
        assert events[j - 1] == ("fsync", "manifest.json")
    assert events.index(("durable", "inventory_pre.json")) < events.index(("begin", ""))  # N1


# ------------------------------------------------------- N1-N2: evidence and remaining guards
@pytest.mark.parametrize("content", [None, '{"lake/x": "ab'])
def test_missing_or_torn_pre_inventory_refuses_cleanly(
    lakes: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str | None,
) -> None:
    canon, src = lakes
    fault_at(monkeypatch, "between_composites")
    with pytest.raises(Crash):
        d17.do_apply(canon, src, tmp_path / "e", "TEST")
    inv = tmp_path / "e" / "inventory_pre.json"
    if content is None:
        inv.unlink()
    else:
        inv.write_text(content)
    no_faults(monkeypatch)
    before = d17.inventory(canon)
    for op in (d17.do_rollback, d17.do_recover):
        with pytest.raises(d17.Refused, match="inventory_pre"):
            op(canon, tmp_path / "e")
        assert d17.inventory(canon) == before


def test_incomplete_part_referenced_by_another_manifest_is_not_deleted(
    lakes: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canon, src = lakes
    fault_at(monkeypatch, "after_part_rename")
    with pytest.raises(Crash):
        d17.do_apply(canon, src, tmp_path / "e", "TEST")
    it = item(tmp_path / "e")
    (canon / it["part_path"]).write_bytes(b"")  # torn, yet another dataset now references the path
    other = canon / "imports" / ("c" * 64) / "manifest.json"
    other.parent.mkdir(parents=True)
    other.write_text(json.dumps({"parts": [{"path": it["part_path"]}]}))
    no_faults(monkeypatch)
    before = d17.inventory(canon)
    for op in (d17.do_recover, d17.do_rollback):
        with pytest.raises(d17.Refused, match="shared"):
            op(canon, tmp_path / "e")
        assert d17.inventory(canon) == before


def test_rollback_refuses_when_a_foreign_manifest_is_unreadable(
    lakes: tuple[Path, Path], tmp_path: Path
) -> None:
    canon, src = lakes
    d17.do_apply(canon, src, tmp_path / "e", "TEST")
    bad = canon / "imports" / ("d" * 64) / "manifest.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{")
    before = d17.inventory(canon)
    with pytest.raises(d17.Refused, match="unreadable"):
        d17.do_rollback(canon, tmp_path / "e")
    assert d17.inventory(canon) == before


def test_composite_emptied_after_done_is_refused_not_treated_as_incomplete(
    lakes: tuple[Path, Path], tmp_path: Path
) -> None:
    canon, src = lakes
    d17.do_apply(canon, src, tmp_path / "e", "TEST")
    (canon / item(tmp_path / "e")["composite_path"]).write_bytes(b"")
    before = d17.inventory(canon)
    with pytest.raises(d17.Refused, match="unreadable"):
        d17.do_rollback(canon, tmp_path / "e")
    assert d17.inventory(canon) == before


def test_composite_with_another_dataset_id_is_refused(
    lakes: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canon, src = lakes
    fault_at(monkeypatch, "after_composite_commit")
    with pytest.raises(Crash):
        d17.do_apply(canon, src, tmp_path / "e", "TEST")
    mf = canon / item(tmp_path / "e")["composite_path"]
    m = json.loads(mf.read_text())
    m["dataset_id"] = "9" * 64  # our op_id, intent and directory, but not the planned dataset
    mf.write_text(json.dumps(m))
    no_faults(monkeypatch)
    before = d17.inventory(canon)
    for op in (d17.do_rollback, d17.do_recover):
        with pytest.raises(d17.Refused, match="not provably"):
            op(canon, tmp_path / "e")
        assert d17.inventory(canon) == before


def test_recover_refuses_while_a_rollback_is_unfinished(
    lakes: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canon, src = lakes
    d17.do_apply(canon, src, tmp_path / "e", "TEST")
    fault_at(monkeypatch, "rollback_after_composite_delete")
    with pytest.raises(Crash):
        d17.do_rollback(canon, tmp_path / "e")
    no_faults(monkeypatch)
    before = d17.inventory(canon)
    with pytest.raises(d17.Refused, match="ROLLING_BACK"):
        d17.do_recover(canon, tmp_path / "e")
    assert d17.inventory(canon) == before


def test_no_catalog_rollback_leaves_a_catalog_someone_else_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canon, src = make_lakes(tmp_path, catalog=False)

    def someone_builds_catalog(stage: str) -> None:
        if stage == "before_catalog":
            Lake(canon).build_catalog()

    monkeypatch.setattr(d17, "_fault", someone_builds_catalog)
    d17.do_apply(canon, src, tmp_path / "e", "TEST")
    no_faults(monkeypatch)
    cat = d17.sha(canon / "catalog.duckdb")
    assert d17.do_rollback(canon, tmp_path / "e") == "ROLLED_BACK"
    assert d17.sha(canon / "catalog.duckdb") == cat  # not ours: never deleted or restored


# ------------------------------------------------------------ F6: rollback preconditions first
def test_rollback_refuses_before_any_deletion_when_a_pre_existing_file_changed(
    lakes: tuple[Path, Path], tmp_path: Path
) -> None:
    canon, src = lakes
    d17.do_apply(canon, src, tmp_path / "e", "TEST")
    victim = next(
        p for p in sorted((canon / "lake").rglob("*.parquet")) if "month=02" in p.as_posix()
    )
    victim.write_bytes(victim.read_bytes() + b"x")
    before = d17.inventory(canon)
    with pytest.raises(d17.Refused, match="changed or removed"):
        d17.do_rollback(canon, tmp_path / "e")
    assert d17.inventory(canon) == before
    assert state(tmp_path / "e") == "COMPLETE"  # never entered ROLLING_BACK


def test_crash_after_catalog_restore_then_rollback_again(
    lakes: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canon, src = lakes
    pre = d17.inventory(canon)
    d17.do_apply(canon, src, tmp_path / "e", "TEST")
    fault_at(monkeypatch, "rollback_after_catalog_restore")
    with pytest.raises(Crash):
        d17.do_rollback(canon, tmp_path / "e")
    assert state(tmp_path / "e") == "ROLLING_BACK"
    assert any(r["op"] == "intent_restore_catalog" for r in d17.jread(tmp_path / "e"))
    no_faults(monkeypatch)
    assert d17.do_rollback(canon, tmp_path / "e") == "ROLLED_BACK"
    assert d17.inventory(canon) == pre


def test_apply_refuses_to_commit_when_a_pre_existing_file_changed(
    lakes: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canon, src = lakes
    victim = next(
        p for p in sorted((canon / "lake").rglob("*.parquet")) if "month=02" in p.as_posix()
    )

    def corrupt(stage: str) -> None:
        if stage == "before_data_committed":
            victim.write_bytes(victim.read_bytes() + b"x")  # another actor alters an immutable part

    monkeypatch.setattr(d17, "_fault", corrupt)
    with pytest.raises(d17.Refused, match="changed or removed"):
        d17.do_apply(canon, src, tmp_path / "e", "TEST")
    assert state(tmp_path / "e") == "IN_PROGRESS"
    assert d17.do_status(tmp_path / "e")["accepted"] is False


def test_catalog_failure_is_separately_recoverable(
    lakes: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canon, src = lakes
    orig = Lake.build_catalog

    def locked(self: Lake) -> Path:
        raise PermissionError("catalog.duckdb is in use")

    monkeypatch.setattr(Lake, "build_catalog", locked)
    with pytest.raises(PermissionError):
        d17.do_apply(canon, src, tmp_path / "e", "TEST")
    assert state(tmp_path / "e") == "DATA_COMMITTED"
    assert d17.do_status(tmp_path / "e")["accepted"] is False
    monkeypatch.setattr(Lake, "build_catalog", orig)
    assert d17.do_recover(canon, tmp_path / "e") == "COMPLETE"


def test_torn_last_journal_record_is_ignored(
    lakes: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canon, src = lakes
    fault_at(monkeypatch, "between_composites")
    with pytest.raises(Crash):
        d17.do_apply(canon, src, tmp_path / "e", "TEST")
    with (tmp_path / "e" / d17.JOURNAL).open("a", encoding="utf-8") as f:
        f.write('{"op": "intent", "kind": "compo')  # power loss mid-write
    no_faults(monkeypatch)
    assert d17.do_recover(canon, tmp_path / "e") == "COMPLETE"


def test_torn_record_followed_by_a_persisted_newline_is_sealed(
    lakes: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canon, src = lakes
    fault_at(monkeypatch, "between_composites")
    with pytest.raises(Crash):
        d17.do_apply(canon, src, tmp_path / "e", "TEST")
    with (tmp_path / "e" / d17.JOURNAL).open("a", encoding="utf-8") as f:
        f.write('{"op": "intent", "kind": "compo\n')  # torn record whose newline did persist
    no_faults(monkeypatch)
    assert d17.do_recover(canon, tmp_path / "e") == "COMPLETE"
    assert d17.do_recover(canon, tmp_path / "e") == "COMPLETE"  # the journal stays readable


def test_corruption_before_the_last_record_is_refused(
    lakes: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canon, src = lakes
    fault_at(monkeypatch, "between_composites")
    with pytest.raises(Crash):
        d17.do_apply(canon, src, tmp_path / "e", "TEST")
    with (tmp_path / "e" / d17.JOURNAL).open("a", encoding="utf-8") as f:
        f.write("{garbage\n{more garbage\n")
    no_faults(monkeypatch)
    before = d17.inventory(canon)
    for op in (d17.do_rollback, d17.do_recover):
        with pytest.raises(d17.Refused, match="corrupt"):
            op(canon, tmp_path / "e")
    assert d17.inventory(canon) == before


# N7 (fourth review): a power loss may keep a whole record but lose its line end. The next record must
# start on its own line; before the fix both were merged into one line and dropped as a torn tail.
def _drop_line_end(e: Path) -> None:
    j = e / d17.JOURNAL
    j.write_bytes(j.read_bytes().rstrip(b"\r\n"))


def test_n7_a_record_without_its_line_end_is_kept_and_the_next_one_too(tmp_path: Path) -> None:
    e = tmp_path / "e"
    e.mkdir()
    d17.jappend(e, {"op": "begin"})
    _drop_line_end(e)
    d17.jappend(e, {"op": "second"})
    assert [r["op"] for r in d17.jread(e)] == ["begin", "second"]
    # a correctly terminated journal gets no extra line end: one per record
    d17.jappend(e, {"op": "third"})
    assert [r["op"] for r in d17.jread(e)] == ["begin", "second", "third"]
    assert (e / d17.JOURNAL).read_bytes().count(b"\n") == 3


def test_n7_lost_line_end_of_an_intent_keeps_it_so_rollback_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canon, src = make_lakes(tmp_path, catalog=False)
    pre = d17.inventory(canon)
    e = tmp_path / "e"
    # the last record is the part intent; the rename happened
    fault_at(monkeypatch, "after_part_rename")
    with pytest.raises(Crash):
        d17.do_apply(canon, src, e, "TEST")
    _drop_line_end(e)
    no_faults(monkeypatch)
    assert d17.do_status(e)["accepted"] is False
    assert d17.do_recover(canon, e) == "COMPLETE"
    # the intent survived the next append, and a whole record is not treated as torn
    recs = d17.jread(e)
    assert d17._intended(recs, "part", item(e, 0)["part_path"])
    assert not any(r.get("op") == "torn_sealed" for r in recs)
    assert d17.do_rollback(canon, e) == "ROLLED_BACK"
    assert d17.inventory(canon) == pre


def test_n7_lost_line_end_of_rollback_begin_never_reports_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canon, src = make_lakes(tmp_path, catalog=False)
    pre = d17.inventory(canon)
    e = tmp_path / "e"
    d17.do_apply(canon, src, e, "TEST")
    assert d17.do_status(e)["accepted"] is True
    real = d17.jappend

    def crash_after_rollback_begin(out: Path, rec: dict[str, Any]) -> None:
        real(out, rec)
        if rec.get("op") == "rollback_begin":
            raise Crash("power loss")

    monkeypatch.setattr(d17, "jappend", crash_after_rollback_begin)
    with pytest.raises(Crash):
        d17.do_rollback(canon, e)
    monkeypatch.setattr(d17, "jappend", real)
    _drop_line_end(e)
    st = d17.do_status(e)
    assert (st["state"], st["accepted"]) == ("ROLLING_BACK", False)
    # a second interruption: one composite is already deleted, so one symbol is unrepaired
    fault_at(monkeypatch, "rollback_after_composite_delete")
    with pytest.raises(Crash):
        d17.do_rollback(canon, e)
    no_faults(monkeypatch)
    st = d17.do_status(e)
    # the reviewed tool reported ("COMPLETE", True) here
    assert (st["state"], st["accepted"]) == ("ROLLING_BACK", False)
    assert [r["op"] for r in d17.jread(e)].count("rollback_begin") == 1
    with pytest.raises(d17.Refused, match="ROLLING_BACK"):
        d17.do_recover(canon, e)
    assert d17.do_status(e)["accepted"] is False
    assert d17.do_rollback(canon, e) == "ROLLED_BACK"
    assert d17.inventory(canon) == pre


# ------------------------------------------------------------------------------------------ rollback
def test_rollback_after_full_apply_restores_exactly_and_is_repeatable(
    lakes: tuple[Path, Path], tmp_path: Path
) -> None:
    canon, src = lakes
    pre = d17.inventory(canon)
    d17.do_apply(canon, src, tmp_path / "e", "TEST")
    assert d17.do_rollback(canon, tmp_path / "e") == "ROLLED_BACK"
    assert d17.inventory(canon) == pre
    assert d17.do_rollback(canon, tmp_path / "e") == "ROLLED_BACK"
    assert d17.inventory(canon) == pre


def test_crash_during_rollback_then_rollback_again(
    lakes: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canon, src = lakes
    pre = d17.inventory(canon)
    d17.do_apply(canon, src, tmp_path / "e", "TEST")
    fault_at(monkeypatch, "rollback_after_composite_delete")
    with pytest.raises(Crash):
        d17.do_rollback(canon, tmp_path / "e")
    assert state(tmp_path / "e") == "ROLLING_BACK"
    no_faults(monkeypatch)
    assert d17.do_rollback(canon, tmp_path / "e") == "ROLLED_BACK"
    assert d17.inventory(canon) == pre


def test_rollback_never_deletes_files_of_another_actor(
    lakes: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canon, src = lakes
    foreign = canon / "imports" / ("f" * 64) / "note.txt"
    stray = canon / "lake" / "stray.txt"

    def write_foreign(stage: str) -> None:
        if stage == "before_catalog":
            foreign.parent.mkdir(parents=True)
            foreign.write_text("another process")
            stray.write_text("another process")

    monkeypatch.setattr(d17, "_fault", write_foreign)
    d17.do_apply(canon, src, tmp_path / "e", "TEST")
    no_faults(monkeypatch)
    assert d17.do_rollback(canon, tmp_path / "e") == "ROLLED_BACK"
    assert foreign.read_text() == "another process"
    assert stray.read_text() == "another process"


def test_rollback_refuses_when_a_later_dataset_reuses_the_part(
    lakes: tuple[Path, Path], tmp_path: Path
) -> None:
    canon, src = lakes
    d17.do_apply(canon, src, tmp_path / "e", "TEST")
    with Lake(canon).begin() as st:  # a later, legitimate import reuses the content-addressed file
        st.write_parts("spot", LakeKind.BARS_1M, "XRPUSDT", rows(APR, 200, ing=3))
        st.write_parts("spot", LakeKind.BARS_1M, "XRPUSDT", rows(FEB, 100, ing=3))
        res = st.commit(meta("XRPUSDT"), {"summary": {}})
    assert res is not None
    before = d17.inventory(canon)
    with pytest.raises(d17.Refused, match="shared"):
        d17.do_rollback(canon, tmp_path / "e")
    assert d17.inventory(canon) == before  # refused before any destructive step
    assert Lake(canon).verify(res[0])["ok"]
    assert state(tmp_path / "e") == "COMPLETE"


def test_rollback_refuses_when_a_run_pins_a_composite(
    lakes: tuple[Path, Path], tmp_path: Path
) -> None:
    canon, src = lakes
    d17.do_apply(canon, src, tmp_path / "e", "TEST")
    comp = next(i for i in ids(canon) if "supersedes" in Lake(canon).manifest(i))
    (canon / "runs" / "r1").mkdir(parents=True)
    (canon / "runs" / "r1" / "manifest.json").write_text(json.dumps({"datasets": {"main": [comp]}}))
    before = d17.inventory(canon)
    with pytest.raises(d17.Refused, match="pins"):
        d17.do_rollback(canon, tmp_path / "e")
    assert d17.inventory(canon) == before


def test_rollback_refuses_to_overwrite_an_externally_changed_catalog(
    lakes: tuple[Path, Path], tmp_path: Path
) -> None:
    canon, src = lakes
    d17.do_apply(canon, src, tmp_path / "e", "TEST")
    (canon / "catalog.duckdb").write_bytes(b"rebuilt by someone else")
    before = d17.inventory(canon)
    with pytest.raises(d17.Refused, match="another actor"):
        d17.do_rollback(canon, tmp_path / "e")
    assert d17.inventory(canon) == before


def test_rollback_refuses_a_tampered_owned_file(lakes: tuple[Path, Path], tmp_path: Path) -> None:
    canon, src = lakes
    d17.do_apply(canon, src, tmp_path / "e", "TEST")
    plan_item = d17.jread(tmp_path / "e")[0]["items"][0]
    (canon / plan_item["part_path"]).write_bytes(b"changed")
    before = d17.inventory(canon)
    with pytest.raises(d17.Refused, match="not provably"):
        d17.do_rollback(canon, tmp_path / "e")
    assert d17.inventory(canon) == before


# ---------------------------------------------------------------------------------------------- lock
def test_every_mutating_command_refuses_while_the_app_lock_is_held(
    lakes: tuple[Path, Path], tmp_path: Path
) -> None:
    canon, src = lakes
    pre = d17.inventory(canon)
    lock = InstanceLock(canon / "tsclaude.lock")  # what the running app holds
    try:
        with pytest.raises(d17.Refused, match="held"):
            d17.do_apply(canon, src, tmp_path / "e", "TEST")
        assert d17.inventory(canon) == pre
        assert not (tmp_path / "e").exists()
    finally:
        lock.release()
    d17.do_apply(canon, src, tmp_path / "e", "TEST")
    lock = InstanceLock(canon / "tsclaude.lock")
    try:
        with pytest.raises(d17.Refused, match="held"):
            d17.do_rollback(canon, tmp_path / "e")
        with pytest.raises(d17.Refused, match="held"):
            d17.do_recover(canon, tmp_path / "e")
    finally:
        lock.release()
    assert state(tmp_path / "e") == "COMPLETE"
    assert (
        d17.do_rollback(canon, tmp_path / "e") == "ROLLED_BACK"
    )  # lock released on every exit path


# ------------------------------------------------------------------------------------ refusals kept
def test_refuses_value_difference(lakes: tuple[Path, Path], tmp_path: Path) -> None:
    canon, _ = lakes
    src2 = tmp_path / "src2"
    with Lake(src2).begin() as st:
        st.write_parts("spot", LakeKind.BARS_1M, "XRPUSDT", rows(APR, 200, bump=1.0))
        st.commit(meta("XRPUSDT"), {"summary": {"classification_ok": True}})
    add_cache(src2, "XRPUSDT")
    with pytest.raises(d17.Refused, match="differ in value"):
        d17.do_plan(canon, src2, tmp_path / "e")


def test_refuses_existing_evidence_dir_and_missing_approval(
    lakes: tuple[Path, Path], tmp_path: Path
) -> None:
    canon, src = lakes
    pre = d17.inventory(canon)
    (tmp_path / "e").mkdir()
    with pytest.raises(d17.Refused, match="exists"):
        d17.do_plan(canon, src, tmp_path / "e")
    with pytest.raises(d17.Refused, match="approval"):
        d17.do_apply(canon, src, tmp_path / "e2", " ")
    assert d17.inventory(canon) == pre
