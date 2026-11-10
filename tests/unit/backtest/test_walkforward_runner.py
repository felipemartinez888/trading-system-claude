"""Walk-forward through the real backtest runner on a synthetic lake (no network, no pyarrow).

Every evaluation is a normal recorded run: manifests exist, train runs end before the embargoed
test window, only the selected configuration runs out of sample, and the stitched verdict counts
every configuration of the strategy family as a trial.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from bt_helpers import FAST_PARAMS, TEST_CONFIG, make_env, ms, synthetic_rows, write_lake

from tsclaude.contracts.market import Market
from tsclaude.contracts.timeutil import from_iso
from tsclaude.research.runner import load_run
from tsclaude.research.walkforward import (
    INSUFFICIENT_OOS_EVIDENCE,
    NO_EDGE_DEMONSTRATED,
    OOS_MARGIN_CERTIFIED_DEVELOPMENT_ONLY,
    RunnerEvaluator,
    config_id_of,
    plan_folds,
    run_walk_forward,
)

DAY = 86_400_000


def test_walk_forward_end_to_end_on_a_synthetic_lake(tmp_path: Path) -> None:
    start, end = ms(2024, 1, 1), ms(2024, 3, 1)
    write_lake(tmp_path, [(Market.SPOT, "BTCUSDT", synthetic_rows(start, end))])
    env = make_env(tmp_path)
    base = dataclasses.asdict(FAST_PARAMS)
    grid = {config_id_of(p): p for p in ({**base, "tp_r": 1.0}, {**base, "tp_r": 0.5})}
    folds = plan_folds(start, end, initial_train_months=1, test_months=1, embargo_ms=DAY)
    assert len(folds) == 1
    report = run_walk_forward(
        env,
        TEST_CONFIG,
        strategy_id="bracket_reference",
        grid=grid,
        symbols=[(Market.SPOT, "BTCUSDT")],
        folds=folds,
        n_min=5,
        hypothesis_id="WF-TEST",
        report_dir=tmp_path / "wf",
    )
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
    # every evaluation is a recorded run; train ranges end before the embargoed test window
    test_from = from_iso(fold["test"][0])
    for run_id in report["run_ids"]:
        manifest = json.loads((env.runs_dir / run_id / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["provenance"] == "BACKTEST"
        rng_to = from_iso(manifest["range"]["to"])
        if run_id in runs:
            assert rng_to <= test_from - DAY
        else:
            assert from_iso(manifest["range"]["from"]) == test_from
    oos = report["oos"]
    # the registry counts every (configuration, window) evaluated for the family as a trial,
    # train runs included, not only the winner: the conservative count goes into the verdict
    assert oos["n_trials"] == len(report["run_ids"])
    assert oos["verdict"] in {
        INSUFFICIENT_OOS_EVIDENCE,
        NO_EDGE_DEMONSTRATED,
        OOS_MARGIN_CERTIFIED_DEVELOPMENT_ONLY,
    }
    assert "Validation (2025" in oos["not_validation"]
    written = json.loads((tmp_path / "wf" / "walkforward.json").read_text(encoding="utf-8"))
    assert written["oos"]["verdict"] == oos["verdict"]
    assert written["provenance"] == "BACKTEST"


def test_rescore_certifies_every_train_run_of_a_fold_with_one_common_trial_count(
    tmp_path: Path,
) -> None:
    """The first run was certified with n_trials 1, the second with 2; after rescoring both
    carry the certification for the registry's common count (2)."""
    start, end = ms(2024, 1, 1), ms(2024, 3, 1)
    write_lake(tmp_path, [(Market.SPOT, "BTCUSDT", synthetic_rows(start, end))])
    env = make_env(tmp_path)
    base = dataclasses.asdict(FAST_PARAMS)
    grid = {"tp1": {**base, "tp_r": 1.0}, "tp05": {**base, "tp_r": 0.5}}
    (fold,) = plan_folds(start, end, initial_train_months=1, test_months=1, embargo_ms=DAY)
    ev = RunnerEvaluator(
        env, TEST_CONFIG, strategy_id="bracket_reference", grid=grid,
        symbols=[(Market.SPOT, "BTCUSDT")], hypothesis_id="WF-RESCORE",
    )  # fmt: skip
    first, second = ev.score("tp1", fold), ev.score("tp05", fold)
    stored = [ev.runs[0].metrics["n_trials"], ev.runs[1].metrics["n_trials"]]
    assert stored == [1, 2]
    rescored = ev.rescore([first, second], fold)
    assert [s.config_id for s in rescored] == ["tp1", "tp05"]
    for score in rescored:
        assert score.run_id is not None
        loaded = load_run(env.runs_dir, score.run_id, env.trials, recompute=True)
        assert loaded is not None
        assert loaded["summary"]["n_trials"] == 2  # the common count
        assert score.margin_lcb == loaded["summary"]["margin_lcb"]
    assert rescored[1] == second  # already certified with the common count: unchanged
