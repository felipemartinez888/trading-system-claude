"""Preregistration schema, freeze and selection records (M4-T1 deliverable 1).

Owner policy fields stay PENDING: validation lists them, freeze refuses them. A frozen file that is
edited, or whose strategy code changes, no longer validates."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from tsclaude.research import preregistration as pr
from tsclaude.research.prereg import PreregistrationRefused, check_preregistration

REPO = Path(__file__).resolve().parents[3]
TEMPLATE = REPO / "preregistrations" / "TEMPLATE.yaml"
CLEAN = {"commit": "abc123", "dirty": False}


def _doc() -> dict[str, Any]:
    return pr.load(TEMPLATE)


def _write(tmp: Path, doc: dict[str, Any], name: str | None = None) -> Path:
    doc = copy.deepcopy(doc)
    doc["prereg_id"] = name or doc["prereg_id"]
    path = tmp / f"{doc['prereg_id']}.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _decided(doc: dict[str, Any]) -> dict[str, Any]:
    """The template with the three owner decisions filled in (for tests only)."""
    d = copy.deepcopy(doc)
    d["trial_budget"]["counting"] = "configurations"
    d["holdout_procedure"] = "two-stage (test value)"
    d["risk_compatibility"] = "live rules wired into backtests (test value)"
    return d


def test_the_template_is_valid_and_lists_the_open_owner_decisions() -> None:
    v = pr.validate_file(TEMPLATE)
    assert v.ok, v.problems
    assert v.kind == "search"
    assert not v.frozen
    assert [d.split(":")[0] for d in v.open_decisions] == [
        "trial_budget.counting", "holdout_procedure", "risk_compatibility",
    ]  # fmt: skip
    assert len(v.configurations) == 2


def test_the_template_id_is_refused_by_every_run() -> None:
    with pytest.raises(PreregistrationRefused, match="TEMPLATE"):
        check_preregistration(REPO, "TEMPLATE")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d.update(schema="v0"), "schema must be"),
        (lambda d: d.update(objective="total_return"), "objective must be"),
        (lambda d: d["data"].update(development=["2022-01-01", "2025-06-01"]), "D-05"),
        (lambda d: d["walk_forward"].update(n_min=100), "n_min must be"),
        (lambda d: d["walk_forward"].update(time_exit_required=False), "time_exit_required"),
        (lambda d: d["walk_forward"].update(min_oos_coverage=0), "min_oos_coverage"),
        (lambda d: d["walk_forward"].update(anchor="2022-02-15"), "month start"),
        (lambda d: d["trial_budget"].update(max_configurations=1), "above trial_budget"),
        (lambda d: d["trial_budget"].pop("counting"), "owner decision D-C"),
        (lambda d: d.pop("holdout_procedure"), "owner decision D-D"),
        (lambda d: d["rejection_criteria"].pop(0), "rejection_criteria must include"),
        (lambda d: d["strategy"].update(version="9.9.9"), "implemented version"),
        (lambda d: d["strategy"].update(strategy_id="h9"), "not an implemented strategy"),
        (lambda d: d["search_space"].append({"no_such": 1}), "search_space[2]"),
        (lambda d: d["search_space"].append({"tp_r": 1.0}), "repeats a configuration"),
        (lambda d: d["universe"].update(symbols=["BTCUSDT", "BTCUSDT"]), "twice"),
        (lambda d: d.update(max_loss_r_policy="lower"), "max_loss_r_policy"),
    ],
)
def test_invalid_preregistrations_are_reported(tmp_path: Path, change: Any, message: str) -> None:
    doc = _doc()
    change(doc)
    v = pr.validate_file(_write(tmp_path, doc, "H-T"))
    assert not v.ok
    assert any(message in p for p in v.problems), v.problems


def test_the_file_name_must_match_the_id(tmp_path: Path) -> None:
    path = tmp_path / "other.yaml"
    path.write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    assert any("does not match the file name" in p for p in pr.validate_file(path).problems)


def test_freeze_refuses_open_decisions_and_unidentified_code(tmp_path: Path) -> None:
    path = _write(tmp_path, _doc(), "H-T")
    with pytest.raises(pr.PreregistrationInvalid) as exc:
        pr.freeze(path, now_ms=0, code=CLEAN)
    assert sum("cannot freeze: " in p for p in exc.value.problems) == 3
    decided = _write(tmp_path, _decided(_doc()), "H-D")
    with pytest.raises(pr.PreregistrationInvalid, match="unidentified code"):
        pr.freeze(decided, now_ms=0, code={"commit": "abc", "dirty": True})
    assert "frozen" not in pr.load(decided)  # nothing was written


def test_a_frozen_file_detects_edits_and_implementation_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path, _decided(_doc()), "H-F")
    block = pr.freeze(path, now_ms=1_700_000_000_000, code=CLEAN)
    assert block["code_commit"] == "abc123"
    assert block["content_sha256"] == pr.content_sha256(pr.load(path))
    v = pr.validate_file(path)
    assert v.ok, v.problems
    assert v.frozen
    with pytest.raises(pr.PreregistrationInvalid, match="already frozen"):
        pr.freeze(path, now_ms=0, code=CLEAN)
    # an edit after the freeze
    edited = pr.load(path)
    edited["walk_forward"]["test_months"] = 6
    assert any("edited after it was frozen" in p for p in pr.validate_document(edited).problems)
    # a change of the strategy code since the freeze
    monkeypatch.setattr(pr, "implementation_sha256", lambda _sid: "0" * 64)
    assert any("implementation changed" in p for p in pr.validate_file(path).problems)


def test_the_implementation_hash_covers_strategy_features_and_adapter() -> None:
    names = {p.name for p in pr.implementation_files("bracket_reference")}
    assert {"bracket_reference.py", "base.py", "source.py", "core.py"} <= names
    assert pr.implementation_sha256("bracket_reference") == pr.implementation_sha256(
        "bracket_reference"
    )


def _frozen(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    path = _write(tmp_path, _decided(_doc()), "H-S")
    pr.freeze(path, now_ms=0, code=CLEAN)
    return path, pr.load(path)


def _report(doc: dict[str, Any], **over: Any) -> dict[str, Any]:
    cid, params = next(iter(pr.configurations_of(doc)[0])), doc["search_space"][0]
    from tsclaude.strategies import build_strategy

    report: dict[str, Any] = {
        "prereg_id": doc["prereg_id"],
        "plan": {"plan_id": "p" * 16},
        "checks": {"same_code": True},
        "oos": {"verdict": "OOS_MARGIN_CERTIFIED_DEVELOPMENT_ONLY"},
        "final_selection": {
            "rule": "full-development-train-v1",
            "status": "CANDIDATE_PENDING_GOVERNANCE",
            "selected": cid,
            "params": params,
            "strategy_hash": build_strategy("bracket_reference", params).params_hash,
        },
    }
    report.update(over)
    return report


def test_a_selection_record_links_a_frozen_search_to_one_strategy_hash(tmp_path: Path) -> None:
    search, doc = _frozen(tmp_path)
    rpath = tmp_path / "walkforward.json"
    rpath.write_text(json.dumps(_report(doc)), encoding="utf-8")
    record = pr.make_selection_record(search, rpath, prereg_id="H-S-SEL1")
    assert record["holdout_procedure"] == pr.PENDING  # D-D stays the owner's
    assert record["parent"]["content_sha256"] == pr.content_sha256(doc)
    out = _write(tmp_path, record, "H-S-SEL1")
    v = pr.validate_file(out)
    assert v.ok, v.problems
    assert v.kind == "selection"
    assert any("D-D" in d for d in v.open_decisions)
    # the record is tied to the parent's content: editing the parent breaks it
    pdoc = pr.load(search)
    pdoc["title"] = "changed"
    search.write_text(yaml.safe_dump(pdoc, sort_keys=False), encoding="utf-8")
    assert any("parent" in p for p in pr.validate_file(out).problems)


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"oos": {"verdict": "NO_EDGE_DEMONSTRATED"}}, "not positive"),
        ({"prereg_id": "OTHER"}, "not a run of this preregistration"),
        ({"checks": {"same_code": False}}, "validity checks"),
        ({"final_selection": {"status": "NOT_RUN"}}, "no final-selection candidate"),
    ],
)
def test_a_selection_record_is_refused_without_a_verified_candidate(
    tmp_path: Path, over: dict[str, Any], message: str
) -> None:
    search, doc = _frozen(tmp_path)
    rpath = tmp_path / "walkforward.json"
    rpath.write_text(json.dumps(_report(doc, **over)), encoding="utf-8")
    with pytest.raises(pr.PreregistrationInvalid, match=message):
        pr.make_selection_record(search, rpath, prereg_id="H-S-SEL1")


def test_walkforward_conformance_lists_every_deviation(tmp_path: Path) -> None:
    _, doc = _frozen(tmp_path)
    grid = {cid: {} for cid in pr.configurations_of(doc)[0]}
    base: dict[str, Any] = {
        "strategy_id": "bracket_reference", "grid": grid,
        "symbols": ["spot:BTCUSDT", "spot:ETHUSDT"], "initial_train_months": 12,
        "test_months": 3, "embargo_minutes": 1440, "n_min": 300, "min_oos_coverage": 1.0,
        "anchor": "2022-02-01T00:00:00Z", "dataset_ids": None, "final_selection": True,
    }  # fmt: skip
    assert pr.walkforward_conformance(doc, **base) == []
    bad = {**base, "test_months": 6, "n_min": 30, "symbols": ["spot:BTCUSDT"]}
    problems = pr.walkforward_conformance(doc, **bad)
    assert {p.split(" ")[0] for p in problems} == {"symbols", "test_months", "n_min"}
    unfrozen = _decided(_doc())
    assert any("not a frozen" in p for p in pr.walkforward_conformance(unfrozen, **base))


def test_the_cli_validates_and_refuses_to_freeze_the_template(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tsclaude.cli import main

    assert main(["prereg", "validate", str(TEMPLATE)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    copy_path = tmp_path / "TEMPLATE.yaml"
    shutil.copy(TEMPLATE, copy_path)
    assert main(["prereg", "freeze", str(copy_path)]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["refused"] == "freeze"
    assert any("D-C" in p for p in out["problems"])
    assert copy_path.read_text(encoding="utf-8") == TEMPLATE.read_text(encoding="utf-8")
