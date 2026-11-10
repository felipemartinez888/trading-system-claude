"""M5-3 sessions through the M6 evaluation export (mock client only, no network).

An ``agent_gated`` session journals three event types inside its paper batches:
``agent_review_requested`` (with the baseline submission), ``agent_review_completed`` (its own
batch) and ``agent_gated_admission``. The export has to carry them, keep every batch complete, and
let the independent scorer reproduce all four books, ``agent_gated`` included, from the export
alone. This needs export format v2 (``agents.jsonl``). Before it, every such export was refused
("event 'agent_review_requested' … inside a paper batch"). A mode-off session exports as before,
plus an empty ``agents.jsonl``.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import test_paper_session as ps
from paper_agent_helpers import approve, config, downgrade, gated_config, mock_client, run, veto
from paper_agent_helpers import worker as make_worker

from tsclaude.contracts.events import EventType
from tsclaude.evaluation.export import export_store
from tsclaude.evaluation.scorer import FILE_OF, FILES, score_directory
from tsclaude.evaluation.verify import verify_export
from tsclaude.sim.paper import AGENT_GATED, BASELINE, BENCHMARK, SHADOW, PaperTradingService
from tsclaude.store.eventstore import EventStore

AGENT_TYPES = (
    str(EventType.AGENT_REVIEW_REQUESTED),
    str(EventType.AGENT_REVIEW_COMPLETED),
    str(EventType.AGENT_GATED_ADMISSION),
)
MISSING = frozenset({(13, "ETHUSDT")})
EXPORTED_AT = 1_800_000_000_000


def _bars() -> Any:
    from paper_agent_helpers import two_symbol_bars

    return two_symbol_bars(ps.N, ps.BTC_ROWS, ps.ETH_ROWS)


def _session(
    data: Path, cfg: Any, reviews: list[Any] | None
) -> tuple[dict[str, str], Counter[str]]:
    """Run the scenario into an EventStore; return the live ledger hashes and event type counts."""
    bars = _bars()
    w = make_worker(mock_client(reviews), bars) if reviews is not None else None
    with EventStore(data / "state.db") as store:
        svc = PaperTradingService.open(
            cfg, store, session_id="paper-a", strategy=ps.scenario(), reviewer=w
        )
        run(svc, bars, list(range(ps.N)), wait=w, missing=MISSING)
        if w is not None:
            w.close()
        types = Counter(str(e.type) for e in store.read(after_seq=0, limit=10_000))
        return svc.ledger_hashes(), types


def _export(data: Path, out: Path) -> dict[str, Any]:
    with EventStore(data / "state.db") as store:
        result = export_store(store, out, exported_at_ms=EXPORTED_AT)
        report = verify_export(out, store=store)
    assert report.ok, report.problems
    return {"result": result, "manifest": json.loads((out / "manifest.json").read_text("utf-8"))}


def test_every_agent_event_type_has_an_export_file() -> None:
    for kind in AGENT_TYPES:
        assert kind in FILE_OF, kind
    assert FILES["agents.jsonl"] == AGENT_TYPES


@pytest.mark.parametrize(
    "reviews",
    [approve() * 3, downgrade() * 3, veto() * 3, approve() + veto() + downgrade()],
    ids=["approve", "downgrade", "veto", "mixed"],
)
def test_an_agent_gated_session_exports_verifies_and_scores(
    tmp_path: Path, reviews: list[Any]
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    live, types = _session(data, gated_config(), reviews)
    assert set(live) == {BASELINE, SHADOW, BENCHMARK, AGENT_GATED}
    assert types[AGENT_TYPES[0]] > 0  # the scenario does request reviews
    out = tmp_path / "export"
    exported = _export(data, out)

    lines = [json.loads(x) for x in (out / "agents.jsonl").read_text("utf-8").splitlines()]
    got = Counter(ln["record_type"] for ln in lines)
    assert got == Counter({k: types[k] for k in AGENT_TYPES if types[k]})  # every one, once
    assert all(ln["paper_session_id"] == "paper-a" for ln in lines)
    assert "agent_review_requested" not in exported["manifest"].get("not_exported", {})

    scored = score_directory(out)["paper_sessions"]["paper-a"]
    assert {n: b["ledger_hash"] for n, b in scored["books"].items()} == live


def test_a_mode_off_session_exports_without_an_agent_line(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    live, types = _session(data, config(), None)
    assert not any(types[k] for k in AGENT_TYPES)
    out = tmp_path / "export"
    _export(data, out)
    assert (out / "agents.jsonl").read_text("utf-8") == ""
    scored = score_directory(out)["paper_sessions"]["paper-a"]
    assert {n: b["ledger_hash"] for n, b in scored["books"].items()} == live


def test_agent_lines_agree_with_a_reference_validator(tmp_path: Path) -> None:
    """Cross-check the v2 agent and step schemas with `jsonschema` when it is installed."""
    jsonschema = pytest.importorskip("jsonschema")
    referencing = pytest.importorskip("referencing")
    from tsclaude.evaluation.scorer import SchemaSet

    schemas = SchemaSet()
    registry = referencing.Registry().with_resources(
        [(s["$id"], referencing.Resource.from_contents(s)) for s in schemas.schemas.values()]
    )
    data = tmp_path / "data"
    data.mkdir()
    _session(data, gated_config(), approve() + veto() + downgrade())
    out = tmp_path / "export"
    _export(data, out)
    for name in ("agents.jsonl", "steps.jsonl"):
        v = jsonschema.Draft202012Validator(
            schemas.schemas[name.replace(".jsonl", ".schema.json")], registry=registry
        )
        lines = (out / name).read_text("utf-8").splitlines()
        assert lines
        for raw in lines:
            assert list(v.iter_errors(json.loads(raw))) == [], raw[:200]


def test_the_agent_schemas_reject_what_advice_must_never_carry(tmp_path: Path) -> None:
    """The v2 schemas refuse an outcome that claims to affect the baseline, an admission to a
    book other than agent_gated, a non-SIMULATED agent line and a review step without its outcome.
    """
    from tsclaude.evaluation.scorer import SchemaSet

    schemas = SchemaSet()
    data = tmp_path / "data"
    data.mkdir()
    _session(data, gated_config(), approve() * 3)
    out = tmp_path / "export"
    _export(data, out)
    agents = [json.loads(x) for x in (out / "agents.jsonl").read_text("utf-8").splitlines()]
    by_type = {ln["record_type"]: ln for ln in agents}
    steps = [json.loads(x) for x in (out / "steps.jsonl").read_text("utf-8").splitlines()]
    review_step = next(s for s in steps if s["event"]["payload"]["kind"] == "agent_review")
    for line in [*by_type.values(), review_step]:
        name = "steps.schema.json" if line is review_step else "agents.schema.json"
        assert schemas.validate(line, name) == []

    def broken(line: dict[str, Any], change: Any) -> dict[str, Any]:
        copy = json.loads(json.dumps(line))
        change(copy)
        return copy

    completed = by_type["agent_review_completed"]
    admission = by_type["agent_gated_admission"]
    cases = [
        (completed, lambda x: x["event"]["payload"].update(baseline_affected=True)),
        (completed, lambda x: x["event"]["payload"].pop("risk_passed")),
        (completed, lambda x: x.update(provenance="LIVE")),
        (admission, lambda x: x["event"]["payload"].update(book="baseline")),
        (admission, lambda x: x["event"].update(aggregate_type="sim")),
    ]
    for line, change in cases:
        assert schemas.validate(broken(line, change), "agents.schema.json") != []
    no_outcome = broken(review_step, lambda x: x["event"]["payload"].pop("review"))
    assert schemas.validate(no_outcome, "steps.schema.json") != []
