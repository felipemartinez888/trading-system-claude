"""Anchored walk-forward (RESEARCH_PROTOCOL §3): folds, train-only selection, stitched OOS verdict.

The canary tests prove from the evaluator's call log that selection never sees a test window and
that only the selected configuration is evaluated out of sample.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from tsclaude.research.runner import BacktestConfig
from tsclaude.research.walkforward import (
    INSUFFICIENT_OOS_EVIDENCE,
    INVALID_OOS_EVIDENCE,
    NO_EDGE_DEMONSTRATED,
    OOS_MARGIN_CERTIFIED_DEVELOPMENT_ONLY,
    Fold,
    OosRun,
    Score,
    WalkForwardError,
    add_months,
    certify_oos,
    check_anchor,
    config_id_of,
    earliest_anchor_ms,
    month_start_at_or_after,
    plan_folds,
    run_problems,
    select,
    verdict,
    walk_forward,
)

M = 60_000
DAY = 1440 * M


def ms(y: int, m: int, d: int = 1) -> int:
    return int(datetime(y, m, d, tzinfo=UTC).timestamp() * 1000)


DEV_START, DEV_END = ms(2022, 1), ms(2025, 1)


# ---------------------------------------------------------------- folds
def test_anchored_folds_on_the_development_segment() -> None:
    folds = plan_folds(DEV_START, DEV_END, initial_train_months=12, test_months=3, embargo_ms=DAY)
    assert len(folds) == 8  # 2023-01 .. 2025-01 in quarters
    for i, f in enumerate(folds):
        assert f.index == i
        assert f.train_from_ms == DEV_START  # anchored
        assert f.test_from_ms == f.train_to_ms + DAY  # embargo between train and test
        assert f.test_to_ms == add_months(f.train_to_ms, 3)
        if i:
            assert f.train_to_ms == folds[i - 1].test_to_ms  # the train window grows
    assert folds[0].train_to_ms == ms(2023, 1)
    assert folds[-1].test_to_ms == DEV_END
    assert all(f.test_to_ms <= DEV_END for f in folds)


def test_only_whole_test_windows_are_kept() -> None:
    folds = plan_folds(
        ms(2022, 1), ms(2023, 5), initial_train_months=12, test_months=3, embargo_ms=M
    )
    assert [(f.train_to_ms, f.test_to_ms) for f in folds] == [(ms(2023, 1), ms(2023, 4))]
    assert folds[0].test_from_ms == ms(2023, 1) + M


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"initial_train_months": 0}, "must be >= 1"),
        ({"test_months": 0}, "must be >= 1"),
        ({"embargo_ms": -M}, "at least one minute"),
        ({"embargo_ms": 0}, "at least one minute"),
        ({"embargo_ms": 30_000}, "whole number of minutes"),
        ({"embargo_ms": 100 * DAY}, "longer than a test window"),
    ],
)
def test_invalid_fold_plans_are_refused(kwargs: dict[str, int], message: str) -> None:
    args = {"initial_train_months": 12, "test_months": 3, "embargo_ms": M, **kwargs}
    with pytest.raises(WalkForwardError, match=message):
        plan_folds(DEV_START, DEV_END, **args)


def test_no_fold_fits_and_month_starts_are_required() -> None:
    with pytest.raises(WalkForwardError, match="no whole fold fits"):
        plan_folds(ms(2022, 1), ms(2022, 6), initial_train_months=12, test_months=3, embargo_ms=M)
    with pytest.raises(WalkForwardError, match="not a UTC month start"):
        add_months(ms(2022, 1, 15), 1)
    assert add_months(ms(2022, 11), 3) == ms(2023, 2)


# ---------------------------------------------------------------- selection
def test_selection_ranks_by_margin_then_p_lcb_then_id_and_needs_n_min() -> None:
    scores = [
        Score("c", n=100, margin_lcb=0.02, p_lcb=0.50),
        Score("a", n=100, margin_lcb=0.03, p_lcb=0.40),
        Score("b", n=100, margin_lcb=0.03, p_lcb=0.45),  # same margin, higher p_lcb wins
        Score("d", n=10, margin_lcb=0.90, p_lcb=0.90),  # too few trades: not eligible
        Score("e", n=100, margin_lcb=None, p_lcb=None),  # no certification: not eligible
    ]
    best, reasons = select(scores, n_min=30)
    assert best is not None
    assert best.config_id == "b"
    assert reasons == []
    tie = [Score("z", 50, 0.01, 0.5), Score("y", 50, 0.01, 0.5)]
    chosen, _ = select(tie, n_min=30)
    assert chosen is not None
    assert chosen.config_id == "y"  # result-independent tie-break


def test_selection_can_pick_a_negative_margin_and_reports_none_eligible() -> None:
    best, _ = select([Score("a", 40, -0.2, 0.3), Score("b", 40, -0.1, 0.2)], n_min=30)
    assert best is not None
    assert best.config_id == "b"  # selection ranks; the OOS verdict decides
    none, reasons = select([Score("a", 5, 0.5, 0.9)], n_min=30)
    assert none is None
    assert "n >= 30" in reasons[0]
    with pytest.raises(WalkForwardError):
        select([], n_min=0)


# ---------------------------------------------------------------- the canary
@dataclass
class Recorder:
    """Fake evaluator: train scores and test outcomes per configuration, plus a call log."""

    train: dict[str, Score]
    test: dict[str, list[float]]
    calls: list[tuple[str, str, int, int]] = field(default_factory=list)
    #: fold index -> integrity problems of that fold's test run
    test_problems: dict[int, tuple[str, ...]] = field(default_factory=dict)

    def score(self, config_id: str, fold: Fold) -> Score:
        self.calls.append(("train", config_id, fold.train_from_ms, fold.train_to_ms))
        return self.train[config_id]

    def oos(self, config_id: str, fold: Fold) -> OosRun:
        self.calls.append(("test", config_id, fold.test_from_ms, fold.test_to_ms))
        return OosRun(
            tuple(self.test[config_id]),
            f"bt-{config_id}-{fold.index}",
            self.test_problems.get(fold.index, ()),
        )

    def rescore(self, scores: list[Score], fold: Fold) -> list[Score]:
        return scores


@dataclass
class Rescoring(Recorder):
    """Train scores certified at their own record time; rescore applies one common count."""

    common: dict[str, Score] = field(default_factory=dict)

    def rescore(self, scores: list[Score], fold: Fold) -> list[Score]:
        self.calls.append(("rescore", ",".join(s.config_id for s in scores), 0, 0))
        return [self.common[s.config_id] for s in scores]


def test_selection_never_sees_a_test_window_and_only_the_winner_is_tested() -> None:
    folds = plan_folds(DEV_START, DEV_END, initial_train_months=12, test_months=3, embargo_ms=DAY)
    rec = Recorder(
        train={"a": Score("a", 50, 0.01, 0.5), "b": Score("b", 50, 0.05, 0.6)},
        test={"a": [9.0] * 50, "b": [1.0, -1.0]},  # "a" would look great out of sample
    )
    result = walk_forward(["a", "b"], folds, rec, n_min=30)
    for kind, _config, lo, hi in rec.calls:
        if kind == "train":  # every train call ends before its fold's test window starts
            fold = next(f for f in folds if f.train_to_ms == hi)
            assert lo == DEV_START
            assert hi < fold.test_from_ms
    tests = [c for c in rec.calls if c[0] == "test"]
    assert [c[1] for c in tests] == ["b"] * len(folds)  # the test result never feeds selection
    assert result.n_train_evaluations == 2 * len(folds)
    assert result.n_test_evaluations == len(folds)
    assert result.oos_outcomes() == [1.0, -1.0] * len(folds)
    # the calls are ordered: all train evaluations of a fold come before its single test run
    for f in folds:
        idx = [i for i, c in enumerate(rec.calls) if c[2:] in ((f.train_from_ms, f.train_to_ms),)]
        test_idx = next(
            i for i, c in enumerate(rec.calls) if c[0] == "test" and c[2] == f.test_from_ms
        )
        assert max(idx) < test_idx


def test_selection_uses_the_rescored_comparable_scores() -> None:
    """Review finding: scores certified with different trial counts are not comparable, so the
    fold's scores are rescored with one common count before selection."""
    folds = plan_folds(DEV_START, ms(2023, 4), initial_train_months=12, test_months=3, embargo_ms=M)
    rec = Rescoring(
        train={"a": Score("a", 50, 0.10, 0.6), "b": Score("b", 50, 0.05, 0.6)},  # a looks best
        test={"a": [1.0], "b": [1.0, -1.0]},
        common={"a": Score("a", 50, 0.01, 0.5), "b": Score("b", 50, 0.04, 0.55)},  # b is best
    )
    result = walk_forward(["a", "b"], folds, rec, n_min=30)
    assert result.folds[0].selected == "b"
    kinds = [c[0] for c in rec.calls]
    assert kinds == ["train", "train", "rescore", "test"]  # rescored after all train runs


