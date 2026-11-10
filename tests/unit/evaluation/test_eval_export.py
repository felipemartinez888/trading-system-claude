"""M6 evaluation export: a real event store with deterministic paper sessions is exported,
verified and scored; the scorer's numbers must equal the runtime's own numbers for the same
sessions (gate: "a scorer reproduces dashboard metrics from the export alone"), and every kind of
tampering must fail verification."""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from eval_helpers import Built, build_store, config

from tsclaude.contracts.events import AggregateType, EventType, NewEvent
from tsclaude.contracts.timeutil import ManualClock
from tsclaude.evaluation import scorer as scorer_mod
from tsclaude.evaluation.export import ExportError, build_lines, export_store
from tsclaude.evaluation.scorer import (
    FILES,
    MANIFEST,
    ExportInvalid,
    SchemaSet,
    canonical_json,
    check_export,
    line_hash,
    score_directory,
)
from tsclaude.evaluation.verify import verify_export
from tsclaude.store.eventstore import EventStore

EXPORTED_AT = 1_735_700_000_000
SCHEMA_DIR = scorer_mod.SCHEMA_DIR  # the bundled set of the current format version (v2)


# ---------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Built:
    return build_store(tmp_path_factory.mktemp("store") / "state.db")


@pytest.fixture(scope="module")
def exported(built: Built, tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("export") / "export"
    with EventStore(built.path) as store:
        export_store(store, out, exported_at_ms=EXPORTED_AT)
    return out


@pytest.fixture(scope="module")
def scored(exported: Path) -> dict[str, Any]:
    return score_directory(exported)


def _copy(src: Path, tmp_path: Path) -> Path:
    dst = tmp_path / "copy"
    shutil.copytree(src, dst)
    return dst


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def _rewrite(root: Path, name: str, lines: list[dict[str, Any]]) -> None:
    """Rewrite a file *consistently*: renumber, relink and rehash its lines, then fix the
    manifest's count and hashes (a careful forger, not a careless editor)."""
    prev = "0" * 64
    for n, line in enumerate(lines, 1):
        line["line_no"] = n
        line["prev_line_hash"] = prev
        line["line_hash"] = line_hash(line)
        prev = line["line_hash"]
    data = "".join(canonical_json(x) + "\n" for x in lines).encode("utf-8")
    (root / name).write_bytes(data)
    manifest = json.loads((root / MANIFEST).read_bytes())
    manifest["files"][name] = {
        "lines": len(lines),
        "sha256": hashlib.sha256(data).hexdigest(),
        "last_line_hash": lines[-1]["line_hash"] if lines else None,
    }
    (root / MANIFEST).write_bytes((canonical_json(manifest) + "\n").encode("utf-8"))


def _num(value: Any) -> Any:
    return Decimal(value) if isinstance(value, str) else value


def _same(a: Any, b: Any) -> bool:
    """Equal as JSON values, decimal strings compared by value ("0" == "0E-8")."""
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, str) and isinstance(b, str) and a != b:
        try:
            return Decimal(a) == Decimal(b)
        except ArithmeticError:
            return False
    return bool(a == b)


# ---------------------------------------------------------------------------- the gate
def test_export_validates_and_verifies_against_the_store(built: Built, exported: Path) -> None:
    report, export = check_export(exported)
    assert report.ok, report.problems
    assert export is not None
    manifest = export.manifest
    assert manifest["paper_sessions"] == ["paper", "paper-2"]
    assert manifest["not_exported"] == {}
    assert manifest["chain_verified"] is True
    with EventStore(built.path) as store:
        assert manifest["chain_head"] == {"seq": store.head[0], "hash": store.head[1]}
        assert report.lines == store.count()  # every event of this store has a record family
        full = verify_export(exported, store=store)
    assert full.ok, full.problems
    assert full.store_checked


