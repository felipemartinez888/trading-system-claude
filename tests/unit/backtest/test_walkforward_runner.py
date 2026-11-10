"""Walk-forward through the real backtest runner on a synthetic lake (no network, no pyarrow).

Every evaluation is a normal recorded run: manifests exist, train runs end before the embargoed
test window, only the selected configuration runs out of sample, and the stitched verdict counts
every configuration of the strategy family as a trial. Audit WF-1 and WF-2: the anchor leaves the
warm-up inside the data (every run is integrity-clean), an anchor without warm-up is refused before
anything is recorded, and runs the runner grades X are never used.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from bt_helpers import FAST_PARAMS, TEST_CONFIG, make_env, ms, synthetic_rows, write_lake

from tsclaude.contracts.market import Market
from tsclaude.contracts.timeutil import from_iso
from tsclaude.research.runner import RunEnv, load_run
from tsclaude.research.walkforward import (
    INSUFFICIENT_OOS_EVIDENCE,
    INVALID_OOS_EVIDENCE,
    NO_EDGE_DEMONSTRATED,
    OOS_MARGIN_CERTIFIED_DEVELOPMENT_ONLY,
    RunnerEvaluator,
    WalkForwardError,
    config_id_of,
    plan_folds,
    run_walk_forward,
)

DAY = 86_400_000
MIN = 60_000
BTC = [(Market.SPOT, "BTCUSDT")]
#: the lake starts a month before the anchor, so every run's warm-up (about 20 h) is in the data
DATA_START, ANCHOR, END = ms(2023, 12, 1), ms(2024, 1, 1), ms(2024, 3, 1)


def _grid() -> dict[str, dict[str, Any]]:
    base = dataclasses.asdict(FAST_PARAMS)
    return {config_id_of(p): p for p in ({**base, "tp_r": 1.0}, {**base, "tp_r": 0.5})}


def _env(tmp_path: Path, *, start: int = DATA_START, skip: tuple[int, ...] = ()) -> RunEnv:
    write_lake(tmp_path, [(Market.SPOT, "BTCUSDT", synthetic_rows(start, END, skip=skip))])
    return make_env(tmp_path)


def _walk(env: RunEnv, tmp_path: Path, hypothesis: str, anchor: int = ANCHOR) -> dict[str, Any]:
    folds = plan_folds(anchor, END, initial_train_months=1, test_months=1, embargo_ms=DAY)
    return run_walk_forward(
        env,
        TEST_CONFIG,
        strategy_id="bracket_reference",
        grid=_grid(),
        symbols=BTC,
        folds=folds,
        n_min=5,
        hypothesis_id=hypothesis,
        report_dir=tmp_path / "wf",
    )


def _metrics(env: RunEnv, run_id: str) -> dict[str, Any]:
    return dict(json.loads((env.runs_dir / run_id / "metrics.json").read_text(encoding="utf-8")))


def test_walk_forward_end_to_end_on_a_synthetic_lake(tmp_path: Path) -> None:
    env = _env(tmp_path)
    grid = _grid()
    report = _walk(env, tmp_path, "WF-TEST")
    assert report["n_train_evaluations"] == 2
    (fold,) = report["folds"]
    assert {s["config_id"] for s in fold["scores"]} == set(grid)
    runs = {r["run_id"]: r for r in fold["scores"]}
    if fold["selected"] is not None:
        assert report["n_test_evaluations"] == 1
        assert fold["selected"] in grid
        assert fold["oos_run_id"] is not None
        assert len(report["run_ids"]) == 3
    else:  # no configuration reached n_min: no test run at all
        assert report["n_test_evaluations"] == 0
        assert len(report["run_ids"]) == 2
    # every evaluation is a recorded, integrity-clean run (WF-2: the warm-up is in the data);
    # train ranges end before the embargoed test window
    test_from = from_iso(fold["test"][0])
    for run_id in report["run_ids"]:
        manifest = json.loads((env.runs_dir / run_id / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["provenance"] == "BACKTEST"
        metrics = _metrics(env, run_id)
        assert metrics["integrity"]["problems"] == []
        assert metrics["integrity"]["missing_minutes_in_warmup"] == 0
        assert metrics["evidence_grade"] != "X"
        assert manifest["range"]["warmup_from_ms"] >= DATA_START
        rng_to = from_iso(manifest["range"]["to"])
        if run_id in runs:
            assert rng_to <= test_from - DAY
        else:
            assert from_iso(manifest["range"]["from"]) == test_from
    assert all(s["valid"] for s in fold["scores"])
    assert report["warmup"]["data_start"] == "2023-12-01T00:00:00Z"
    assert report["warmup"]["earliest_valid_anchor"] == "2024-01-01T00:00:00Z"
    oos = report["oos"]
    # the registry counts every (configuration, window) evaluated for the family as a trial,
    # train runs included, not only the winner: the conservative count goes into the verdict
    assert oos["n_trials"] == len(report["run_ids"])
    assert oos["verdict"] in {
        INSUFFICIENT_OOS_EVIDENCE,
        NO_EDGE_DEMONSTRATED,
        OOS_MARGIN_CERTIFIED_DEVELOPMENT_ONLY,
    }
    assert oos["failed_test_folds"] == []
    assert "Validation (2025" in oos["not_validation"]
    written = json.loads((tmp_path / "wf" / "walkforward.json").read_text(encoding="utf-8"))
    assert written["oos"]["verdict"] == oos["verdict"]
    assert written["provenance"] == "BACKTEST"


def test_an_anchor_without_warm_up_is_refused_before_anything_is_recorded(tmp_path: Path) -> None:
    """The audit's reproduction: data starting at the anchor makes every train run grade X."""
    env = _env(tmp_path, start=ANCHOR)
    with pytest.raises(WalkForwardError, match="earliest valid anchor is 2024-02-01"):
        _walk(env, tmp_path, "WF-NO-WARMUP")
    assert not env.runs_dir.exists()
    assert env.trials.n_trials("bracket_reference") == 0


