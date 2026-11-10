"""Research status (MVP): tiers and the NO_VALIDATED_STRATEGY state come only from evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tsclaude.contracts.events import canonical_json
from tsclaude.research.status import (
    NO_VALIDATED_STRATEGY,
    VALIDATED_STRATEGY_AVAILABLE,
    RunEvidence,
    StrategyTier,
    assess,
    load_status,
)


def _run(
    run_id: str,
    strategy: str = "h1_breakout",
    *,
    grade: str | None = "C",
    segment: str = "validation",
    verified: bool | None = True,
    prereg: str | None = "H1-v1",
) -> RunEvidence:
    return RunEvidence(
        run_id=run_id,
        strategy_id=strategy,
        segment=segment,
        evidence_grade=grade,
        metrics_verified=verified,
        prereg_id=prereg,
    )


def _tiers(status: dict[str, object]) -> dict[str, str]:
    return {s["strategy_id"]: s["tier"] for s in status["strategies"]}  # type: ignore[attr-defined,index]


def test_today_only_the_reference_exists_so_there_is_no_validated_strategy() -> None:
    status = assess(["bracket_reference"], [])
    assert status["state"] == NO_VALIDATED_STRATEGY
    assert status["validated"] == 0
    assert _tiers(status) == {"bracket_reference": "REFERENCE"}
    assert "only pipeline REFERENCE strategies exist (bracket_reference)" in status["reasons"]
    assert "no preregistration file exists (preregistrations/)" in status["reasons"]
    assert "no backtest run is recorded" in status["reasons"]
    assert "NO VALIDATED STRATEGY" in status["statement"]


def test_a_reference_stays_reference_even_with_a_verified_grade_a_run() -> None:
    """D-14: a REFERENCE label wins over any grade it carries."""
    run = _run("bt-1", "bracket_reference", grade="A", segment="holdout", prereg="X-v1")
    status = assess(["bracket_reference"], [run])
    assert _tiers(status) == {"bracket_reference": "REFERENCE"}
    assert status["state"] == NO_VALIDATED_STRATEGY


@pytest.mark.parametrize(
    ("run", "tier"),
    [
        (_run("bt-a", grade="A", segment="holdout"), StrategyTier.VALIDATED),
        # grade A means validation AND holdout: an A outside the holdout is not validation
        (_run("bt-a-val", grade="A", segment="validation"), StrategyTier.CANDIDATE),
        (_run("bt-a-dev", grade="A", segment="development"), StrategyTier.CANDIDATE),
        (_run("bt-d", grade="D"), StrategyTier.REJECTED),
        (_run("bt-d-holdout", grade="D", segment="holdout"), StrategyTier.REJECTED),
        (_run("bt-b", grade="B"), StrategyTier.CANDIDATE),
        (_run("bt-c", grade="C"), StrategyTier.CANDIDATE),
        # a D on the development segment is not the preregistered validation verdict
        (_run("bt-d-dev", grade="D", segment="development"), StrategyTier.CANDIDATE),
        # untrusted runs never count, whatever their grade
        (_run("bt-unverified", grade="A", verified=False), StrategyTier.UNREGISTERED),
        (_run("bt-unchecked", grade="A", verified=None), StrategyTier.UNREGISTERED),
        (_run("bt-no-prereg", grade="A", prereg=None), StrategyTier.UNREGISTERED),
    ],
)
def test_tiers_follow_the_protocol_grades(run: RunEvidence, tier: StrategyTier) -> None:
    status = assess(["h1_breakout"], [run])
    assert _tiers(status) == {"h1_breakout": str(tier)}
    expected = (
        VALIDATED_STRATEGY_AVAILABLE if tier is StrategyTier.VALIDATED else NO_VALIDATED_STRATEGY
    )
    assert status["state"] == expected


def test_only_grade_a_is_validated_and_a_beats_an_earlier_rejection() -> None:
    runs = [_run("bt-1", grade="D"), _run("bt-2", grade="A", segment="holdout")]
    status = assess(["h1_breakout"], runs)
    assert _tiers(status) == {"h1_breakout": "VALIDATED"}
    assert status["validated"] == 1
    assert status["runs_trusted"] == 2


def test_ignored_runs_are_explained_and_counted() -> None:
    runs = [_run("bt-1", grade="B"), _run("bt-2", grade="A", verified=False)]
    status = assess(["h1_breakout"], runs, preregistration_files=["H1-v1.yaml"])
    (row,) = status["strategies"]
    assert row["tier"] == "CANDIDATE"
    assert row["runs"] == 2
    assert row["trusted_runs"] == 1
    assert any("1 run(s) ignored" in r for r in row["reasons"])
    assert "1 candidate(s) not validated" in status["reasons"]
    assert status["preregistration_files"] == ["H1-v1.yaml"]


def test_a_run_of_an_unregistered_strategy_is_still_listed() -> None:
    status = assess(["bracket_reference"], [_run("bt-1", "old_strategy", grade="C")])
    assert _tiers(status) == {"bracket_reference": "REFERENCE", "old_strategy": "CANDIDATE"}
    row = next(s for s in status["strategies"] if s["strategy_id"] == "old_strategy")
    assert row["registered"] is False


def _write_run(runs: Path, run_id: str, *, grade: str, prereg: str | None, tamper: bool) -> None:
    d = runs / run_id
    d.mkdir(parents=True)
    text = canonical_json({"evidence_grade": grade, "n_trials": 1, "trades": {"n": 3}})
    (d / "metrics.json").write_text(text + "\n", encoding="utf-8")
    manifest = {
        "run_id": run_id,
        "created_at": "2026-11-09T00:00:00.000Z",
        "strategy": {"strategy_id": "h1_breakout", "version": "1.0.0", "params_hash": "x"},
        "segment": "holdout",
        "prereg_id": prereg,
        "outputs": {"metrics_sha256": hashlib.sha256(text.encode()).hexdigest()},
    }
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if tamper:  # metrics edited after the run: the hash no longer matches
        (d / "metrics.json").write_text(text.replace('"A"', '"B"') + "\n", encoding="utf-8")


def test_load_status_reads_runs_and_preregistrations_from_disk(tmp_path: Path) -> None:
    runs, repo = tmp_path / "data" / "runs", tmp_path / "repo"
    (repo / "preregistrations").mkdir(parents=True)
    (repo / "preregistrations" / "TEMPLATE.yaml").write_text("x: 1\n", encoding="utf-8")
    assert load_status(runs, repo)["state"] == NO_VALIDATED_STRATEGY  # nothing on disk yet
    _write_run(runs, "bt-tampered", grade="A", prereg="H1-v1", tamper=True)
    status = load_status(runs, repo)
    assert status["state"] == NO_VALIDATED_STRATEGY  # a tampered grade A is not trusted
    assert status["preregistration_files"] == []  # the template is not a registration
    (repo / "preregistrations" / "H1-v1.yaml").write_text("x: 1\n", encoding="utf-8")
    _write_run(runs, "bt-ok", grade="A", prereg="H1-v1", tamper=False)
    status = load_status(runs, repo)
    assert status["state"] == VALIDATED_STRATEGY_AVAILABLE
    assert _tiers(status)["h1_breakout"] == "VALIDATED"
    assert _tiers(status)["bracket_reference"] == "REFERENCE"
    assert status["runs_recorded"] == 2
    assert status["runs_trusted"] == 1


def test_walk_forward_reports_are_listed_as_recorded_and_never_as_validation(
    tmp_path: Path,
) -> None:
    import json

    from tsclaude.research.status import walk_forward_summaries

    root = tmp_path / "walkforward"
    assert walk_forward_summaries(root) == []
    good = root / "aaaa"
    good.mkdir(parents=True)
    (good / "walkforward.json").write_text(json.dumps({
        "plan": {"plan_id": "aaaa"}, "strategy_id": "bracket_reference",
        "oos": {"verdict": "INVALID_OOS_EVIDENCE", "n_trials": 21, "failed_test_folds": [2],
                "oos_coverage": {"share": 0.25}},
        "checks": {"same_code": True}, "live_compatibility": {"any_deployable": False},
    }), encoding="utf-8")  # fmt: skip
    bad = root / "bbbb"
    bad.mkdir()
    (bad / "walkforward.json").write_text("{not json", encoding="utf-8")
    rows = {r["report"]: r for r in walk_forward_summaries(root)}
    assert rows["aaaa"]["verdict"] == "INVALID_OOS_EVIDENCE"
    assert rows["aaaa"]["reference"] is True
    assert rows["aaaa"]["failed_test_folds"] == [2]
    assert rows["aaaa"]["live_deployable"] is False
    assert rows["aaaa"]["not_validation"] is True
    assert rows["aaaa"]["risk_policy"] == "not_recorded"  # a report from before P1-6
    assert "unreadable" in rows["bbbb"]
    live = root / "cccc"
    live.mkdir()
    (live / "walkforward.json").write_text(json.dumps({
        "plan": {"plan_id": "cccc"}, "strategy_id": "bracket_reference", "oos": {},
        "risk_policy": {"mode": "live", "risk_config_hash": "h"},
    }), encoding="utf-8")  # fmt: skip
    assert {r["report"]: r for r in walk_forward_summaries(root)}["cccc"]["risk_policy"] == "live"
    assert rows["aaaa"]["min_detectable_margin"] is None  # a report from before power-plan
    for name, reading, expected in (
        ("dddd", {"min_detectable_margin": 0.045, "n_used": 900}, 0.045),
        ("eeee", {"error": "OverflowError: x"}, None),
        ("ffff", None, None),
    ):
        d = root / name
        d.mkdir()
        (d / "walkforward.json").write_text(json.dumps({
            "plan": {"plan_id": name}, "strategy_id": "bracket_reference", "oos": {},
            "power_reading": reading,
        }), encoding="utf-8")  # fmt: skip
        got = {r["report"]: r for r in walk_forward_summaries(root)}[name]
        assert got["min_detectable_margin"] == expected