def test_every_line_validates_against_its_schema(exported: Path) -> None:
    schemas = SchemaSet()
    seen: set[str] = set()
    for name in FILES:
        for line in _lines(exported / name):
            assert schemas.validate(line, name.replace(".jsonl", ".schema.json")) == []
            seen.add(line["record_type"])
    for kind in (
        "paper_step",
        "signal_created",
        "signal_transitioned",
        "signal_shadow_outcome",
        "sim_order_filled",
        "sim_position_opened",
        "sim_position_closed",
        "sim_ledger_posted",
        "sim_request_rejected",
        "sim_run_started",
        "sim_bar_gap",
        "session_started",
        "session_crash_detected",
        "monitoring_started",
        "monitoring_stopped",
        "session_stopped",
    ):
        assert kind in seen, kind


def test_scorer_reproduces_the_service_status_books(built: Built, scored: dict[str, Any]) -> None:
    """Every value of `PaperTradingService.status()["books"]` (the API's ``summary``) and the
    ledger hashes, for both sessions, including the session restored after a restart."""
    for sid, svc in (("paper", built.service), ("paper-2", built.service2)):
        got = scored["paper_sessions"][sid]
        status = svc.status()
        assert list(got["books"]) == list(status["books"])
        derivable_missing = {"no_trade_deferrals"}  # listed in NOT_DERIVABLE
        for name, ref in status["books"].items():
            mine = got["books"][name]
            for key, value in ref.items():
                if key in derivable_missing or key == "book":
                    continue
                if key == "ledger_ok":
                    assert mine["ledger_reconciled"] is value
                    continue
                assert key in mine, key
                assert _num(mine[key]) == _num(value), (sid, name, key, mine[key], value)
            assert mine["ledger_hash"] == status["ledger_hashes"][name]
        assert got["batches"] == status["batches"]
        assert got["last_minute_ms"] == status["last_minute_ms"]


def test_scorer_reproduces_the_published_dashboard_snapshot(
    built: Built, scored: dict[str, Any], tmp_path: Path
) -> None:
    """The snapshot `PaperRuntime` publishes is what `/api/paper/books|fills|activity` serve:
    book values (equity, cash, unrealized P&L, positions, halted, ledger reconciled), the fills,
    the closed trades and the request rejections."""
    from tsclaude.runtime.paper_runtime import PaperRuntime, PaperSettings

    with EventStore(built.path) as store:
        for sid, svc in (("paper", built.service), ("paper-2", built.service2)):
            runtime = PaperRuntime(
                PaperSettings(enabled=True, session_id=sid),
                store,
                ManualClock(0),
                config_factory=config,
                audit_path=tmp_path / f"{sid}.jsonl",
            )
            runtime.service = svc
            runtime.session_id = sid
            runtime._publish()  # the code path that feeds the API; nothing else is computed
            snap, _ = runtime.published()
            got = scored["paper_sessions"][sid]
            assert snap["books"], sid
            for name, ref in snap["books"].items():
                mine = {k: got["books"][name][k] for k in ref if k != "no_trade_deferrals"}
                assert _same(mine, {k: v for k, v in ref.items() if k != "no_trade_deferrals"})
            assert _same(got["fills"][-200:], snap["fills"])
            assert _same(got["trades"], snap["trades"])
            assert _same(got["request_rejections"][-200:], snap["request_rejections"])
            assert snap["values_as_of_ms"] == got["values_as_of_ms"]
    paper = scored["paper_sessions"]["paper"]
    assert len(paper["trades"]) == 5
    assert len(paper["request_rejections"]) == 1
    short = scored["paper_sessions"]["paper-2"]["books"]["shadow"]["positions"]
    assert Decimal(short["usdm:BTCUSDT"]) < 0  # the open USD-M short is marked


def test_scorer_reproduces_the_signals_page(built: Built, scored: dict[str, Any]) -> None:
    from tsclaude.api.market_views import signals_response

    with EventStore(built.path) as store:
        api = signals_response(store, 0, 200)
    assert api.signals.provenance == "DEMO"
    want = [s.model_dump(mode="json") for s in api.signals.value or []]
    assert len(want) == 2
    assert scored["demo_signals"]["provenance"] == "DEMO"
    assert scored["demo_signals"]["items"] == want