def test_grade_x_train_runs_are_ineligible(tmp_path: Path) -> None:
    """A gap inside the train window: the runner grades both train runs X, so nothing is
    selected and no test window is evaluated."""
    gap = tuple(range(ms(2024, 1, 10), ms(2024, 1, 10) + 3 * MIN, MIN))
    env = _env(tmp_path, skip=gap)
    report = _walk(env, tmp_path, "WF-X-TRAIN")
    (fold,) = report["folds"]
    assert fold["status"] == "NO_SELECTION"
    assert sorted(fold["train_invalid"]) == sorted(_grid())
    for s in fold["scores"]:
        assert s["valid"] is False
        assert s["margin_lcb"] is None  # an invalid run's numbers are not reported
        assert any("data gaps" in p for p in s["problems"])
        assert _metrics(env, s["run_id"])["evidence_grade"] == "X"
    assert report["n_test_evaluations"] == 0
    assert report["oos"]["verdict"] == INSUFFICIENT_OOS_EVIDENCE
    assert report["oos"]["folds_without_valid_train"] == [0]


def test_a_grade_x_test_run_voids_the_stitched_verdict(tmp_path: Path) -> None:
    """A gap inside the test window only: train runs are clean and a configuration is selected;
    its test run is graded X, so nothing is certified."""
    gap = tuple(range(ms(2024, 2, 10), ms(2024, 2, 10) + 3 * MIN, MIN))
    env = _env(tmp_path, skip=gap)
    report = _walk(env, tmp_path, "WF-X-TEST")
    (fold,) = report["folds"]
    assert all(s["valid"] for s in fold["scores"])
    assert fold["selected"] is not None  # the synthetic train window always has enough trades
    assert fold["status"] == "TEST_INVALID"
    assert fold["oos_valid"] is False
    assert any("data gaps" in p for p in fold["oos_problems"])
    assert _metrics(env, fold["oos_run_id"])["evidence_grade"] == "X"
    oos = report["oos"]
    assert oos["verdict"] == INVALID_OOS_EVIDENCE
    assert oos["failed_test_folds"] == [0]
    assert oos["oos_trades"] is None
    assert "margin_lcb" not in oos


def test_rescore_certifies_every_train_run_of_a_fold_with_one_common_trial_count(
    tmp_path: Path,
) -> None:
    """The first run was certified with n_trials 1, the second with 2; after rescoring both
    carry the certification for the registry's common count (2)."""
    env = _env(tmp_path)
    base = dataclasses.asdict(FAST_PARAMS)
    grid = {"tp1": {**base, "tp_r": 1.0}, "tp05": {**base, "tp_r": 0.5}}
    (fold,) = plan_folds(ANCHOR, END, initial_train_months=1, test_months=1, embargo_ms=DAY)
    ev = RunnerEvaluator(
        env, TEST_CONFIG, strategy_id="bracket_reference", grid=grid,
        symbols=BTC, hypothesis_id="WF-RESCORE",
    )  # fmt: skip
    first, second = ev.score("tp1", fold), ev.score("tp05", fold)
    assert first.valid
    assert second.valid
    stored = [ev.runs[0].metrics["n_trials"], ev.runs[1].metrics["n_trials"]]
    assert stored == [1, 2]
    rescored = ev.rescore([first, second], fold)
    assert [s.config_id for s in rescored] == ["tp1", "tp05"]
    for score in rescored:
        assert score.run_id is not None
        assert score.valid
        loaded = load_run(env.runs_dir, score.run_id, env.trials, recompute=True)
        assert loaded is not None
        assert loaded["summary"]["n_trials"] == 2  # the common count
        assert score.margin_lcb == loaded["summary"]["margin_lcb"]
    assert rescored[1] == second  # already certified with the common count: unchanged


def test_rescore_rejects_a_run_whose_metrics_no_longer_match_the_manifest(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    (fold,) = plan_folds(ANCHOR, END, initial_train_months=1, test_months=1, embargo_ms=DAY)
    ev = RunnerEvaluator(
        env, TEST_CONFIG, strategy_id="bracket_reference",
        grid={"a": dataclasses.asdict(FAST_PARAMS)}, symbols=BTC, hypothesis_id="WF-TAMPER",
    )  # fmt: skip
    score = ev.score("a", fold)
    assert score.valid
    assert score.run_id is not None
    path = env.runs_dir / score.run_id / "metrics.json"
    path.write_text(path.read_text(encoding="utf-8").rstrip("\n") + " \n", encoding="utf-8")
    (rescored,) = ev.rescore([score], fold)
    assert not rescored.valid
    assert any("do not match the manifest" in p for p in rescored.problems)
