"""D-17 minimal repair: add verified replacement months to a canonical lake, additively and recoverably.

Lake model (src/tsclaude/store/lake.py): a part file is named by its content hash, a dataset manifest is
immutable and named by the hash of its parts' content hashes, and `Lake.index()` (hence `LakeReader` and
the catalog) only sees parts referenced by a manifest under `imports/<64-hex>/manifest.json`.

Canonical objects this operation creates (and only these are ever deleted by its rollback):
  P_s  the verified replacement part of symbol s (new content-addressed name, never an existing file)
  C_s  imports/<composite id>/manifest.json: the old canonical dataset with that one part swapped
The derived catalog.duckdb is rebuilt (after a backup) only if one existed before; if none existed (the
real data folder today) none is created. The verified monthly source datasets are NOT published into
imports/ (M1); their manifest and QA are copied into the evidence folder and bound by SHA-256 in each
composite's `supersedes`.

Visibility per stage (what Lake.index / LakeReader can see):
  begin, parts published  -> nothing new (an unreferenced part is invisible)
  C_first committed       -> that symbol is repaired for unpinned readers; the other is not yet
  C_second committed      -> both symbols repaired (DATA_COMMITTED after postconditions)
  catalog rebuilt         -> exploration views updated (CATALOG_DONE)
  COMPLETE                -> the only state in which the repair counts as accepted
Each object is published atomically (os.rename / os.replace). The two-symbol repair is NOT atomic: no
single pointer exists across manifests. The acceptance boundary is the COMPLETE record of the journal
(fail closed): any other state is an incomplete promotion, reported by `status`, finished by `recover`
or undone by `rollback`.

Durability: every step appends to <out>/journal.jsonl (flush + fsync) BEFORE the filesystem change it
announces; a torn last line is ignored (the change it announced never started), and a whole record
whose line end was lost gets its line ended before the next append (N7). The staged part is fsynced
before its rename. A manifest is published by commit_dataset (os.replace) and fsynced right after, before
its `done` record: a power loss in between can leave it empty and visible, which makes Lake.index() fail
for every reader until `recover` or `rollback` (N8, fail closed). Directory fsync is not available
on Windows, so recovery re-checks every object by hash instead of trusting the journal; an object of
ours torn by a power loss (announced, no `done`, absent before, unreadable as the planned content) is
classified "incomplete" and redone by `recover` or deleted by `rollback`; anything else refuses.

All mutating subcommands hold the app's InstanceLock (<canonical>/tsclaude.lock), so they refuse while
the app (paper / M3) or another D-17 operation runs. No other `tsclaude` command (data import, backtest,
catalog, verify) takes that lock: none may run on the data folder meanwhile (operator rule, see the plan).

Subcommands:
  plan      --canonical D --source S --out E                 read-only preflight; writes only E
  apply     --canonical D --source S --out E --approval-ref T [--bind FILE ...]
  recover   --canonical D --out E                            finish an incomplete apply (idempotent)
  rollback  --canonical D --out E                            undo an applied or partial operation
  status    --out E                                          journal state
Exit: 0 ok, 1 refused, 2 usage. Never overwrites a file, never deletes what it did not create. No network.
The sealed holdout is never decoded: lake_sealed is only stat()-ed and its parts are referenced by path.
"""

# ruff: noqa: E501
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from tsclaude.contracts.market import Market
from tsclaude.store.holdout import HoldoutGuard, SealedUnlocks
from tsclaude.store.lake import SCHEMAS, Lake, LakeKind, PartInfo, dataset_id_of
from tsclaude.store.lake_reader import LakeReader

EXCL = {"ingested_at_ms"}
JOURNAL = "journal.jsonl"
QA_DIRTY = (
    "missing_interior",
    "missing_unexpected",
    "gap_ranges",
    "malformed_rows",
    "rejected_rows",
    "duplicate_conflicts",
    "unavailable_files",
)


class Refused(RuntimeError):
    pass


def _fault(stage: str) -> None:
    """Failure-injection hook for the synthetic regression tests; a no-op in real use."""


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def inventory(data: Path) -> dict[str, Any]:
    """lake/, imports/ and catalog.duckdb by SHA-256; lake_sealed by (size, mtime_ns) only."""
    inv: dict[str, Any] = {}
    for top in ("lake", "imports"):
        root = data / top
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                if p.is_file():
                    inv[p.relative_to(data).as_posix()] = sha(p)
    cat = data / "catalog.duckdb"
    if cat.is_file():
        inv["catalog.duckdb"] = sha(cat)
    sealed = data / "lake_sealed"
    if sealed.is_dir():
        for p in sorted(sealed.rglob("*")):
            if p.is_file():
                st = p.stat()
                inv["SEALED:" + p.relative_to(data).as_posix()] = [st.st_size, st.st_mtime_ns]
    return inv