def test_api_routes_match_the_scorer(built: Built, scored: dict[str, Any], tmp_path: Path) -> None:
    """Through the real HTTP routes (needs fastapi; runs in CI)."""
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from tsclaude.api.paper import paper_router
    from tsclaude.runtime.paper_runtime import PaperRuntime, PaperSettings

    with EventStore(built.path) as store:
        runtime = PaperRuntime(
            PaperSettings(enabled=True),
            store,
            ManualClock(0),
            config_factory=config,
            audit_path=tmp_path / "a.jsonl",
        )
        runtime.service = built.service
        runtime.session_id = "paper"
        runtime._publish()
        app = fastapi.FastAPI()
        app.include_router(paper_router(lambda: runtime, ManualClock(0), []))
        client = TestClient(app)
        books = client.get("/api/paper/books").json()["books"]["value"]
        activity = client.get("/api/paper/activity", params={"limit": 200}).json()["activity"]
        fills = client.get("/api/paper/fills", params={"limit": 200}).json()["fills"]["value"]
    got = scored["paper_sessions"]["paper"]
    for name, ref in books["books"].items():
        assert _same(
            {k: got["books"][name][k] for k in ref if k != "no_trade_deferrals"},
            {k: v for k, v in ref.items() if k != "no_trade_deferrals"},
        )
    for name, ref in books["summary"].items():
        assert _num(got["books"][name]["max_drawdown"]) == _num(ref["max_drawdown"])
        assert got["books"][name]["trades"] == ref["trades"]
    assert books["ledger_hashes"] == {n: b["ledger_hash"] for n, b in got["books"].items()}
    assert _same(got["fills"], fills)
    assert _same(got["trades"], activity["value"]["trades"])
    assert _same(got["request_rejections"], activity["value"]["request_rejections"])


def test_kill_switch_halt_and_operator_reset(
    built: Built, scored: dict[str, Any], tmp_path: Path
) -> None:
    """paper-2's baseline gapped through its stop, halted on the drawdown kill switch and was
    reset by the operator: halted while the export ends before the reset, None after it."""
    with EventStore(built.path) as store:
        export_store(store, tmp_path / "halted", upto_seq=built.halted_head_seq)
    while_halted = score_directory(tmp_path / "halted")["paper_sessions"]["paper-2"]["books"]
    assert while_halted["baseline"]["halted"] == "drawdown_kill_switch"
    assert while_halted["shadow"]["halted"] is None
    final = scored["paper_sessions"]["paper-2"]["books"]["baseline"]
    assert final["halted"] is None
    assert Decimal(final["max_drawdown"]) > Decimal("0.10")


def test_ledger_reconciliation_detects_an_unbalanced_posting() -> None:
    book = scorer_mod._Book("b", Decimal(100))
    book.ledger.append(
        {"tx_id": 1, "ts_ms": 0, "memo": "capital", "legs": [["cash", "100"], ["capital", "-100"]]}
    )
    assert book.reconcile() == (True, Decimal(100))
    book.ledger.append({"tx_id": 2, "ts_ms": 0, "memo": "x", "legs": [["cash", "1"]]})
    assert book.reconcile()[0] is False
    book.ledger[1] = {"tx_id": 3, "ts_ms": 0, "memo": "x", "legs": [["cash", "1"], ["fees", "-1"]]}
    assert book.reconcile()[0] is False  # transaction ids not consecutive


def test_bar_step_coverage_counts(built: Built, scored: dict[str, Any]) -> None:
    """Export summary (not a dashboard value). Every bar of these sessions was observed; the
    unobserved branch is not exercised here (no downtime in the scenario)."""
    resets = {"paper": 0, "paper-2": 1}
    for sid, svc in (("paper", built.service), ("paper-2", built.service2)):
        steps = scored["paper_sessions"][sid]["bar_steps"]
        assert steps == {"observed": svc.session.batches - resets[sid], "unobserved": 0}


