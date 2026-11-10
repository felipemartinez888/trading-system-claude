"""Walk-forward infrastructure for M4 (audit WF-4, WF-6, WF-7 and the trial budget), through the
real runner on a synthetic lake: resume after a restart with verified reuse, partial progress,
refused resumes, the trial-budget projection, dataset pins, preregistration ids, the warm-up gap
check, explicit validity checks and the final-selection mechanism (pending governance)."""

from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from bt_helpers import FAST_PARAMS, TEST_CONFIG, make_env, ms, synthetic_rows, write_lake

from tsclaude.contracts.market import Market
from tsclaude.research import runner
from tsclaude.research.max_loss_cache import MaxLossCache
from tsclaude.research.prereg import PreregistrationRefused
from tsclaude.research.runner import RunEnv, RunOutcome
from tsclaude.research.walkforward import (
    FINAL_SELECTION_RULE,
    OOS_MARGIN_CERTIFIED_DEVELOPMENT_ONLY,
    Fold,
    Score,
    WalkForwardError,
    WalkForwardResult,
    config_id_of,
    final_selection,
    plan_folds,
    run_walk_forward,
    validity_checks,
)
from tsclaude.research.wf_progress import ProgressLedger, ResumeRefused

DAY = 86_400_000
MIN = 60_000
BTC = [(Market.SPOT, "BTCUSDT")]
DATA_START, ANCHOR, END = ms(2023, 12, 1), ms(2024, 1, 1), ms(2024, 3, 1)
BASE = dataclasses.asdict(FAST_PARAMS)
GRID = {config_id_of(p): p for p in ({**BASE, "tp_r": 1.0}, {**BASE, "tp_r": 0.5})}
FOLDS = plan_folds(ANCHOR, END, initial_train_months=1, test_months=1, embargo_ms=DAY)


def _env(data: Path, **kw: Any) -> RunEnv:
    env = make_env(data)
    return dataclasses.replace(env, max_loss_cache=MaxLossCache(data / "cache"), **kw)


def _walk(env: RunEnv, data: Path, **kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "strategy_id": "bracket_reference", "grid": GRID, "symbols": BTC, "folds": FOLDS,
        "n_min": 5, "hypothesis_id": "WF-RESUME", "work_root": data / "wf",
    }  # fmt: skip
    args.update(kw)
    return run_walk_forward(env, TEST_CONFIG, **args)


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    """What must not depend on whether the walk-forward was interrupted."""
    oos = report["oos"]
    return {
        "selected": [f["selected"] for f in report["folds"]],
        "verdict": oos["verdict"],
        "oos_trades": oos.get("oos_trades"),
        "oos_wins": oos.get("oos_wins"),
        "margin_lcb": oos.get("margin_lcb"),
        "n_trials": oos["n_trials"],
        "scores": [
            [(s["config_id"], s["margin_lcb"]) for s in f["scores"]] for f in report["folds"]
        ],
    }


