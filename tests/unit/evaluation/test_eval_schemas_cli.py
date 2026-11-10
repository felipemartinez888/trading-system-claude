"""The scorer's built-in JSON Schema validator, the schema files themselves, and the
``tsclaude evaluation export | verify | score`` commands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest
from eval_helpers import build_store

from tsclaude.evaluation.cli import register_evaluation
from tsclaude.evaluation.scorer import FILES, SchemaSet

SCHEMAS = SchemaSet()


def _envelope(**over: Any) -> dict[str, Any]:
    line: dict[str, Any] = {
        "format": "tsc-eval",
        "format_version": 1,
        "record_type": "sim_order_filled",
        "line_no": 1,
        "prev_line_hash": "0" * 64,
        "line_hash": "a" * 64,
        "provenance": "SIMULATED",
        "paper_session_id": "paper",
        "batch_seq": 1,
        "event": {
            "seq": 2,
            "ts_ms": 0,
            "type": "sim_order_filled",
            "aggregate_type": "sim",
            "aggregate_id": "baseline",
            "payload": {
                "book": "baseline",
                "order_id": "o",
                "role": "entry",
                "bar_open_ms": 0,
                "at_open": True,
                "direction": "buy",
                "price": "1.5",
                "raw_price": "1.5",
                "qty": "2",
                "fee": "0.1",
                "notional": "3",
                "liquidity": "taker",
                "sim_seq": 1,
            },
            "schema_version": 1,
            "prev_hash": "0" * 64,
            "hash": "b" * 64,
        },
        "chain_head": {"seq": 9, "hash": "c" * 64},
    }
    line.update(over)
    return line


def test_valid_line_passes() -> None:
    assert SCHEMAS.validate(_envelope(), "fills.schema.json") == []


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        (lambda x: x.pop("line_hash"), "missing 'line_hash'"),
        (lambda x: x.update(extra=1), "unexpected property 'extra'"),
        (lambda x: x.update(line_no=0), "< 1"),
        (lambda x: x.update(line_no=True), "expected integer"),
        (lambda x: x.update(provenance="DEMO"), "not in"),
        (lambda x: x.update(prev_line_hash="xyz"), "does not match"),
        (lambda x: x.update(paper_session_id=None), "expected string"),
        (lambda x: x["event"]["payload"].update(price="1e5"), "does not match"),
        (lambda x: x["event"]["payload"].pop("qty"), "missing 'qty'"),
        (lambda x: x["event"]["payload"].update(direction="up"), "not in"),
        (lambda x: x.update(record_type="sim_position_opened"), "not in"),
        (lambda x: x.update(format_version=2), "expected 1"),
    ],
)
def test_invalid_lines_fail(change: Any, fragment: str) -> None:
    line = _envelope()
    change(line)
    errors = SCHEMAS.validate(line, "fills.schema.json")
    assert errors
    assert any(fragment in e for e in errors), errors


def test_if_then_selects_the_payload_schema() -> None:
    line = _envelope(record_type="sim_position_opened")
    line["event"]["type"] = "sim_position_opened"
    errors = SCHEMAS.validate(line, "positions.schema.json")
    assert any("missing 'trade_id'" in e for e in errors)  # the opened-position payload applies


def test_one_schema_per_file_and_a_manifest_schema() -> None:
    names = set(SCHEMAS.schemas)
    assert {n.replace(".jsonl", ".schema.json") for n in FILES} <= names
    assert {"envelope.schema.json", "manifest.schema.json"} <= names
    for name, schema in SCHEMAS.schemas.items():
        assert schema["$id"].endswith("/eval/v1/" + name)


def test_schemas_agree_with_a_reference_validator(tmp_path: Path) -> None:
    """Cross-check with the `jsonschema` package when it is installed (not a dependency)."""
    jsonschema = pytest.importorskip("jsonschema")
    referencing = pytest.importorskip("referencing")
    resources = [
        (s["$id"], referencing.Resource.from_contents(s)) for s in SCHEMAS.schemas.values()
    ]
    registry = referencing.Registry().with_resources(resources)
    fills = SCHEMAS.schemas["fills.schema.json"]
    validator = jsonschema.Draft202012Validator(fills, registry=registry)
    good = _envelope()
    assert list(validator.iter_errors(good)) == []
    bad = _envelope(provenance="DEMO")
    assert list(validator.iter_errors(bad)) != []
    for schema in SCHEMAS.schemas.values():
        jsonschema.Draft202012Validator.check_schema(schema)
    # and every line of a real export agrees in both validators
    built = build_store(tmp_path / "state.db")
    from tsclaude.evaluation.export import build_lines
    from tsclaude.store.eventstore import EventStore

    with EventStore(built.path) as store:
        files, _, _, _ = build_lines(store)
    for name, lines in files.items():
        schema = SCHEMAS.schemas[name.replace(".jsonl", ".schema.json")]
        v = jsonschema.Draft202012Validator(schema, registry=registry)
        for line in lines:
            assert list(v.iter_errors(line)) == []


# ---------------------------------------------------------------------------- CLI
def _run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="tsclaude")
    sub = parser.add_subparsers(dest="command", required=True)
    register_evaluation(sub)
    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


def test_cli_export_verify_score(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    built = build_store(tmp_path / "state.db")
    db = str(built.path)
    out = tmp_path / "export"
    assert _run(["evaluation", "export", "--db", db, "--out", str(out)]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["ok"] is True
    manifest = json.loads((out / "manifest.json").read_bytes())
    assert isinstance(manifest["exported_at_ms"], int)  # the wall clock, manifest only
    assert _run(["evaluation", "verify", "--db", db, "--export", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["store_checked"] is True
    assert _run(["evaluation", "verify", "--no-store", "--export", str(out)]) == 0
    capsys.readouterr()
    assert _run(["evaluation", "score", "--export", str(out)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert set(result["score"]["paper_sessions"]) == {"paper", "paper-2"}
    # refusals
    assert _run(["evaluation", "export", "--db", db, "--out", str(out)]) == 2  # not empty
    assert "not an empty directory" in capsys.readouterr().out
    missing = str(tmp_path / "nope.db")
    assert _run(["evaluation", "export", "--db", missing, "--out", str(tmp_path / "x")]) == 2
    assert not Path(missing).exists()  # no store is created by a read-only command
    capsys.readouterr()
    (out / "fills.jsonl").write_bytes(b"")
    assert _run(["evaluation", "verify", "--db", db, "--export", str(out)]) == 2
    assert _run(["evaluation", "score", "--export", str(out)]) == 2