# ---------------------------------------------------------------------------- provenance
def test_provenance_labels_are_preserved(exported: Path) -> None:
    signals = _lines(exported / "signals.jsonl")
    demo = [x for x in signals if x["event"]["payload"]["strategy_id"] == "demo_detector"]
    paper = [x for x in signals if x["paper_session_id"] is not None]
    assert len(demo) == 2
    assert {x["provenance"] for x in demo} == {"DEMO"}
    assert {x["provenance"] for x in paper} == {"SIMULATED"}
    assert len(demo) + len(paper) == len(signals)
    assert {x["provenance"] for x in _lines(exported / "coverage.jsonl")} == {"SYSTEM"}
    steps = _lines(exported / "steps.jsonl")
    assert {x["provenance"] for x in steps if x["event"]["payload"]["kind"] == "bar"} == {"LIVE"}
    resets = [x for x in steps if x["event"]["payload"]["kind"] == "reset"]
    assert len(resets) == 1
    assert resets[0]["provenance"] == "SIMULATED"
    for name in ("fills.jsonl", "positions.jsonl", "ledger.jsonl", "orders.jsonl"):
        assert {x["provenance"] for x in _lines(exported / name)} == {"SIMULATED"}


def test_demo_line_relabelled_live_fails(exported: Path, tmp_path: Path) -> None:
    root = _copy(exported, tmp_path)
    lines = _lines(root / "signals.jsonl")
    for line in lines:
        if line["provenance"] == "DEMO":
            line["provenance"] = "LIVE"
    _rewrite(root, "signals.jsonl", lines)
    report, _ = check_export(root)
    assert not report.ok
    assert any("signals.jsonl" in p and "schema" in p for p in report.problems)


# ---------------------------------------------------------------------------- tampering
def test_edited_line_fails(exported: Path, tmp_path: Path) -> None:
    root = _copy(exported, tmp_path)
    raw = (root / "fills.jsonl").read_text(encoding="utf-8").splitlines()
    line = json.loads(raw[2])
    line["event"]["payload"]["price"] = "1.0"
    raw[2] = canonical_json(line)
    (root / "fills.jsonl").write_bytes(("\n".join(raw) + "\n").encode("utf-8"))
    report, export = check_export(root)
    assert export is None
    assert any("line_hash mismatch" in p for p in report.problems)
    assert any("sha256 does not match" in p for p in report.problems)
    with pytest.raises(ExportInvalid):
        score_directory(root)


def test_consistently_rehashed_edit_still_fails(
    built: Built, exported: Path, tmp_path: Path
) -> None:
    """A forger who rehashes the line and the manifest is caught by the source event hash; one
    who also recomputes the event hash breaks the chain link; and the store catches both."""
    root = _copy(exported, tmp_path)
    lines = _lines(root / "ledger.jsonl")
    lines[3]["event"]["payload"]["legs"][0][1] = "999999.00000000"
    _rewrite(root, "ledger.jsonl", lines)
    report, _ = check_export(root)
    assert any("source event hash does not match" in p for p in report.problems)

    lines[3]["event"]["hash"] = scorer_mod.chain_hash(
        lines[3]["event"]["prev_hash"], scorer_mod.event_body(lines[3]["event"])
    )
    _rewrite(root, "ledger.jsonl", lines)
    report, _ = check_export(root)
    assert not report.ok
    assert any("prev_hash does not link" in p for p in report.problems)
    with EventStore(built.path) as store:
        assert not verify_export(root, store=store).ok


