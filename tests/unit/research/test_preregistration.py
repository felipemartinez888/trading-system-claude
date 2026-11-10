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
CFG = "c" * 64  # a backtest config hash (any sha256 hex digest)


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
    d["holdout_procedure"] = "two-stage-selection-record"
    d["risk_compatibility"] = "live-floor-kept-geometries-dropped"
    d["walk_forward"]["anchor"] = "2022-02-01"  # a frozen file pins its folds (re-review N2)
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
        (lambda d: d["search_space"].append({"tp_r": 1.0, "time_exit_bars": 48}), "repeats a"),
        (lambda d: d["universe"].update(symbols=["BTCUSDT", "BTCUSDT"]), "twice"),
        (lambda d: d.update(max_loss_r_policy="lower"), "max_loss_r_policy"),
        # the embargo must cover one maximum holding period (overnight H1 causality review)
        (lambda d: d["search_space"].append({"tp_r": 0.8}), "holding period is unbounded"),
        (lambda d: d["walk_forward"].update(embargo_minutes=700), "shorter than its maximum"),
        (lambda d: d["walk_forward"].update(end="2025-06-01"), "walk_forward.end"),
        # a typo or placeholder is never taken as an owner decision (review B2)
        (lambda d: d.update(holdout_procedure="TBD"), "is not a decision"),
        (lambda d: d.update(holdout_procedure=""), "is not a decision"),
        (lambda d: d.update(risk_compatibility="pending"), "is not a decision"),
        (lambda d: d["trial_budget"].update(counting=False), "is not a decision"),
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
        pr.freeze(path, now_ms=0, code=CLEAN, backtest_config_hash=CFG)
    assert sum("cannot freeze: " in p for p in exc.value.problems) == 3
    decided = _write(tmp_path, _decided(_doc()), "H-D")
    with pytest.raises(pr.PreregistrationInvalid, match="unidentified code"):
        pr.freeze(
            decided, now_ms=0, code={"commit": "abc", "dirty": True}, backtest_config_hash=CFG
        )
    assert "frozen" not in pr.load(decided)  # nothing was written


def test_a_frozen_file_detects_edits_and_implementation_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path, _decided(_doc()), "H-F")
    block = pr.freeze(path, now_ms=1_700_000_000_000, code=CLEAN, backtest_config_hash=CFG)
    assert block["code_commit"] == "abc123"
    assert block["content_sha256"] == pr.content_sha256(pr.load(path))
    v = pr.validate_file(path)
    assert v.ok, v.problems
    assert v.frozen
    with pytest.raises(pr.PreregistrationInvalid, match="already frozen"):
        pr.freeze(path, now_ms=0, code=CLEAN, backtest_config_hash=CFG)
    # an edit after the freeze
    edited = pr.load(path)
    edited["walk_forward"]["test_months"] = 6
    assert any("edited after it was frozen" in p for p in pr.validate_document(edited).problems)
    # the frozen block itself is sealed (review M4)
    tampered = pr.load(path)
    tampered["frozen"]["code_commit"] = "other"
    assert any("frozen block was edited" in p for p in pr.validate_document(tampered).problems)
    # an engine change is reported, not enforced
    monkeypatch.setattr(pr, "engine_sha256", lambda: "1" * 64)
    assert pr.validate_file(path).ok
    assert any("engine changed" in n for n in pr.validate_file(path).notes)
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
    pr.freeze(path, now_ms=0, code=CLEAN, backtest_config_hash=CFG)
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
    # review B1: the holdout guard reads a top-level strategy_hash; a record never has one
    assert "strategy_hash" not in record
    assert record["selected"]["strategy_hash"]
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