def test_rescore_must_keep_one_score_per_configuration_in_order() -> None:
    folds = plan_folds(DEV_START, ms(2023, 4), initial_train_months=12, test_months=3, embargo_ms=M)
    rec = Rescoring(
        train={"a": Score("a", 50, 0.1, 0.6), "b": Score("b", 50, 0.1, 0.6)},
        test={"a": [], "b": []},
        common={"a": Score("b", 50, 0.1, 0.6), "b": Score("a", 50, 0.1, 0.6)},
    )
    with pytest.raises(WalkForwardError, match="one score per configuration"):
        walk_forward(["a", "b"], folds, rec, n_min=30)


def test_a_fold_without_an_eligible_configuration_runs_no_test() -> None:
    folds = plan_folds(DEV_START, ms(2023, 4), initial_train_months=12, test_months=3, embargo_ms=M)
    rec = Recorder(train={"a": Score("a", 3, 0.5, 0.9)}, test={"a": [1.0]})
    result = walk_forward(["a"], folds, rec, n_min=30)
    assert result.folds[0].selected is None
    assert all(c[0] == "train" for c in rec.calls)
    assert verdict(result, None, n_trials=1)["verdict"] == INSUFFICIENT_OOS_EVIDENCE


def test_the_grid_must_be_non_empty_and_unique() -> None:
    folds = plan_folds(DEV_START, DEV_END, initial_train_months=12, test_months=3, embargo_ms=M)
    rec = Recorder(train={}, test={})
    with pytest.raises(WalkForwardError, match="empty"):
        walk_forward([], folds, rec, n_min=1)
    with pytest.raises(WalkForwardError, match="unique"):
        walk_forward(["a", "a"], folds, rec, n_min=1)