def test_missing_line_fails(built: Built, exported: Path, tmp_path: Path) -> None:
    # careless: a line cut out of the middle
    root = _copy(exported, tmp_path)
    raw = (root / "positions.jsonl").read_text(encoding="utf-8").splitlines()
    del raw[4]
    (root / "positions.jsonl").write_bytes(("\n".join(raw) + "\n").encode("utf-8"))
    report, _ = check_export(root)
    assert any("prev_line_hash" in p for p in report.problems)
    assert any("manifest says" in p for p in report.problems)
    # careful: removed, renumbered, rehashed, manifest fixed -> the paper batch is incomplete
    root2 = tmp_path / "careful"
    shutil.copytree(exported, root2)
    lines = _lines(root2 / "ledger.jsonl")
    del lines[5]
    _rewrite(root2, "ledger.jsonl", lines)
    report, _ = check_export(root2)
    assert not report.ok
    assert any("paper batch at seq" in p for p in report.problems)
    with EventStore(built.path) as store:
        assert not verify_export(root2, store=store).ok


def test_missing_file_or_manifest_fails(exported: Path, tmp_path: Path) -> None:
    root = _copy(exported, tmp_path)
    (root / "coverage.jsonl").unlink()
    report, _ = check_export(root)
    assert "coverage.jsonl: missing" in report.problems
    (root / MANIFEST).unlink()
    report, _ = check_export(root)
    assert report.problems == [f"{MANIFEST} is missing"]


def test_export_of_another_store_does_not_verify_against_this_one(
    built: Built, exported: Path, tmp_path: Path
) -> None:
    with EventStore(tmp_path / "other.db") as other:
        other.append(
            NewEvent(
                type=EventType.SESSION_STARTED,
                aggregate_type=AggregateType.SESSION,
                aggregate_id="x",
                ts_ms=1,
                payload={"session_id": "x"},
            )
        )
        report = verify_export(exported, store=other)
    assert not report.ok


# ---------------------------------------------------------------------------- determinism
def test_two_exports_of_the_same_store_are_byte_identical(
    built: Built, exported: Path, tmp_path: Path
) -> None:
    with EventStore(built.path) as store:  # a fresh handle on the same file
        export_store(store, tmp_path / "again", exported_at_ms=EXPORTED_AT)
        export_store(store, tmp_path / "no_time")
    for name in [*FILES, MANIFEST]:
        assert (tmp_path / "again" / name).read_bytes() == (exported / name).read_bytes(), name
    for name in FILES:  # the export time lives in the manifest only
        assert (tmp_path / "no_time" / name).read_bytes() == (exported / name).read_bytes()