def test_a_hand_written_selection_record_with_a_top_level_hash_is_invalid(tmp_path: Path) -> None:
    search, doc = _frozen(tmp_path)
    rpath = tmp_path / "walkforward.json"
    rpath.write_text(json.dumps(_report(doc)), encoding="utf-8")
    record = pr.make_selection_record(search, rpath, prereg_id="H-S-SEL2")
    record["strategy_hash"] = record["selected"]["strategy_hash"]
    v = pr.validate_file(_write(tmp_path, record, "H-S-SEL2"))
    assert any("must not have a top-level strategy_hash" in p for p in v.problems)


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"oos": {"verdict": "NO_EDGE_DEMONSTRATED"}}, "not positive"),
        ({"prereg_id": "OTHER"}, "not a run of this preregistration"),
        ({"checks": {"same_code": False}}, "validity checks"),
        ({"checks": {}}, "validity checks"),
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
        "config_hash": CFG,
    }  # fmt: skip
    assert pr.walkforward_conformance(doc, **base) == []
    from tsclaude.research.walkforward import plan_folds

    def ms(y: int, m: int) -> int:
        from datetime import UTC, datetime

        return int(datetime(y, m, 1, tzinfo=UTC).timestamp() * 1000)

    planned = [f.to_dict() for f in plan_folds(ms(2022, 2), ms(2025, 1), initial_train_months=12,
                                              test_months=3, embargo_ms=1440 * 60_000)]  # fmt: skip
    assert pr.walkforward_conformance(doc, **base, folds=planned) == []
    short = [f.to_dict() for f in plan_folds(ms(2022, 2), ms(2024, 1), initial_train_months=12,
                                            test_months=3, embargo_ms=1440 * 60_000)]  # fmt: skip
    assert any("folds differ" in p for p in pr.walkforward_conformance(doc, **base, folds=short))
    for other in ("d" * 64, None):  # another config, or none reported: refused (re-review N2)
        assert any("config hash" in p for p in pr.walkforward_conformance(
            doc, **{**base, "config_hash": other}))  # fmt: skip
    bad = {**base, "test_months": 6, "n_min": 30, "symbols": ["spot:BTCUSDT"]}
    problems = pr.walkforward_conformance(doc, **bad)
    assert {p.split(" ")[0] for p in problems} == {"symbols", "test_months", "n_min"}
    unfrozen = _decided(_doc())
    assert any("not a frozen" in p for p in pr.walkforward_conformance(unfrozen, **base))


def test_a_frozen_file_pins_its_folds_and_backtest_config(tmp_path: Path) -> None:
    """Re-review N2: with ``anchor: auto`` any first fold conformed and the config hash was
    compared only if the file declared one. A frozen file now fixes both."""
    auto = _decided(_doc())
    auto["walk_forward"]["anchor"] = "auto"
    path = _write(tmp_path, auto, "H-AUTO")
    with pytest.raises(pr.PreregistrationInvalid, match="anchor 'auto'"):
        pr.freeze(path, now_ms=0, code=CLEAN, backtest_config_hash=CFG)
    with pytest.raises(pr.PreregistrationInvalid, match="backtest config hash"):
        pr.freeze(_write(tmp_path, _decided(_doc()), "H-NOCFG"), now_ms=0, code=CLEAN,
                  backtest_config_hash="")  # fmt: skip
    declared = _decided(_doc())
    declared["backtest_config_hash"] = "d" * 64
    with pytest.raises(pr.PreregistrationInvalid, match="the current config is"):
        pr.freeze(_write(tmp_path, declared, "H-DECL"), now_ms=0, code=CLEAN,
                  backtest_config_hash=CFG)  # fmt: skip

    path, doc = _frozen(tmp_path)
    assert doc["frozen"]["backtest_config_hash"] == CFG
    grid = {cid: {} for cid in pr.configurations_of(doc)[0]}
    base: dict[str, Any] = {
        "strategy_id": "bracket_reference", "grid": grid,
        "symbols": ["spot:BTCUSDT", "spot:ETHUSDT"], "initial_train_months": 12,
        "test_months": 3, "embargo_minutes": 1440, "n_min": 300, "min_oos_coverage": 1.0,
        "dataset_ids": None, "final_selection": True, "config_hash": CFG,
    }  # fmt: skip
    from datetime import UTC, datetime

    from tsclaude.research.walkforward import plan_folds

    def folds_from(year: int, month: int) -> list[dict[str, Any]]:
        start = int(datetime(year, month, 1, tzinfo=UTC).timestamp() * 1000)
        end = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1000)
        planned = plan_folds(
            start, end, initial_train_months=12, test_months=3, embargo_ms=1440 * 60_000
        )
        return [f.to_dict() for f in planned]

    ok = pr.walkforward_conformance(
        doc, **base, anchor="2022-02-01T00:00:00Z", folds=folds_from(2022, 2)
    )
    assert ok == []
    late = pr.walkforward_conformance(
        doc, **base, anchor="2023-06-01T00:00:00Z", folds=folds_from(2023, 6)
    )
    assert any(p.startswith("anchor ") for p in late), late
    # a frozen block without the pin (or a hand-edited anchor) no longer validates
    stripped = copy.deepcopy(doc)
    del stripped["frozen"]["backtest_config_hash"]
    assert any("pin the backtest config" in p for p in pr.validate_document(stripped).problems)
    unpinned = copy.deepcopy(doc)
    unpinned["walk_forward"]["anchor"] = "auto"
    assert any("must fix walk_forward.anchor" in p for p in pr.validate_document(unpinned).problems)