# ---------------------------------------------------------------- verdict
CFG = BacktestConfig(n_resamples=2000)


def _result(outcomes: list[float]) -> object:
    folds = plan_folds(DEV_START, ms(2023, 4), initial_train_months=12, test_months=3, embargo_ms=M)
    rec = Recorder(train={"a": Score("a", 100, 0.1, 0.6)}, test={"a": outcomes})
    return walk_forward(["a"], folds, rec, n_min=30)


def test_a_coin_flip_strategy_shows_no_edge() -> None:
    outcomes = [1.0, -1.0] * 150  # p = 0.5 at break-even 0.5 (realized)
    result = _result(outcomes)
    cert = certify_oos(
        outcomes, cfg=CFG, n_trials=4, max_loss_r=1.5, max_loss_source="test", seed=7
    )
    out = verdict(result, cert, n_trials=4)  # type: ignore[arg-type]
    assert out["verdict"] == NO_EDGE_DEMONSTRATED
    assert out["oos_trades"] == 300
    assert out["n_trials"] == 4
    assert out["p_star_source"] == "realized"
    assert "Validation (2025, one preregistered run)" in out["not_validation"]


def test_a_strong_synthetic_edge_is_certified_but_never_called_validated() -> None:
    outcomes = ([1.0] * 3 + [-1.0]) * 120  # p = 0.75 vs realized break-even 0.5
    result = _result(outcomes)
    cert = certify_oos(
        outcomes, cfg=CFG, n_trials=2, max_loss_r=1.5, max_loss_source="test", seed=7
    )
    out = verdict(result, cert, n_trials=2)  # type: ignore[arg-type]
    assert out["verdict"] == OOS_MARGIN_CERTIFIED_DEVELOPMENT_ONLY
    assert "VALIDATED" not in out["verdict"]
    assert out["margin_lcb"] > 0