# ------------------------------------------------------------------------------------------ journal
def jappend(out: Path, rec: dict[str, Any]) -> None:
    rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **rec}
    path = out / JOURNAL
    torn = False
    unterminated = False
    if path.is_file():
        # N7: a whole record may persist without its line end; the next record must not share its line
        unterminated = not path.read_bytes().endswith(b"\n")
        last = [x for x in path.read_text(encoding="utf-8").split("\n") if x.strip()][-1:]
        if last:
            try:
                json.loads(last[0])
            except json.JSONDecodeError:
                torn = True  # decided by parsing, not by the last byte (a lone "\n" may have persisted)
    with path.open("a", encoding="utf-8") as f:
        if torn:
            # terminate a torn record left by a crash and mark it as skipped, never reuse its line
            f.write("\n" + json.dumps({"op": "torn_sealed"}) + "\n")
        elif unterminated:
            # a complete record whose line end was lost: end its line and keep the record (N7)
            f.write("\n")
        f.write(json.dumps(rec, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


def jread(out: Path) -> list[dict[str, Any]]:
    path = out / JOURNAL
    if not path.is_file():
        raise Refused(f"no journal in {out}")
    recs = []
    lines = path.read_text(encoding="utf-8").split("\n")
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            rest = [x for x in lines[i + 1 :] if x.strip()]
            if not rest:
                break  # torn last record: the change it announced never started
            try:
                sealed = json.loads(rest[0]).get("op") == "torn_sealed"
            except json.JSONDecodeError:
                sealed = False
            if sealed:
                continue  # a torn record that a later append terminated
            raise Refused("journal is corrupt before its last record") from None
        if rec.get("op") != "torn_sealed":
            recs.append(rec)
    if not recs or recs[0].get("op") != "begin":
        raise Refused("journal has no begin record")
    return recs


def jstate(recs: list[dict[str, Any]]) -> str:
    ops = [r["op"] for r in recs]
    if "rolled_back" in ops:
        return "ROLLED_BACK"
    if "rollback_begin" in ops:
        return "ROLLING_BACK"
    if "complete" in ops:
        return "COMPLETE"
    if "catalog_done" in ops:
        return "CATALOG_DONE"
    if "data_committed" in ops:
        return "DATA_COMMITTED"
    return "IN_PROGRESS"


def _done(recs: list[dict[str, Any]], kind: str, path: str) -> dict[str, Any] | None:
    return next(
        (r for r in recs if r["op"] == "done" and r["kind"] == kind and r["path"] == path), None
    )


def _intended(recs: list[dict[str, Any]], kind: str, path: str) -> bool:
    return any(r["op"] == "intent" and r["kind"] == kind and r["path"] == path for r in recs)


@contextlib.contextmanager
def canonical_lock(canon: Path) -> Iterator[None]:
    from tsclaude.api.process import InstanceLock  # the app's own lock contract

    try:
        lock = InstanceLock(canon / "tsclaude.lock")
    except OSError as e:
        raise Refused(
            f"{canon / 'tsclaude.lock'} is held (the app or another D-17 operation is running)"
        ) from e
    try:
        yield
    finally:
        lock.release()


# --------------------------------------------------------------------------------------------- plan
def _src_monthly(src: Path) -> list[tuple[str, dict[str, Any], PartInfo]]:
    lake = Lake(src)
    out = []
    for dsid in lake.dataset_ids():
        m = lake.manifest(dsid)
        if m.get("kind") != "bars_1m" or m.get("market") != "spot" or len(m["parts"]) != 1:
            raise Refused(f"source dataset {dsid[:12]} is not a single-month spot bars_1m dataset")
        part = PartInfo.from_dict(m["parts"][0])
        if part.sealed or part.year >= 2026:
            raise Refused(f"source part {part.path} is in the sealed holdout period")
        rep = lake.verify(dsid, raw_cache_dir=src / "archive_cache")
        q = m.get("qa_summary", {})
        if not rep["ok"] or rep["sources_checked"] < 1:
            raise Refused(f"source dataset {dsid[:12]} fails verify: {rep['errors']}")
        if q.get("classification_ok") is not True or any(q.get(k) for k in QA_DIRTY):
            raise Refused(f"source dataset {dsid[:12]} QA is not clean: {q}")
        out.append((dsid, m, part))
    if not out:
        raise Refused("no source datasets")
    if len({m["symbol"] for _, m, _ in out}) != len(out):
        raise Refused("more than one source month per symbol")
    return out


def build_plan(canon: Path, src: Path) -> dict[str, Any]:
    lc, ls = Lake(canon), Lake(src)
    items = []
    for sdsid, sm, spart in _src_monthly(src):
        cands = []
        for cid in lc.dataset_ids():
            cm = lc.manifest(cid)
            if (cm.get("market"), cm.get("kind"), cm.get("symbol")) != (
                "spot",
                "bars_1m",
                sm["symbol"],
            ):
                continue
            if len(cm["parts"]) > 1 and any(
                (p["year"], p["month"]) == (spart.year, spart.month) for p in cm["parts"]
            ):
                cands.append(cid)
        if len(cands) != 1:
            raise Refused(
                f"{sm['symbol']} {spart.year}-{spart.month:02d}: expected 1 canonical dataset, found {len(cands)}"
            )
        cid = cands[0]
        cm = lc.manifest(cid)
        old = next(
            PartInfo.from_dict(p)
            for p in cm["parts"]
            if (p["year"], p["month"]) == (spart.year, spart.month)
        )
        key = f"-1m-{spart.year:04d}-{spart.month:02d}.zip"
        s_src = [
            s["sha256"]
            for s in sm["sources"]
            if s.get("role") == "data" and s["cache_path"].endswith(key)
        ]
        c_src = [
            s["sha256"]
            for s in cm["sources"]
            if s.get("role") == "data" and s["cache_path"].endswith(key)
        ]
        if not s_src or s_src != c_src:
            raise Refused(
                f"{sm['symbol']} {spart.year}-{spart.month:02d}: source archive sha differs or is missing"
            )
        if old.content_hash == spart.content_hash:
            raise Refused("canonical part already equals the replacement (nothing to repair)")
        orows = {int(r["open_time_ms"]): r for r in lc.read_part(old)}
        nrows = {int(r["open_time_ms"]): r for r in ls.read_part(spart)}
        if not set(orows) <= set(nrows):
            raise Refused("canonical part has minutes the replacement lacks")
        cols = [n for n, _ in SCHEMAS[LakeKind.BARS_1M] if n not in EXCL]
        diff = [t for t in orows if any(orows[t][c] != nrows[t][c] for c in cols)]
        if diff:
            raise Refused(f"{len(diff)} common minute(s) differ in value, first {diff[0]}")
        new_parts = [spart if p["path"] == old.path else PartInfo.from_dict(p) for p in cm["parts"]]
        items.append(
            {
                "symbol": sm["symbol"],
                "month": f"{spart.year}-{spart.month:02d}",
                "source_dataset": sdsid,
                "source_part": spart.to_dict(),
                "canonical_dataset": cid,
                "old_part": old.to_dict(),
                "added_minutes_ms": sorted(set(nrows) - set(orows)),
                "old_rows": old.rows,
                "new_rows": spart.rows,
                "composite_dataset_id": dataset_id_of(p.content_hash for p in new_parts),
                "sealed_parts_in_composite": sum(1 for p in new_parts if p.sealed),
            }
        )
    return {"created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "items": items}


def _fsync_file(path: Path) -> None:
    with path.open("rb+") as f:
        os.fsync(f.fileno())


def _write_durable(path: Path, text: str) -> None:
    """Evidence the recovery depends on is on disk before the `begin` record that relies on it (N1)."""
    with path.open("x", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def _load_pre(out: Path) -> dict[str, Any]:
    try:
        pre = json.loads((out / "inventory_pre.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise Refused(
            f"{out / 'inventory_pre.json'} is missing or unreadable: ownership cannot be proven; inspect by hand"
        ) from None
    if not isinstance(pre, dict):
        raise Refused(f"{out / 'inventory_pre.json'} is not an inventory")
    return pre


def _new_dir(out: Path) -> None:
    if out.exists():
        raise Refused(f"{out} exists: choose a new evidence folder")
    out.mkdir(parents=True)


def do_plan(canon: Path, src: Path, out: Path) -> dict[str, Any]:
    _new_dir(out)
    plan = build_plan(canon, src)
    _write_durable(out / "plan.json", json.dumps(plan, indent=2))
    return plan


# -------------------------------------------------------------------------------------------- apply
def _new_dirs(canon: Path, rel_file: str) -> list[str]:
    """Ancestor directories of rel_file (inside canon) that do not exist yet, deepest first."""
    out, d = [], (canon / rel_file).parent
    while d != canon and not d.exists():
        out.append(d.relative_to(canon).as_posix())
        d = d.parent
    return out


def do_apply(
    canon: Path, src: Path, out: Path, approval: str, bind: list[Path] | None = None
) -> None:
    if not approval.strip():
        raise Refused("--approval-ref is required")
    with canonical_lock(canon):
        plan = do_plan(canon, src, out)
        bound = {p.name: sha(p) for p in (bind or [])}
        ev = out / "source_evidence"
        for it in plan[
            "items"
        ]:  # M1: monthly datasets kept as evidence, never published into imports/
            shutil.copytree(src / "imports" / it["source_dataset"], ev / it["source_dataset"])
            for f in sorted((ev / it["source_dataset"]).rglob("*")):
                if f.is_file():
                    _fsync_file(f)
        pre = inventory(canon)
        _write_durable(out / "inventory_pre.json", json.dumps(pre, indent=0))
        (out / "backup").mkdir()
        cat_pre = pre.get("catalog.duckdb")
        if cat_pre is not None:
            shutil.copy2(canon / "catalog.duckdb", out / "backup" / "catalog.duckdb")
            _fsync_file(out / "backup" / "catalog.duckdb")
            if sha(out / "backup" / "catalog.duckdb") != cat_pre:
                raise Refused("catalog backup does not match")
        op_id = uuid.uuid4().hex
        lake = Lake(canon)
        items = []
        for it in plan["items"]:
            sp = PartInfo.from_dict(it["source_part"])
            cid = it["composite_dataset_id"]
            crel = f"imports/{cid}/manifest.json"
            if (canon / sp.path).exists() or (canon / "imports" / cid).exists():
                raise Refused(f"{it['symbol']}: target object already exists in the canonical lake")
            cm = lake.manifest(it["canonical_dataset"])
            skip = {
                "dataset_id",
                "parts",
                "sealed_parts",
                "row_count",
                "time_range",
                "lake_format_version",
                "qa_summary",
                "created_at",
                "created_at_ms",
            }
            sdir = ev / it["source_dataset"]
            meta = {k: v for k, v in cm.items() if k not in skip}
            meta.update(
                created_at=time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                created_at_ms=int(time.time() * 1000),
                d17_op_id=op_id,
                promoted_by="tests/owner_checks/d17_promote.py",
                qa_summary_note="not recomputed for the composite; see supersedes and the source dataset QA",
                supersedes={
                    "dataset_id": it["canonical_dataset"],
                    "approval_ref": approval,
                    "replaced_part": {
                        "month": it["month"],
                        "old_content_hash": it["old_part"]["content_hash"],
                        "old_rows": it["old_rows"],
                        "new_content_hash": sp.content_hash,
                        "new_rows": sp.rows,
                        "added_minutes_ms": it["added_minutes_ms"],
                    },
                    "source_dataset_id": it["source_dataset"],
                    "source_evidence_sha256": {f.name: sha(f) for f in sorted(sdir.iterdir())},
                    "bound_evidence_sha256": bound,
                },
            )
            parts = [
                it["source_part"] if p["path"] == it["old_part"]["path"] else p for p in cm["parts"]
            ]
            items.append(
                {
                    "symbol": it["symbol"],
                    "canonical_dataset": it["canonical_dataset"],
                    "part_path": sp.path,
                    "part_sha256": sp.file_sha256,
                    "part_dirs_new": _new_dirs(canon, sp.path),
                    "source_part_abs": str((src / sp.path).resolve()),
                    "composite_id": cid,
                    "composite_path": crel,
                    "composite_dirs_new": _new_dirs(canon, crel),
                    "meta": meta,
                    "parts": parts,
                    "new_rows": sp.rows,
                    "first_ms": sp.first_time_ms,
                    "last_ms": sp.last_time_ms,
                }
            )
        jappend(
            out,
            {
                "op": "begin",
                "op_id": op_id,
                "canonical": os.path.realpath(canon),
                "catalog_pre_sha256": cat_pre,
                "staging": f".staging/d17-{op_id}",
                "staging_root_new": not (canon / ".staging").exists(),
                "items": items,
            },
        )
        _forward(canon, out)


def _content_ok(lake: Lake, it: dict[str, Any], path: Path) -> bool:
    part = PartInfo.from_dict(next(p for p in it["parts"] if p["path"] == it["part_path"]))
    try:
        lake._check_part_content(part, path)
    except Exception:
        return False
    return True


def _part_state(
    canon: Path, recs: list[dict[str, Any]], pre: dict[str, Any], it: dict[str, Any]
) -> str:
    """absent | ours | incomplete (ours, torn by a power loss). Refuses anything not provably ours.

    Ours: announced in the journal, absent before the operation, and either the planned bytes, or (no
    `done` record yet) a file that does not even read back as the planned content. A readable part with
    the planned content but other bytes is another actor's file (content-addressed reuse): refused.
    """
    path = canon / it["part_path"]
    if not path.exists():
        return "absent"
    if it["part_path"] in pre or not _intended(recs, "part", it["part_path"]):
        raise Refused(f"{it['part_path']} exists but is not provably created by this operation")
    if sha(path) == it["part_sha256"]:
        return "ours"
    if _done(recs, "part", it["part_path"]) is None and not _content_ok(Lake(canon), it, path):
        return "incomplete"
    raise Refused(f"{it['part_path']} exists but is not provably created by this operation")


def _composite_state(
    canon: Path, recs: list[dict[str, Any]], pre: dict[str, Any], it: dict[str, Any], op_id: str
) -> str:
    """absent | ours | incomplete (unreadable, announced, no `done`, in a directory we created)."""
    path = canon / it["composite_path"]
    if not path.exists():
        return "absent"
    if it["composite_path"] in pre or not _intended(recs, "composite", it["composite_path"]):
        raise Refused(
            f"{it['composite_path']} exists but is not provably created by this operation"
        )
    done = _done(recs, "composite", it["composite_path"])
    try:
        m = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        if done is None and it["composite_dirs_new"]:
            return "incomplete"
        raise Refused(f"{it['composite_path']} is unreadable and not provably ours") from None
    if (
        isinstance(m, dict)
        and m.get("d17_op_id") == op_id
        and m.get("dataset_id") == it["composite_id"]
        and (done is None or done["sha256"] == sha(path))
    ):
        return "ours"
    raise Refused(f"{it['composite_path']} exists but is not provably created by this operation")


def _clean_commit_debris(canon: Path, it: dict[str, Any]) -> None:
    """Temp files `Lake.commit_dataset` leaves if killed, only inside the composite directory we created."""
    if it["composite_dirs_new"]:
        d = (canon / it["composite_path"]).parent
        if d.is_dir():
            for f in d.glob(".manifest.json.*.tmp"):
                f.unlink()


def _shared_refs(canon: Path, part_paths: set[str], own_manifests: set[str]) -> None:
    """Refuse if any other manifest in imports/ references one of the given part paths."""
    for mf in sorted((canon / "imports").glob("*/manifest.json")):
        rel = mf.relative_to(canon).as_posix()
        if rel in own_manifests:
            continue
        try:
            refs = {
                p.get("path") for p in json.loads(mf.read_text(encoding="utf-8")).get("parts", [])
            }
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            raise Refused(f"{rel} is unreadable: cannot prove the parts are not shared") from None
        shared = refs & part_paths
        if shared:
            raise Refused(
                f"{rel} references {sorted(shared)[0]}: shared by another dataset, not deleting"
            )


def _postconditions(canon: Path, begin: dict[str, Any]) -> None:
    lake = Lake(canon)
    reader = LakeReader(lake, HoldoutGuard(SealedUnlocks()))
    for it in begin["items"]:
        part = PartInfo.from_dict(next(p for p in it["parts"] if p["path"] == it["part_path"]))
        lake._check_part_content(part, canon / it["part_path"])
        m = lake.manifest(it["composite_id"])
        if (
            dataset_id_of(p["content_hash"] for p in m["parts"]) != it["composite_id"]
            or m.get("d17_op_id") != begin["op_id"]
        ):
            raise Refused(f"{it['symbol']}: composite manifest does not match its id")
        lo, hi = it["first_ms"], it["last_ms"] + 60_000
        for pin in (None, [it["composite_id"]]):
            r = reader.read_bars(
                Market.SPOT, it["symbol"], lo, hi, purpose="research", dataset_ids=pin
            )
            t = [b.open_time_ms for b in r.items]
            if len(t) != it["new_rows"] or len(set(t)) != len(t) or r.missing or r.conflicts:
                raise Refused(
                    f"{it['symbol']}: reader postcondition failed (pin={pin}): rows {len(t)}, "
                    f"missing {r.missing[:2]}, conflicts {len(r.conflicts)}"
                )


def _preserved(canon: Path, pre: dict[str, Any], allow_catalog: bool) -> None:
    now = inventory(canon)
    bad = [
        k
        for k, v in pre.items()
        if now.get(k) != v and not (allow_catalog and k == "catalog.duckdb")
    ]
    if bad:
        raise Refused(f"pre-existing objects changed or removed: {bad[:5]}")


def _forward(canon: Path, out: Path) -> None:
    """Idempotent: performs whatever the journal and the bytes show as not yet done. Caller holds the lock."""
    recs = jread(out)
    begin = recs[0]
    op_id = begin["op_id"]
    pre = _load_pre(out)
    lake = Lake(canon)
    staging = canon / begin["staging"]
    for it in begin["items"]:  # stage 1: parts (invisible: no manifest refers to them yet)
        st = _part_state(canon, recs, pre, it)
        if st == "ours":
            if not _done(recs, "part", it["part_path"]):
                jappend(
                    out,
                    {
                        "op": "done",
                        "kind": "part",
                        "path": it["part_path"],
                        "sha256": it["part_sha256"],
                    },
                )
            continue
        if (
            st == "incomplete"
        ):  # our own rename survived a power loss but its bytes did not: redo it
            _shared_refs(canon, {it["part_path"]}, set())
            jappend(
                out, {"op": "intent_delete", "kind": "part_incomplete", "path": it["part_path"]}
            )
            (canon / it["part_path"]).unlink()
        if not _intended(recs, "part", it["part_path"]):
            jappend(
                out,
                {
                    "op": "intent",
                    "kind": "part",
                    "path": it["part_path"],
                    "sha256": it["part_sha256"],
                },
            )
        tmp = staging / uuid.uuid4().hex / Path(it["part_path"]).name
        tmp.parent.mkdir(parents=True)
        shutil.copyfile(it["source_part_abs"], tmp)
        _fsync_file(tmp)  # the bytes are durable before the rename that publishes them
        if sha(tmp) != it["part_sha256"]:
            raise Refused("copied part differs from the source part")
        part = PartInfo.from_dict(next(p for p in it["parts"] if p["path"] == it["part_path"]))
        lake._check_part_content(part, tmp)
        _fault("before_part_publish")
        final = canon / it["part_path"]
        final.parent.mkdir(parents=True, exist_ok=True)
        os.rename(tmp, final)  # Windows: fails if the target exists
        _fault("after_part_rename")
        jappend(
            out,
            {"op": "done", "kind": "part", "path": it["part_path"], "sha256": it["part_sha256"]},
        )
        _fault("after_part_publish")
        recs = jread(out)
    shutil.rmtree(staging, ignore_errors=True)
    if begin.get("staging_root_new"):
        with contextlib.suppress(OSError):
            (canon / ".staging").rmdir()  # only if empty
    recs = jread(out)
    for n, it in enumerate(
        begin["items"]
    ):  # stage 2: composites, consecutively (visibility points)
        st = _composite_state(canon, recs, pre, it, op_id)
        if st == "ours":
            if not _done(recs, "composite", it["composite_path"]):
                _fsync_file(canon / it["composite_path"])
                jappend(
                    out,
                    {
                        "op": "done",
                        "kind": "composite",
                        "path": it["composite_path"],
                        "sha256": sha(canon / it["composite_path"]),
                    },
                )
            continue
        # an empty/torn manifest of ours (power loss). It is not invisible: Lake.index() fails on it
        # for every reader until this step replaces it (N8, fail closed).
        if st == "incomplete":
            jappend(
                out,
                {
                    "op": "intent_delete",
                    "kind": "composite_incomplete",
                    "path": it["composite_path"],
                },
            )
            (canon / it["composite_path"]).unlink()
        if n > 0:
            _fault("between_composites")
        jappend(out, {"op": "intent", "kind": "composite", "path": it["composite_path"]})
        _clean_commit_debris(canon, it)
        dsid, made = lake.commit_dataset(
            [PartInfo.from_dict(p) for p in it["parts"]], it["meta"], None
        )
        if not made or dsid != it["composite_id"]:
            raise Refused(f"composite id mismatch: {dsid} vs {it['composite_id']}")
        _fsync_file(canon / it["composite_path"])  # durable before `done` is journaled
        _fault("after_composite_commit")
        jappend(
            out,
            {
                "op": "done",
                "kind": "composite",
                "path": it["composite_path"],
                "sha256": sha(canon / it["composite_path"]),
            },
        )
        recs = jread(out)
    _fault("before_data_committed")
    recs = jread(out)
    if jstate(recs) == "IN_PROGRESS":
        _postconditions(canon, begin)
        _preserved(canon, pre, allow_catalog=True)
        jappend(out, {"op": "data_committed"})
    recs = jread(out)
    if jstate(recs) == "DATA_COMMITTED":  # stage 3: derived catalog, separately recoverable
        if begin["catalog_pre_sha256"] is None:
            # no catalog existed before: none is created (minimal canonical change; it is derived data)
            _fault("before_catalog")
            jappend(
                out,
                {"op": "catalog_done", "sha256": None, "note": "no catalog before; none created"},
            )
        else:
            cur = sha(canon / "catalog.duckdb") if (canon / "catalog.duckdb").is_file() else None
            if cur != begin["catalog_pre_sha256"] and not _catalog_is_ours(canon, begin, pre):
                raise Refused("catalog.duckdb changed by another actor; not replacing it")
            jappend(out, {"op": "catalog_intent"})
            _fault("before_catalog")
            lake.build_catalog()
            jappend(out, {"op": "catalog_done", "sha256": sha(canon / "catalog.duckdb")})
        _fault("after_catalog")
    recs = jread(out)
    if jstate(recs) == "CATALOG_DONE":
        _postconditions(canon, begin)
        _preserved(canon, pre, allow_catalog=True)
        summary = [
            {k: it[k] for k in ("symbol", "composite_id", "part_path", "canonical_dataset")}
            for it in begin["items"]
        ]
        (out / "COMPLETE.json").write_text(
            json.dumps({"op_id": op_id, "items": summary}, indent=2), encoding="utf-8"
        )
        jappend(out, {"op": "complete"})


def _catalog_datasets(path: Path) -> set[str] | None:
    try:
        import duckdb

        con = duckdb.connect(str(path), read_only=True)
        try:
            return {r[0] for r in con.execute("SELECT dataset_id FROM datasets").fetchall()}
        finally:
            con.close()
    except Exception:
        return None


def _catalog_is_ours(canon: Path, begin: dict[str, Any], pre: dict[str, Any]) -> bool:
    """A catalog rebuilt from exactly the pre-existing datasets plus our composites (ours or an
    equivalent rebuild): safe to replace or restore. Anything else belongs to another actor."""
    pre_ids = {
        k.split("/")[1]
        for k in pre
        if k.startswith("imports/") and k.endswith("/manifest.json") and len(k.split("/")[1]) == 64
    }
    got = _catalog_datasets(canon / "catalog.duckdb")
    return got is not None and got == pre_ids | {it["composite_id"] for it in begin["items"]}


def do_recover(canon: Path, out: Path) -> str:
    with canonical_lock(canon):
        recs = jread(out)
        if os.path.realpath(canon) != recs[0]["canonical"]:
            raise Refused("canonical folder differs from the journal's")
        st = jstate(recs)
        if st in ("ROLLING_BACK", "ROLLED_BACK"):
            raise Refused(f"state {st}: finish with rollback, not recover")
        if st != "COMPLETE":
            _forward(canon, out)
        return jstate(jread(out))


# ----------------------------------------------------------------------------------------- rollback
def do_rollback(canon: Path, out: Path) -> str:
    with canonical_lock(canon):
        recs = jread(out)
        begin = recs[0]
        if os.path.realpath(canon) != begin["canonical"]:
            raise Refused("canonical folder differs from the journal's")
        if jstate(recs) == "ROLLED_BACK":
            return "ROLLED_BACK"
        pre = _load_pre(out)
        op_id = begin["op_id"]
        # ---- every precondition is checked before the first destructive step
        own_parts = [it for it in begin["items"] if _part_state(canon, recs, pre, it) != "absent"]
        own_comps = [
            it for it in begin["items"] if _composite_state(canon, recs, pre, it, op_id) != "absent"
        ]
        _shared_refs(
            canon,
            {it["part_path"] for it in begin["items"]},
            {it["composite_path"] for it in begin["items"]},
        )
        runs = canon / "runs"
        if runs.is_dir():
            ids = [it["composite_id"] for it in begin["items"]]
            for f in runs.rglob("*.json"):
                if any(i in f.read_text(encoding="utf-8", errors="replace") for i in ids):
                    raise Refused(f"{f} pins a composite dataset of this operation")
        _preserved(canon, pre, allow_catalog=True)  # nobody changed a pre-existing file meanwhile
        cat = canon / "catalog.duckdb"
        restore = False
        if (
            begin["catalog_pre_sha256"] is not None
        ):  # with no catalog before, the catalog is never touched
            cur = sha(cat) if cat.is_file() else None
            restore = cur != begin["catalog_pre_sha256"]
            if restore and not _catalog_is_ours(canon, begin, pre):
                raise Refused(
                    "catalog.duckdb was changed by another actor; refusing to overwrite it"
                )
            backup = out / "backup" / "catalog.duckdb"
            if restore and (not backup.is_file() or sha(backup) != begin["catalog_pre_sha256"]):
                raise Refused(
                    "catalog backup is missing or does not match the pre-operation catalog"
                )
        # ---- destructive steps, each announced in the journal first
        if jstate(recs) != "ROLLING_BACK":
            jappend(out, {"op": "rollback_begin"})
        for it in own_comps:  # composites first: makes the repair invisible
            jappend(out, {"op": "intent_delete", "kind": "composite", "path": it["composite_path"]})
            (canon / it["composite_path"]).unlink()
            _fault("rollback_after_composite_delete")
        for it in begin["items"]:
            if _intended(recs, "composite", it["composite_path"]):
                _clean_commit_debris(canon, it)
            for d in it["composite_dirs_new"]:
                with contextlib.suppress(OSError):
                    (canon / d).rmdir()  # only if empty: never removes another actor's file
        for it in own_parts:
            jappend(out, {"op": "intent_delete", "kind": "part", "path": it["part_path"]})
            (canon / it["part_path"]).unlink()
        for it in begin["items"]:
            for d in it["part_dirs_new"]:
                with contextlib.suppress(OSError):
                    (canon / d).rmdir()
        shutil.rmtree(canon / begin["staging"], ignore_errors=True)
        if begin.get("staging_root_new"):
            with contextlib.suppress(OSError):
                (canon / ".staging").rmdir()
        if restore:
            jappend(out, {"op": "intent_restore_catalog"})
            tmp = canon / f".catalog.d17-{op_id}.tmp"
            shutil.copyfile(out / "backup" / "catalog.duckdb", tmp)
            os.replace(tmp, cat)
            _fault("rollback_after_catalog_restore")
        _preserved(canon, pre, allow_catalog=False)
        for it in begin["items"]:
            if (canon / it["part_path"]).exists() or (canon / it["composite_path"]).exists():
                raise Refused("an owned object is still present after rollback")
        jappend(out, {"op": "rolled_back"})
        return "ROLLED_BACK"


def do_status(out: Path) -> dict[str, Any]:
    recs = jread(out)
    return {
        "op_id": recs[0]["op_id"],
        "state": jstate(recs),
        "accepted": jstate(recs) == "COMPLETE",
        "records": len(recs),
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("cmd", choices=["plan", "apply", "recover", "rollback", "status"])
    ap.add_argument("--canonical", type=Path)
    ap.add_argument("--source", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--approval-ref", default="")
    ap.add_argument("--bind", type=Path, action="append", default=[])
    a = ap.parse_args(argv)
    if a.cmd != "status" and a.canonical is None:
        print("--canonical is required", file=sys.stderr)
        return 2
    if a.cmd in ("plan", "apply") and a.source is None:
        print("--source is required", file=sys.stderr)
        return 2
    try:
        if a.cmd == "plan":
            print(json.dumps(do_plan(a.canonical, a.source, a.out), indent=2))
        elif a.cmd == "apply":
            do_apply(a.canonical, a.source, a.out, a.approval_ref, a.bind)
            print(json.dumps(do_status(a.out)))
        elif a.cmd == "recover":
            print(do_recover(a.canonical, a.out))
        elif a.cmd == "rollback":
            print(do_rollback(a.canonical, a.out))
        else:
            print(json.dumps(do_status(a.out)))
    except Refused as e:
        print("REFUSED:", e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