def test_unknown_keys_are_problems_not_ignored(tmp_path: Path) -> None:
    """Re-review N5: a typo must not silently disable the field it meant to set."""
    doc = _doc()
    doc["backtest_config_hsh"] = "c" * 64
    v = pr.validate_document(doc)
    assert any("unknown top-level keys ['backtest_config_hsh']" in p for p in v.problems)
    doc = _doc()
    doc["walk_forward"]["embargo_minute"] = 1440
    assert any("unknown walk_forward keys" in p for p in pr.validate_document(doc).problems)
    doc = _doc()
    doc["backtest_config_hash"] = "not-a-digest"
    assert any("sha256 hex digest" in p for p in pr.validate_document(doc).problems)


def test_the_cli_freeze_pins_the_config_file_hash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tsclaude.cli import main
    from tsclaude.research import runner
    from tsclaude.research.runner import BacktestConfig

    monkeypatch.setattr(runner, "git_version", lambda: CLEAN)
    path = _write(tmp_path, _decided(_doc()), "H-CLI")
    assert main(["prereg", "freeze", str(path), "--config", str(tmp_path / "none.yaml")]) == 2
    assert "does not exist" in capsys.readouterr().out
    cfg = REPO / "configs" / "backtest.yaml"
    assert main(["prereg", "freeze", str(path), "--config", str(cfg)]) == 0
    capsys.readouterr()
    assert pr.load(path)["frozen"]["backtest_config_hash"] == BacktestConfig.load(cfg).config_hash


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


# ---------------------------------------------------------------- draft strategies
H1_DRAFT = REPO / "docs" / "research" / "H1-v2.preregistration.DRAFT.yaml"


def _draft_doc() -> dict[str, Any]:
    d = _decided(_doc())
    d["strategy"] = {"strategy_id": "h1_compression_breakout", "version": "0.1.0-draft"}
    d["search_space"] = [{"tp_r": 1.0}, {"tp_r": 0.5}]
    return d


def test_a_draft_strategy_validates_with_an_open_item_and_cannot_be_frozen(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, _draft_doc(), "H-DRAFT")
    v = pr.validate_file(path)
    assert v.ok, v.problems
    assert v.open_decisions == [pr.DRAFT_OPEN_ITEM]  # an open item, not a problem
    assert len(v.configurations) == 2
    before = path.read_text(encoding="utf-8")
    with pytest.raises(pr.PreregistrationInvalid) as exc:
        pr.freeze(path, now_ms=0, code=CLEAN, backtest_config_hash=CFG)
    assert f"cannot freeze: {pr.DRAFT_OPEN_ITEM}" in exc.value.problems
    assert path.read_text(encoding="utf-8") == before  # nothing was written
    wrong = _draft_doc()
    wrong["strategy"]["version"] = "1.0.0"
    assert any("implemented version" in p for p in pr.validate_document(wrong).problems)
    bad = _draft_doc()
    bad["search_space"].append({"time_exit_bars": None})
    assert any("search_space[2]" in p for p in pr.validate_document(bad).problems)
    with pytest.raises(ValueError, match="unknown strategy"):
        pr.implementation_files("h1_compression_breakout")  # not freezable code identity


def test_the_h1_v2_draft_validates_as_a_copy_named_after_its_id(tmp_path: Path) -> None:
    # at its draft path the stem differs from the id by design
    assert any("does not match the file name" in p for p in pr.validate_file(H1_DRAFT).problems)
    copy_path = tmp_path / "H1-v2.yaml"
    shutil.copy(H1_DRAFT, copy_path)
    v = pr.validate_file(copy_path)
    assert v.ok, v.problems
    assert v.kind == "search"
    assert not v.frozen
    assert len(v.configurations) == 24
    assert [d.split(":")[0] for d in v.open_decisions] == [
        "trial_budget.counting", "holdout_procedure", "risk_compatibility", pr.DRAFT_OPEN_ITEM,
    ]  # fmt: skip
    doc = pr.load(copy_path)
    assert "SOLUSDT" not in doc["universe"]["symbols"]
    assert doc["walk_forward"]["n_min"] == 300
    assert doc["walk_forward"]["time_exit_required"] is True
    assert all(isinstance(c["time_exit_bars"], int) for c in doc["search_space"])
    # the embargo covers one maximum scheduled holding period of every configuration
    from tsclaude.strategies import build_draft_strategy

    longest = max(
        build_draft_strategy("h1_compression_breakout", c).max_holding_ms()  # type: ignore[attr-defined]
        for c in doc["search_space"]
    )
    assert doc["walk_forward"]["embargo_minutes"] * 60_000 >= longest
    with pytest.raises(pr.PreregistrationInvalid, match="cannot freeze"):
        pr.freeze(copy_path, now_ms=0, code=CLEAN, backtest_config_hash=CFG)