def test_fewer_than_two_outcomes_cannot_be_certified() -> None:
    assert (
        certify_oos([1.0], cfg=CFG, n_trials=1, max_loss_r=1.5, max_loss_source="t", seed=1) is None
    )


def test_config_ids_are_stable_content_hashes() -> None:
    a = config_id_of({"tp_r": 1.0, "stop_atr": 1.5})
    assert a == config_id_of({"stop_atr": 1.5, "tp_r": 1.0})
    assert a != config_id_of({"stop_atr": 1.5, "tp_r": 0.5})
    assert len(a) == 12


# ---------------------------------------------------------------- integrity gates (audit WF-1)
BAD = ("integrity: data gaps in warm-up: 5 missing 1m bars before 'from'",)


def test_an_invalid_train_run_is_ineligible_whatever_its_numbers() -> None:
    scores = [
        Score("a", 500, 0.50, 0.90, "bt-a", BAD),  # the best numbers, but graded X
        Score("b", 100, 0.01, 0.55, "bt-b"),
    ]
    best, reasons = select(scores, n_min=30)
    assert best is not None
    assert best.config_id == "b"
    assert reasons == []
    none, why = select([Score("a", 500, 0.5, 0.9, "bt-a", BAD)], n_min=30)
    assert none is None
    assert any("failed integrity" in r and "a" in r for r in why)
    # an invalid score's numbers are not reported as evidence
    assert scores[0].to_dict() == {
        "config_id": "a", "n": None, "margin_lcb": None, "p_lcb": None, "run_id": "bt-a",
        "valid": False, "problems": list(BAD),
    }  # fmt: skip


def test_a_fold_whose_train_runs_all_fail_runs_no_test_and_is_reported() -> None:
    folds = plan_folds(DEV_START, DEV_END, initial_train_months=12, test_months=3, embargo_ms=DAY)
    rec = Recorder(train={"a": Score("a", 100, 0.1, 0.6, "bt-a", BAD)}, test={"a": [1.0] * 10})
    result = walk_forward(["a"], folds, rec, n_min=30)
    assert all(c[0] == "train" for c in rec.calls)  # no test window was ever evaluated
    assert {f.status for f in result.folds} == {"NO_SELECTION"}
    assert result.folds[0].to_dict()["train_invalid"] == ["a"]
    out = verdict(result, None, n_trials=1)
    assert out["verdict"] == INSUFFICIENT_OOS_EVIDENCE
    assert out["folds_without_valid_train"] == list(range(len(folds)))
    assert "data quality only" in out["coverage_note"]