@pytest.fixture(scope="module")
def done(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    """One completed walk-forward (3 runs), copied by each test that resumes it."""
    data = tmp_path_factory.mktemp("wf-done")
    write_lake(data, [(Market.SPOT, "BTCUSDT", synthetic_rows(DATA_START, END))])
    report = _walk(_env(data), data)
    assert report["resume"]["executed_runs"] == 3
    assert report["plan"]["resumable"] is True
    return data, report


def _copy(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst)
    return dst


def test_a_completed_walk_forward_is_reused_without_running_anything(
    done: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    src, first = done
    data = _copy(src, tmp_path / "d")
    env = _env(data)
    rows_before = env.trials.list("bracket_reference")
    again = _walk(env, data)
    assert again["resume"] == {
        "ledger": str(data / "wf" / first["plan"]["plan_id"] / "progress.jsonl"),
        "reused_runs": 3, "executed_runs": 0, "ignored_partial_line": False,
    }  # fmt: skip
    assert again["plan"]["plan_id"] == first["plan"]["plan_id"]
    assert _summary(again) == _summary(first)
    assert again["run_ids"] == first["run_ids"]
    assert env.trials.list("bracket_reference") == rows_before  # no evaluation was repeated
    assert again["trial_budget"]["new_trials_max"] == 0
    assert all(again["checks"].values())


def test_an_interrupted_walk_forward_resumes_to_the_uninterrupted_result(
    done: tuple[Path, dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash before the out-of-sample run (the PC restarts): the train runs are reused and
    the result equals the uninterrupted walk-forward's."""
    _, uninterrupted = done
    data = tmp_path / "d"
    write_lake(data, [(Market.SPOT, "BTCUSDT", synthetic_rows(DATA_START, END))])
    env = _env(data)
    real = runner.run_backtest
    calls: list[int] = []

    def crash_on_third(*args: Any, **kwargs: Any) -> RunOutcome:
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("simulated restart")
        return real(*args, **kwargs)

    monkeypatch.setattr(runner, "run_backtest", crash_on_third)
    with pytest.raises(RuntimeError, match="simulated restart"):
        _walk(env, data)
    monkeypatch.undo()
    resumed = _walk(env, data)
    assert resumed["resume"]["reused_runs"] == 2
    assert resumed["resume"]["executed_runs"] == 1
    assert _summary(resumed) == _summary(uninterrupted)
    assert all(r["evaluations"] == 1 for r in env.trials.list("bracket_reference"))


def test_a_run_finished_but_not_logged_is_run_again_without_a_new_trial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "d"
    write_lake(data, [(Market.SPOT, "BTCUSDT", synthetic_rows(DATA_START, END))])
    env = _env(data)
    real = ProgressLedger.append

    def crash_once(self: ProgressLedger, entry: Any) -> None:
        if not self.entries:
            raise RuntimeError("simulated restart before the ledger line")
        real(self, entry)

    monkeypatch.setattr(ProgressLedger, "append", crash_once)
    with pytest.raises(RuntimeError, match="before the ledger line"):
        _walk(env, data)
    monkeypatch.undo()
    trials_before = env.trials.n_trials("bracket_reference")
    report = _walk(env, data)
    assert report["resume"]["reused_runs"] == 0
    assert report["resume"]["executed_runs"] == 3
    assert trials_before == 1
    assert env.trials.n_trials("bracket_reference") == 3  # the orphan's key was not new
    evaluations = sorted(r["evaluations"] for r in env.trials.list("bracket_reference"))
    assert evaluations == [1, 1, 2]  # the orphan's configuration was evaluated twice


def test_a_partial_ledger_line_is_ignored_and_repaired(
    done: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    src, first = done
    data = _copy(src, tmp_path / "d")
    ledger = data / "wf" / first["plan"]["plan_id"] / "progress.jsonl"
    lines = ledger.read_text(encoding="utf-8").splitlines()
    ledger.write_text("\n".join(lines[:-1]) + "\n" + lines[-1][:20], encoding="utf-8")
    report = _walk(_env(data), data)
    assert report["resume"]["ignored_partial_line"] is True
    assert (report["resume"]["reused_runs"], report["resume"]["executed_runs"]) == (2, 1)
    repaired = ledger.read_text(encoding="utf-8")
    assert repaired.endswith("\n")
    assert all(json.loads(line) for line in repaired.splitlines())
    assert _summary(report)["verdict"] == _summary(first)["verdict"]


@pytest.mark.parametrize("what", ["metrics", "trades", "ledger", "missing"])
def test_an_untrustworthy_record_refuses_the_resume(
    done: tuple[Path, dict[str, Any]], tmp_path: Path, what: str
) -> None:
    src, first = done
    data = _copy(src, tmp_path / "d")
    run_dir = data / "runs" / first["run_ids"][0]
    ledger = data / "wf" / first["plan"]["plan_id"] / "progress.jsonl"
    if what == "metrics":
        p = run_dir / "metrics.json"
        p.write_text(p.read_text(encoding="utf-8").rstrip("\n") + " \n", encoding="utf-8")
    elif what == "trades":
        p = run_dir / "trades.parquet"
        p.write_bytes(p.read_bytes() + b"\n")
    elif what == "ledger":
        text = ledger.read_text(encoding="utf-8").splitlines()
        text.insert(2, '{"kind": "run"}')
        ledger.write_text("\n".join(text) + "\n", encoding="utf-8")
    else:
        shutil.rmtree(run_dir)
    env = _env(data)
    rows_before = env.trials.list("bracket_reference")
    with pytest.raises(ResumeRefused):
        _walk(env, data)
    assert env.trials.list("bracket_reference") == rows_before  # nothing ran


def test_a_ledger_of_another_plan_is_refused(
    done: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    src, first = done
    data = _copy(src, tmp_path / "d")
    with pytest.raises(ResumeRefused, match="another walk-forward plan"):
        _walk(_env(data), data, n_min=6, report_dir=data / "wf" / first["plan"]["plan_id"])


def test_without_identified_code_nothing_is_resumed(tmp_path: Path) -> None:
    data = tmp_path / "d"
    write_lake(data, [(Market.SPOT, "BTCUSDT", synthetic_rows(DATA_START, END))])
    env = _env(data, code_version=lambda: {"commit": "test", "dirty": True})
    grid = dict(list(GRID.items())[:1])
    first = _walk(env, data, grid=grid)
    second = _walk(env, data, grid=grid)
    assert first["plan"]["resumable"] is False
    assert "uncommitted changes" in first["plan"]["not_resumable_reason"]
    assert first["resume"]["ledger"] is None
    assert second["resume"]["executed_runs"] == 2
    assert first["report_path"] != second["report_path"]
    assert first["oos"]["verdict"] == second["oos"]["verdict"]


def test_the_trial_budget_is_projected_and_enforced_before_anything_runs(
    done: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    src, first = done
    budget = first["trial_budget"]
    assert budget["family_trials_before"] == 0
    assert (budget["train_new_trials"], budget["test_new_trials_max"]) == (2, 1)
    assert budget["new_trials_max"] == 3
    assert first["oos"]["n_trials"] == 3
    fresh = tmp_path / "fresh"
    write_lake(fresh, [(Market.SPOT, "BTCUSDT", synthetic_rows(DATA_START, END))])
    env = _env(fresh)
    with pytest.raises(WalkForwardError, match="above --max-new-trials 2"):
        _walk(env, fresh, max_new_trials=2)
    assert not env.runs_dir.exists()
    assert env.trials.n_trials("bracket_reference") == 0
    # resuming a completed walk-forward adds no trial, so a zero budget allows it
    data = _copy(src, tmp_path / "d")
    assert _walk(_env(data), data, max_new_trials=0)["resume"]["executed_runs"] == 0


def test_dataset_pins_are_checked_and_recorded(tmp_path: Path) -> None:
    data = tmp_path / "d"
    (real,) = write_lake(data, [(Market.SPOT, "BTCUSDT", synthetic_rows(DATA_START, END))])
    (late,) = write_lake(
        data, [(Market.SPOT, "ETHUSDT", synthetic_rows(ms(2024, 1, 1), ms(2024, 1, 2)))]
    )
    env = _env(data)
    with pytest.raises(WalkForwardError, match="not in the lake"):
        _walk(env, data, dataset_ids=["0" * 64])
    with pytest.raises(WalkForwardError, match="no stored 1m bars"):
        _walk(env, data, dataset_ids=[late])  # a pin without this symbol's bars
    assert not env.runs_dir.exists()
    report = _walk(env, data, dataset_ids=[real], grid=dict(list(GRID.items())[:1]))
    assert report["dataset_ids"] == [real]
    for run_id in report["run_ids"]:
        manifest = json.loads((env.runs_dir / run_id / "manifest.json").read_text("utf-8"))
        assert manifest["datasets"]["main"] == [real]


def test_an_uncommitted_preregistration_is_refused_before_anything_runs(tmp_path: Path) -> None:
    data = tmp_path / "d"
    write_lake(data, [(Market.SPOT, "BTCUSDT", synthetic_rows(DATA_START, END))])
    env = _env(data, repo_root=tmp_path / "repo")
    (tmp_path / "repo" / "preregistrations").mkdir(parents=True)
    (tmp_path / "repo" / "preregistrations" / "H-TEST.yaml").write_text("id: H-TEST\n")
    with pytest.raises(PreregistrationRefused):
        _walk(env, data, prereg_id="H-TEST")
    assert not env.runs_dir.exists()


def test_a_gap_in_the_first_warm_up_and_non_development_folds_are_refused(
    tmp_path: Path,
) -> None:
    data = tmp_path / "d"
    gap = (ANCHOR - 3 * 3_600_000,)  # one missing minute three hours before the anchor
    write_lake(data, [(Market.SPOT, "BTCUSDT", synthetic_rows(DATA_START, END, skip=gap))])
    env = _env(data)
    with pytest.raises(WalkForwardError, match="1 missing 1m bar"):
        _walk(env, data)
    late = [Fold(0, ms(2024, 10, 1), ms(2024, 12, 1), ms(2024, 12, 2), ms(2025, 2, 1))]
    with pytest.raises(WalkForwardError, match="development segment only"):
        _walk(env, data, folds=late)
    with pytest.raises(WalkForwardError, match="min_oos_coverage"):
        _walk(env, data, min_oos_coverage=1.5)
    assert not env.runs_dir.exists()


def test_final_selection_runs_only_after_a_positive_verdict_and_is_pending_governance(
    done: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    src, first = done
    data = _copy(src, tmp_path / "d")
    report = _walk(_env(data), data, final_selection_enabled=True)  # another plan id
    final = report["final_selection"]
    assert final["rule"] == FINAL_SELECTION_RULE
    assert report["oos"]["verdict"] != OOS_MARGIN_CERTIFIED_DEVELOPMENT_ONLY
    assert final["status"] == "NOT_RUN"
    assert "PENDING GOVERNANCE" in final["governance"]
    # its possible trials are counted in advance in the verdict (conservative)
    assert report["trial_budget"]["final_selection_new_trials_max"] == 2
    assert report["oos"]["n_trials"] == report["n_trials_family_now"] + 2
    assert first["final_selection"] is None


class _StubEvaluator:
    """Duck-typed evaluator for the final-selection rule (selection logic only)."""

    def __init__(self, scores: dict[str, Score]) -> None:
        self._grid = {c: {"tp_r": i} for i, c in enumerate(scores)}
        self._scores = scores
        self.calls: list[tuple[str, str, int, int, int]] = []

    def evaluate_window(self, c: str, phase: str, index: int, lo: int, hi: int) -> Score:
        self.calls.append((c, phase, index, lo, hi))
        return self._scores[c]

    def rescore(self, scores: list[Score], fold: Fold | None = None) -> list[Score]:
        return scores

    def strategy_hash(self, c: str) -> str:
        return f"hash-{c}"


def test_final_selection_applies_the_objective_on_the_whole_development_window() -> None:
    stub = _StubEvaluator(
        {"a": Score("a", 400, 0.01, 0.6, "bt-a"), "b": Score("b", 400, 0.03, 0.6, "bt-b")}
    )
    window = (ANCHOR, END)
    out = final_selection(
        stub, ["a", "b"], window=window, n_min=300,  # type: ignore[arg-type]
        oos_verdict=OOS_MARGIN_CERTIFIED_DEVELOPMENT_ONLY, last_fold=FOLDS[-1],
    )  # fmt: skip
    assert out["status"] == "CANDIDATE_PENDING_GOVERNANCE"
    assert (out["selected"], out["strategy_hash"], out["params"]) == ("b", "hash-b", {"tp_r": 1})
    assert {c[1:] for c in stub.calls} == {("final", -1, ANCHOR, END)}
    none = final_selection(
        _StubEvaluator({"a": Score("a", 10, 0.5, 0.9)}), ["a"], window=window, n_min=300,  # type: ignore[arg-type]
        oos_verdict=OOS_MARGIN_CERTIFIED_DEVELOPMENT_ONLY, last_fold=FOLDS[-1],
    )  # fmt: skip
    assert none["status"] == "NO_ELIGIBLE_CONFIGURATION"


def test_validity_checks_read_the_runs_manifests(done: tuple[Path, dict[str, Any]]) -> None:
    """The checks are recomputed from what the runs recorded, not from the plan."""
    src, report = done
    fold = report["folds"][0]

    @dataclasses.dataclass
    class FakeOut:
        manifest: dict[str, Any]

    def manifest(run_id: str) -> dict[str, Any]:
        return dict(json.loads((src / "runs" / run_id / "manifest.json").read_text("utf-8")))

    by_key: dict[tuple[str, int, str], Any] = {}
    scores = []
    for s in fold["scores"]:
        by_key[("train", 0, s["config_id"])] = FakeOut(manifest(s["run_id"]))
        scores.append(Score(s["config_id"], 10, 0.0, 0.5, s["run_id"]))
    by_key[("test", 0, fold["selected"])] = FakeOut(manifest(fold["oos_run_id"]))
    from tsclaude.research.walkforward import FoldResult

    result = WalkForwardResult(list(GRID), [FoldResult(FOLDS[0], scores, fold["selected"], [])])
    evaluator = type("E", (), {"by_key": by_key})()
    kw = {"config_hash": TEST_CONFIG.config_hash, "prereg_id": None}
    checks, failures = validity_checks(result, evaluator, FOLDS, code_commit="test", **kw)  # type: ignore[arg-type]
    assert all(checks.values())
    assert failures == []
    checks, failures = validity_checks(result, evaluator, FOLDS, code_commit="other", **kw)  # type: ignore[arg-type]
    assert checks["same_code"] is False
    by_key[("test", 0, fold["selected"])].manifest["range"]["from_ms"] -= DAY  # overlaps train
    checks, failures = validity_checks(result, evaluator, FOLDS, code_commit="test", **kw)  # type: ignore[arg-type]
    assert checks["runs_match_their_windows"] is False
    assert any("runs_match_their_windows" in f for f in failures)


def test_a_resume_against_another_trial_registry_is_refused(
    done: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    """Final review M1: runs whose trials this registry does not hold would under-count."""
    src, _ = done
    data = _copy(src, tmp_path / "d")
    (data / "state.db").unlink()
    with pytest.raises(ResumeRefused, match="not in this trial registry"):
        _walk(_env(data), data)


def test_a_lower_coverage_floor_needs_a_preregistration_declaring_it(
    done: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    """Final review M2: the floor cannot be lowered after a result is seen."""
    src, _ = done
    data = _copy(src, tmp_path / "d")
    env = _env(data)
    rows = env.trials.list("bracket_reference")
    with pytest.raises(WalkForwardError, match="needs a committed preregistration"):
        _walk(env, data, min_oos_coverage=0.5)
    assert env.trials.list("bracket_reference") == rows


def test_the_ledger_rescore_count_is_validated_and_never_lowered(
    done: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    """Final review M3."""
    src, first = done
    data = _copy(src, tmp_path / "d")
    ledger = data / "wf" / first["plan"]["plan_id"] / "progress.jsonl"
    lines = ledger.read_text(encoding="utf-8").splitlines()
    lines[1] = '{"kind":"meta","rescore_n_trials":1}'
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = _walk(_env(data), data)
    assert report["rescore_n_trials_stored"] == 1
    assert report["rescore_n_trials"] == first["rescore_n_trials"] == 3
    assert _summary(report) == _summary(first)
    lines[1] = '{"kind":"meta"}'
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ResumeRefused, match="rescore trial count"):
        _walk(_env(data), data)


def _git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.invalid", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True,
    )  # fmt: skip


#: a preregistration needs a bounded holding period: the same grid with a 15m time exit
PREREG_GRID = {config_id_of(p): p for p in ({**g, "time_exit_bars": 48} for g in GRID.values())}


def _committed_prereg(repo: Path, **budget: Any) -> str:
    """A frozen, committed search preregistration matching this module's walk-forward."""
    import yaml

    from tsclaude.research import preregistration as pr

    doc = pr.load(Path(__file__).resolve().parents[3] / "preregistrations" / "TEMPLATE.yaml")
    doc.update(
        prereg_id="H-RUN", search_space=list(PREREG_GRID.values()),
        holdout_procedure="two-stage-selection-record",
        risk_compatibility="live-floor-kept-geometries-dropped",
    )  # fmt: skip
    doc["universe"]["symbols"] = ["BTCUSDT"]
    doc["walk_forward"].update(anchor="2024-01-01", end="2024-03-01", initial_train_months=1,
                               test_months=1, embargo_minutes=1440)  # fmt: skip
    doc["trial_budget"].update({"counting": "configurations", **budget})
    (repo / "preregistrations").mkdir(parents=True)
    path = repo / "preregistrations" / "H-RUN.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    pr.freeze(
        path,
        now_ms=0,
        code={"commit": "test", "dirty": False},
        backtest_config_hash=TEST_CONFIG.config_hash,
    )
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "prereg")
    return "H-RUN"


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_walk_forward_under_a_preregistration_must_run_exactly_its_plan(tmp_path: Path) -> None:
    """Review M2 (overnight): the run-side conformance path, refused before anything runs."""
    data = tmp_path / "d"
    write_lake(data, [(Market.SPOT, "BTCUSDT", synthetic_rows(DATA_START, END))])
    repo = tmp_path / "repo"
    pid = _committed_prereg(repo)
    env = _env(data, repo_root=repo)
    with pytest.raises(WalkForwardError, match=r"not the preregistered walk-forward.*n_min"):
        _walk(env, data, grid=PREREG_GRID, prereg_id=pid, final_selection_enabled=True)
    with pytest.raises(WalkForwardError, match="final_selection"):
        _walk(env, data, grid=PREREG_GRID, prereg_id=pid, n_min=300)
    with pytest.raises(WalkForwardError, match="grid"):
        _walk(env, data, prereg_id=pid, n_min=300, final_selection_enabled=True)  # other grid
    assert not env.runs_dir.exists()
    assert env.trials.n_trials("bracket_reference") == 0


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_registry_rows_budget_in_the_preregistration_caps_the_run(tmp_path: Path) -> None:
    data = tmp_path / "d"
    write_lake(data, [(Market.SPOT, "BTCUSDT", synthetic_rows(DATA_START, END))])
    repo = tmp_path / "repo"
    pid = _committed_prereg(repo, counting="registry_rows", max_registry_trials=2)
    env = _env(data, repo_root=repo)
    with pytest.raises(WalkForwardError, match="above --max-new-trials 2"):
        _walk(env, data, grid=PREREG_GRID, prereg_id=pid, n_min=300, final_selection_enabled=True)
    assert not env.runs_dir.exists()