def test_prefix_export_and_a_grown_store(built: Built, tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    shutil.copy(built.path, path)
    with EventStore(path) as store:
        files, _, _, _ = build_lines(store)
        first_step = next(x for x in files["steps.jsonl"])["event"]["seq"]
        with pytest.raises(ExportError, match="cut by the chain head"):
            export_store(store, tmp_path / "cut", upto_seq=first_step + 1)
        boundary = first_step - 1
        export_store(store, tmp_path / "prefix", upto_seq=boundary)
        store.append(
            NewEvent(
                type=EventType.SESSION_STARTED,
                aggregate_type=AggregateType.SESSION,
                aggregate_id="later",
                ts_ms=2,
                payload={"session_id": "later"},
            )
        )
        report = verify_export(tmp_path / "prefix", store=store)
        assert report.ok, report.problems
        assert report.chain_head is not None
        assert report.chain_head["seq"] == boundary < store.head[0]


# ---------------------------------------------------------------------------- fail closed
def test_broken_chain_refuses_export(built: Built, tmp_path: Path) -> None:
    path = tmp_path / "broken.db"
    shutil.copy(built.path, path)
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER events_no_update")
    conn.execute("UPDATE events SET payload = '{}' WHERE seq = 3")
    conn.commit()
    conn.close()
    with EventStore(path) as store, pytest.raises(ExportError, match="hash chain is broken"):
        export_store(store, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_unclassifiable_signal_and_non_empty_output_are_refused(tmp_path: Path) -> None:
    with EventStore(tmp_path / "s.db") as store:
        store.append(
            NewEvent(
                type=EventType.SIGNAL_CREATED,
                aggregate_type=AggregateType.SIGNAL,
                aggregate_id="S" * 26,
                ts_ms=1,
                payload={"strategy_id": "something"},
            )
        )
        with pytest.raises(ExportError, match="provenance cannot be established"):
            export_store(store, tmp_path / "out")
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "x").write_text("x")
    with EventStore(tmp_path / "empty.db") as store, pytest.raises(ExportError, match="empty"):
        export_store(store, busy)


def test_empty_store_exports_and_verifies(tmp_path: Path) -> None:
    with EventStore(tmp_path / "e.db") as store:
        export_store(store, tmp_path / "out")
        assert verify_export(tmp_path / "out", store=store).ok
    result = score_directory(tmp_path / "out")
    assert result["paper_sessions"] == {}
    assert result["demo_signals"]["items"] == []


# ---------------------------------------------------------------------------- independence
def test_scorer_imports_only_the_standard_library() -> None:
    tree = ast.parse(Path(scorer_mod.__file__).read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            modules.add((node.module or "").split(".")[0])
    assert modules - {"__future__"} <= set(sys.stdlib_module_names), modules


def test_scorer_runs_standalone_from_a_copy(
    exported: Path, scored: dict[str, Any], tmp_path: Path
) -> None:
    """Only scorer.py and the schema files, outside the package, in isolated mode."""
    kit = tmp_path / "kit"
    kit.mkdir()
    shutil.copy(scorer_mod.__file__, kit / "scorer.py")
    shutil.copytree(SCHEMA_DIR, kit / "schemas")
    out = subprocess.run(
        [
            sys.executable,
            "-I",
            str(kit / "scorer.py"),
            str(exported),
            "--schemas",
            str(kit / "schemas"),
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
    )
    result = json.loads(out.stdout)
    assert result["check"]["ok"] is True
    assert result["score"] == json.loads(json.dumps(scored))


def test_not_derivable_metrics_are_listed(scored: dict[str, Any]) -> None:
    text = json.dumps(scored["not_derivable"])
    for item in ("watch", "no_trade_deferrals", "audit log", "STALE"):
        assert item.lower() in text.lower(), item
    assert scored["protocol_metrics_not_scored"]


def test_fill_time_rejections_are_counted_by_reason() -> None:
    """Direct test of the counter: the end-to-end scenarios produce request-level rejections
    only (risk gate, symbol busy, short on spot), no fill-time order rejection."""

    def line(seq: int, kind: str, payload: dict[str, Any]) -> scorer_mod.Line:
        event = {"seq": seq, "ts_ms": seq, "type": kind, "aggregate_id": "x", "payload": payload}
        return scorer_mod.Line(
            "x",
            {
                "record_type": kind,
                "event": event,
                "paper_session_id": "p",
                "provenance": "SIMULATED",
            },
        )

    books = [{"book": "baseline", "starting_equity": "100"}]
    lines = [
        line(1, "sim_run_started", {"books": books}),
        line(2, "sim_order_rejected", {"book": "baseline", "order_id": "a", "reason": "r1"}),
        line(3, "sim_order_rejected", {"book": "baseline", "order_id": "b", "reason": "r1"}),
        line(4, "sim_order_rejected", {"book": "baseline", "order_id": "c", "reason": "r0"}),
    ]
    got = scorer_mod.score_session(lines)["books"]["baseline"]["rejections_by_reason"]
    assert got == {"r0": 1, "r1": 2}
    assert list(got) == ["r0", "r1"]


def test_a_prefix_export_verifies_but_says_it_does_not_cover_the_store(
    built: Built, tmp_path: Path
) -> None:
    """Overnight review M5: a valid prefix is not the whole session; the report says so."""
    with EventStore(built.path) as store:
        export_store(store, tmp_path / "halted", upto_seq=built.halted_head_seq)
        full_head, _ = store.head
        prefix = verify_export(tmp_path / "halted", store=store).to_dict()
        export_store(store, tmp_path / "full")
        whole = verify_export(tmp_path / "full", store=store).to_dict()
    assert prefix["ok"] is True
    assert prefix["store_head_seq"] == full_head
    assert prefix["events_after_head"] == full_head - built.halted_head_seq > 0
    assert prefix["covers_whole_store"] is False
    assert whole["covers_whole_store"] is True
    alone = verify_export(tmp_path / "full").to_dict()
    assert alone["store_checked"] is False
    assert alone["covers_whole_store"] is None


# ------------------------------------------------------------------ format versions (M5-3: v2)
def _as_v1(src: Path, dst: Path) -> None:
    """The v1 form of a v2 export without agent events. Exporting one store with the 9c681ca
    exporter (v1) and with this one, then applying this rewrite, gave byte-identical directories
    (devlog 2026-11-10-m5-runtime-wiring, "Export format v2")."""
    from tsclaude.evaluation.scorer import FILES_BY_VERSION, GENESIS, schema_dir_for

    dst.mkdir(parents=True)
    manifest = json.loads((src / MANIFEST).read_text("utf-8"))
    assert manifest["files"]["agents.jsonl"]["lines"] == 0
    files = {}
    for name in FILES_BY_VERSION[1]:
        prev, out = GENESIS, []
        for raw in (src / name).read_text("utf-8").splitlines():
            obj = json.loads(raw)
            obj["format_version"] = 1
            obj["prev_line_hash"] = prev
            obj.pop("line_hash")
            obj["line_hash"] = scorer_mod.line_hash(obj)
            prev = obj["line_hash"]
            out.append(scorer_mod.canonical_json(obj))
        data = "".join(x + "\n" for x in out).encode("utf-8")
        (dst / name).write_bytes(data)
        files[name] = {
            "lines": len(out),
            "sha256": hashlib.sha256(data).hexdigest(),
            "last_line_hash": prev if out else None,
        }
    manifest.update(format_version=1, files=files, schemas=SchemaSet(schema_dir_for(1)).digests())
    (dst / MANIFEST).write_text(scorer_mod.canonical_json(manifest) + "\n", encoding="utf-8")


def test_the_current_export_is_format_v2_with_an_empty_agents_file(exported: Path) -> None:
    manifest = json.loads((exported / MANIFEST).read_text("utf-8"))
    assert manifest["format_version"] == scorer_mod.FORMAT_VERSION == 2
    assert manifest["files"]["agents.jsonl"] == {
        "lines": 0,
        "sha256": hashlib.sha256(b"").hexdigest(),
        "last_line_hash": None,
    }


def test_a_v1_export_is_still_checked_and_scored(
    built: Built, exported: Path, scored: dict[str, Any], tmp_path: Path
) -> None:
    v1 = tmp_path / "v1"
    _as_v1(exported, v1)
    report = verify_export(v1)
    assert report.ok, report.problems
    again = score_directory(v1)
    assert again["format_version"] == 1
    assert again["paper_sessions"] == scored["paper_sessions"]  # every book value, both sessions
    # a v1 export cannot be compared with a fresh (v2) export of the store: refused, not passed
    with EventStore(built.path) as store:
        cross = verify_export(v1, store=store)
    assert not cross.ok
    assert any("cannot be compared with a fresh export" in p for p in cross.problems)


def test_an_unknown_format_version_is_refused(exported: Path, tmp_path: Path) -> None:
    root = _copy(exported, tmp_path)
    manifest = json.loads((root / MANIFEST).read_text("utf-8"))
    manifest["format_version"] = 3
    (root / MANIFEST).write_text(scorer_mod.canonical_json(manifest) + "\n", encoding="utf-8")
    report, export = check_export(root)
    assert export is None
    assert "format_version 3" in report.problems[0]