def test_a_failed_test_run_voids_the_stitched_verdict_and_stops_the_walk_forward() -> None:
    folds = plan_folds(DEV_START, DEV_END, initial_train_months=12, test_months=3, embargo_ms=DAY)
    assert len(folds) == 8
    rec = Recorder(
        train={"a": Score("a", 100, 0.1, 0.6)},
        test={"a": ([1.0] * 3 + [-1.0]) * 50},  # a strong edge, but fold 2's test run is invalid
        test_problems={2: ("integrity: data gaps: 3 missing 1m bars in range",)},
    )
    result = walk_forward(["a"], folds, rec, n_min=30)
    assert [f.status for f in result.folds] == ["TESTED", "TESTED", "TEST_INVALID"]
    assert result.failed_test_folds == [2]
    assert result.planned_folds == 8
    assert len([c for c in rec.calls if c[0] == "test"]) == 3  # nothing ran after the failure
    with pytest.raises(WalkForwardError, match="void"):
        result.oos_outcomes()
    out = verdict(result, None, n_trials=1)
    assert out["verdict"] == INVALID_OOS_EVIDENCE
    assert out["oos_trades"] is None
    assert "margin_lcb" not in out
    assert any("fold 2" in r and "missing 1m bars" in r for r in out["reasons"])
    assert any("5 planned fold(s) not run" in r for r in out["reasons"])
    failed = result.folds[2].to_dict()
    assert failed["oos_valid"] is False
    assert failed["oos_trades"] is None
    assert failed["oos_problems"] == ["integrity: data gaps: 3 missing 1m bars in range"]
    # a certification over a void set is refused, never reported
    cert = certify_oos([1.0, -1.0] * 2, cfg=CFG, n_trials=1, max_loss_r=1.5,
                       max_loss_source="t", seed=1)  # fmt: skip
    with pytest.raises(WalkForwardError, match="void"):
        verdict(result, cert, n_trials=1)


@pytest.mark.parametrize(
    ("metrics", "expected"),
    [
        (None, "no metrics"),
        ({"evidence_grade": "C"}, "no integrity record"),
        ({"evidence_grade": "C", "integrity": {}}, "no integrity record"),
        ({"evidence_grade": "X", "integrity": {"problems": ["no bars in range"]}}, "no bars"),
        (
            {"evidence_grade": "X", "grade_reasons": ["r"], "integrity": {"problems": []}},
            "graded X",
        ),
        ({"integrity": {"problems": []}}, "no evidence grade"),
    ],
)
def test_run_problems_fail_closed(metrics: dict[str, object] | None, expected: str) -> None:
    problems = run_problems(metrics)
    assert problems
    assert any(expected in p for p in problems)


def test_a_clean_run_has_no_problems() -> None:
    assert run_problems({"evidence_grade": "C", "integrity": {"problems": []}}) == []
    assert run_problems({"evidence_grade": "D", "integrity": {"problems": []}}) == []


# ---------------------------------------------------------------- warm-up anchor (audit WF-2)
def test_the_earliest_anchor_leaves_the_whole_warm_up_inside_the_data() -> None:
    warmup = 1185 * M
    assert earliest_anchor_ms(DEV_START, 0) == DEV_START
    assert earliest_anchor_ms(DEV_START, warmup) == ms(2022, 2)
    assert earliest_anchor_ms(ms(2021, 12, 31), 2 * DAY) == ms(2022, 2)
    assert earliest_anchor_ms(ms(2021, 12, 31), DAY) == ms(2022, 1)  # exactly enough
    assert month_start_at_or_after(ms(2022, 3)) == ms(2022, 3)
    assert month_start_at_or_after(ms(2022, 3) + 1) == ms(2022, 4)
    assert month_start_at_or_after(ms(2022, 12, 5)) == ms(2023, 1)
    check_anchor(ms(2022, 2), data_start_ms=DEV_START, warmup_ms=warmup)  # valid: no error
    with pytest.raises(WalkForwardError, match="earliest valid anchor is 2022-02-01"):
        check_anchor(DEV_START, data_start_ms=DEV_START, warmup_ms=warmup)
    with pytest.raises(WalkForwardError, match="no stored 1m bars"):
        check_anchor(ms(2022, 2), data_start_ms=None, warmup_ms=warmup)
    with pytest.raises(WalkForwardError, match="negative"):
        earliest_anchor_ms(DEV_START, -1)
