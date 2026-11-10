"""Power planning and reading (mathematical audit F3, F4): deterministic, P&L-free, never part of
selection or certification."""

from __future__ import annotations

import itertools
import json
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tsclaude.research import power_plan as pp
from tsclaude.research.stats.power import n_min_one_sided


def ms(y: int, m: int) -> int:
    return int(datetime(y, m, 1, tzinfo=UTC).timestamp() * 1000)


def test_planned_break_even_is_adr_0012s_bracket_formula() -> None:
    assert pp.planned_p_star(2.0, 0.2) == pytest.approx(0.40)
    assert pp.planned_p_star(2.5, 0.2) == pytest.approx(1.2 / 3.5)
    assert pp.planned_p_star(3.0, 0.2) == pytest.approx(0.30)
    with pytest.raises(ValueError, match="tp_r must be"):
        pp.planned_p_star(0.0, 0.2)


def test_min_detectable_margin_inverts_n_min() -> None:
    """With exactly the audit's n_min (1,804 trades at p* 0.40, 199 trials) the detectable margin
    is 5 points; one trade fewer is not enough for it."""
    alpha = 0.05 / 199
    assert n_min_one_sided(0.40, 0.45, alpha) == 1804
    assert pp.min_detectable_margin(1804, 0.40, alpha) == pytest.approx(0.05)
    assert pp.min_detectable_margin(1803, 0.40, alpha) > 0.05
    m = [pp.min_detectable_margin(n, 0.40, alpha) for n in (300, 600, 1200, 2400)]
    assert all(a > b for a, b in itertools.pairwise(m))  # more trades, smaller margin
    assert pp.min_detectable_margin(0, 0.40, alpha) is None
    assert pp.min_detectable_margin(452, 0.40, alpha) == pytest.approx(0.10)  # audit §3.5


def test_registry_rows_match_the_h1_plan() -> None:
    assert pp.registry_rows(24, 7) == 199
    assert pp.registry_rows(24, 7, final_selection=False) == 175


def _feas(
    candidates_per_symbol: dict[str, int], months: dict[str, int] | None = None
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kind": "feasibility_count",
        "pnl_free": True,
        "content_hash": "h",
        "range": {"from_ms": ms(2022, 3), "to_ms": ms(2025, 1)},
        "symbols": {
            s: {"total": {"cumulative": [{"stage": "x", "bars": n + 5}, {"stage": "y", "bars": n}]}}
            for s, n in candidates_per_symbol.items()
        },
    }
    if months is not None:
        out["diagnostics"] = {"candidates_by_utc_month": months}
    return out


#: seven windows covering 17 of the range's 34 months (about half by time) - for the
#: time-share fallback
HALF = [(ms(2023, 1), ms(2023, 4)), (ms(2023, 4), ms(2023, 7)), (ms(2023, 7), ms(2023, 10)),
        (ms(2023, 10), ms(2024, 1)), (ms(2024, 1), ms(2024, 3)), (ms(2024, 3), ms(2024, 5)),
        (ms(2024, 5), ms(2024, 6))]  # fmt: skip


def test_plan_from_feasibility_sums_the_last_stage_and_bounds_the_reading() -> None:
    feas = _feas({"spot:BTCUSDT": 400, "spot:ETHUSDT": 600})
    out = pp.plan_from_feasibility(
        feas, test_windows=HALF, configurations=24, geometries=[("2R", 2.0), ("3R", 3.0)],
        cost_r=0.2,
    )  # fmt: skip
    assert out["candidates_in_range"] == 1000
    share = sum(b - a for a, b in HALF) / (ms(2025, 1) - ms(2022, 3))
    assert out["test_window_share_of_range"] == pytest.approx(share, abs=1e-4)
    assert out["oos_candidates"] == pytest.approx(1000 * share, abs=0.05)
    assert "assumes candidates are spread evenly" in out["oos_candidates_basis"]
    assert out["trials"]["new_registry_rows_max"] == 199
    assert out["trials"]["n_trials_planned"] == 199
    assert out["trials"]["alpha_adj"] == pytest.approx(0.05 / 199)
    two, three = out["geometries"]
    assert two["p_star_planned"] == pytest.approx(0.4)
    full = two["by_n_eff"][0]
    n = out["oos_candidates"]
    assert full["n_eff"] == pytest.approx(n, abs=0.05)
    assert full["below_certification_floor"] is False
    assert full["min_detectable_margin"] == pp.min_detectable_margin(n, 0.4, 0.05 / 199)
    assert full["edge_r_equivalent"] == pytest.approx(full["min_detectable_margin"] * 3, abs=1e-3)
    half = two["by_n_eff"][2]
    assert half["n_eff"] == pytest.approx(n / 2, abs=0.05)
    assert half["below_certification_floor"] is True
    assert three["p_star_planned"] == pytest.approx(0.3)
    assert out["pnl_free"] is True
    assert "never used by selection" in out["note"]
    with pytest.raises(ValueError, match="not a P&L-free"):
        pp.plan_from_feasibility({"kind": "run"}, test_windows=HALF, configurations=1,
                                 geometries=[("2R", 2.0)], cost_r=0.2)  # fmt: skip
    with pytest.raises(ValueError, match="no test windows"):
        pp.plan_from_feasibility(feas, test_windows=[], configurations=1,
                                 geometries=[("2R", 2.0)], cost_r=0.2)  # fmt: skip


def test_month_counts_place_candidates_inside_or_outside_the_test_windows() -> None:
    """Review M2: with candidates concentrated in the test windows the time share would
    understate the out-of-sample pool; the month counts give it exactly."""
    windows = [(ms(2023, 2), ms(2023, 5)), (ms(2023, 5), ms(2023, 8))]
    inside = _feas({"spot:BTCUSDT": 600}, months={"2023-02": 200, "2023-05": 300, "2023-07": 100})
    outside = _feas({"spot:BTCUSDT": 600}, months={"2022-03": 400, "2024-12": 200})
    mixed = _feas({"spot:BTCUSDT": 600}, months={"2023-01": 300, "2023-04": 300})
    plan = {"configurations": 24, "geometries": [("2R", 2.0)], "cost_r": 0.2}
    a = pp.plan_from_feasibility(inside, test_windows=windows, **plan)
    assert a["oos_candidates"] == 600
    assert a["test_window_share_of_range"] < 0.2  # the time share would have said ~107
    assert a["oos_candidates_basis"].startswith("months inside the test windows")
    assert pp.plan_from_feasibility(outside, test_windows=windows, **plan)["oos_candidates"] == 0
    assert pp.plan_from_feasibility(mixed, test_windows=windows, **plan)["oos_candidates"] == 300
    # a window starting mid-month takes its part of that month pro rata (16 of 31 days)
    mid = [(ms(2023, 1) + 15 * 86_400_000, ms(2023, 2))]
    got = pp.plan_from_feasibility(mixed, test_windows=mid, **plan)["oos_candidates"]
    assert got == pytest.approx(300 * 16 / 31, abs=0.1)
    # a month only partly inside the counted range contributes only its counted part: the
    # count starts on 16 March, so a window over 1-16 March holds none of March's candidates
    late = _feas({"spot:BTCUSDT": 16}, months={"2024-03": 16})
    late["range"]["from_ms"] = ms(2024, 3) + 15 * 86_400_000
    early_march = [(ms(2024, 3), ms(2024, 3) + 15 * 86_400_000)]
    assert pp.plan_from_feasibility(late, test_windows=early_march, **plan)["oos_candidates"] == 0
    rest = [(ms(2024, 3) + 15 * 86_400_000, ms(2024, 4))]
    assert pp.plan_from_feasibility(late, test_windows=rest, **plan)["oos_candidates"] == 16


def test_prior_family_rows_lower_the_planned_level() -> None:
    """Review M2: certification divides alpha by the family's whole registry count."""
    feas = _feas({"spot:BTCUSDT": 2000})
    out = pp.plan_from_feasibility(feas, test_windows=HALF, configurations=24,
                                   geometries=[("2R", 2.0)], cost_r=0.2,
                                   prior_family_rows=100)  # fmt: skip
    assert out["trials"]["n_trials_planned"] == 299
    assert out["trials"]["alpha_adj"] == pytest.approx(0.05 / 299)
    clean = pp.plan_from_feasibility(feas, test_windows=HALF, configurations=24,
                                     geometries=[("2R", 2.0)], cost_r=0.2)  # fmt: skip
    m_prior = out["geometries"][0]["by_n_eff"][0]["min_detectable_margin"]
    m_clean = clean["geometries"][0]["by_n_eff"][0]["min_detectable_margin"]
    assert m_prior > m_clean
    with pytest.raises(ValueError, match="prior_family_rows"):
        pp.plan_from_feasibility(feas, test_windows=HALF, configurations=24,
                                 geometries=[("2R", 2.0)], cost_r=0.2,
                                 prior_family_rows=-1)  # fmt: skip


def test_oos_share_of_the_planned_folds() -> None:
    from tsclaude.research.walkforward import plan_folds

    folds = plan_folds(ms(2022, 2), ms(2025, 1), initial_train_months=12, test_months=3,
                       embargo_ms=1440 * 60_000)  # fmt: skip
    share = pp.oos_share_of(folds, ms(2022, 3), ms(2025, 1))
    covered = sum(f.test_to_ms - f.test_from_ms for f in folds)
    assert share == pytest.approx(covered / (ms(2025, 1) - ms(2022, 3)))
    assert 0.5 < share < 0.7  # 7 test windows of about 3 months in 34 months
    assert pp.test_windows_of(folds) == [(f.test_from_ms, f.test_to_ms) for f in folds]


def test_oos_share_clips_windows_to_the_range() -> None:
    """Review M1: a window crossing either edge of the range counts only its inside part."""
    w = [types.SimpleNamespace(test_from_ms=ms(2021, 12), test_to_ms=ms(2022, 6)),
         types.SimpleNamespace(test_from_ms=ms(2024, 10), test_to_ms=ms(2025, 4))]  # fmt: skip
    lo, hi = ms(2022, 3), ms(2025, 1)
    inside = (ms(2022, 6) - lo) + (hi - ms(2024, 10))
    assert pp.oos_share_of(w, lo, hi) == pytest.approx(inside / (hi - lo))
    out = [types.SimpleNamespace(test_from_ms=ms(2020, 1), test_to_ms=ms(2021, 1))]
    assert pp.oos_share_of(out, lo, hi) == 0.0
    with pytest.raises(ValueError, match="empty range"):
        pp.oos_share_of(w, hi, lo)


def test_read_power_uses_n_eff_and_the_runs_trial_count() -> None:
    oos = {"p_star": 0.40, "n_trials": 199, "oos_trades": 900, "n_eff": 452}
    r = pp.read_power(oos, alpha=0.05)
    assert r is not None
    assert r["n_basis"] == "n_eff"
    assert r["min_detectable_margin"] == pytest.approx(0.10)
    assert "does not rule out" in r["reading"]
    assert r["below_certification_floor"] is False
    no_eff = pp.read_power({**oos, "n_eff": None}, alpha=0.05)
    assert no_eff is not None
    assert no_eff["n_basis"] == "oos_trades"
    small = pp.read_power({**oos, "n_eff": 5}, alpha=0.05)
    assert small is not None
    assert small["below_certification_floor"] is True  # review minor 5
    for bad in ({}, {"p_star": None, "n_trials": 1, "oos_trades": 5}, {**oos, "oos_trades": 0,
                                                                       "n_eff": None}):  # fmt: skip
        assert pp.read_power(bad, alpha=0.05) is None


def test_the_cli_plans_from_a_feasibility_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tsclaude.cli import main

    path = tmp_path / "feasibility.json"
    path.write_text(json.dumps(_feas({"spot:BTCUSDT": 800})), encoding="utf-8")
    assert main(["backtest", "power-plan", "--feasibility", str(path), "--anchor", "2022-02-01",
                 "--tp-r", "2.5,3,3.5"]) == 0  # fmt: skip
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["trials"]["folds"] == 7
    assert out["trials"]["new_registry_rows_max"] == 199
    assert out["trials"]["prior_family_rows"] == 0
    assert [g["tp_r"] for g in out["geometries"]] == [2.5, 3.0, 3.5]
    assert len(out["folds"]) == 7
    assert main(["backtest", "power-plan", "--feasibility", str(tmp_path / "none.json")]) == 2
    assert json.loads(capsys.readouterr().out)["refused"] == "power_plan"
    out_file = tmp_path / "plan.json"  # --out writes the same JSON, UTF-8 without a BOM
    assert main(["backtest", "power-plan", "--feasibility", str(path), "--out", str(out_file)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert out_file.read_bytes()[:1] == b"{"
    assert b"\r" not in out_file.read_bytes()  # LF on every platform, Windows included
    assert json.loads(out_file.read_text(encoding="utf-8")) == printed
    unwritable = main(
        ["backtest", "power-plan", "--feasibility", str(path), "--out", str(tmp_path)]
    )
    assert unwritable == 2  # a directory: refused, never a half-written plan
    assert json.loads(capsys.readouterr().out)["refused"] == "power_plan"


def test_walk_forward_exit_mix_and_power_reading() -> None:
    from tsclaude.research import walkforward as wf
    from tsclaude.research.runner import BacktestConfig

    def run(reasons: dict[str, int]) -> Any:
        return types.SimpleNamespace(metrics={"trades": {"exit_reasons": reasons}})

    ev = types.SimpleNamespace(by_key={
        ("train", 0, "a"): run({"stop_loss": 3, "take_profit": 1, "time_exit": 4}),
        ("train", 1, "a"): run({"stop_loss": 1, "take_profit": 1, "time_exit": 0}),
        ("train", 0, "b"): run({}),
        ("test", 0, "a"): run({"time_exit": 2}),
    })  # fmt: skip
    mix = wf._exit_mix(ev)  # type: ignore[arg-type]
    a = mix["train"]["a"]
    assert a["runs"] == 2
    assert a["trades"] == 10
    assert a["exit_reasons"] == {"stop_loss": 4, "take_profit": 2, "time_exit": 4}
    assert a["time_exit_share"] == pytest.approx(0.4)
    assert mix["train"]["b"]["time_exit_share"] is None
    assert mix["test"]["a"]["time_exit_share"] == 1.0
    cfg = BacktestConfig()
    reading = wf._power_reading({"p_star": 0.4, "n_trials": 199, "n_eff": 452}, cfg)
    assert reading is not None
    assert reading["min_detectable_margin"] == pytest.approx(0.10)
    assert wf._power_reading({"verdict": "INSUFFICIENT_OOS_EVIDENCE"}, cfg) is None


def test_walk_forward_diagnostics_never_lose_the_report() -> None:
    """Review M4: both run after the trials are recorded; any failure is an error entry."""
    from tsclaude.research import walkforward as wf
    from tsclaude.research.runner import BacktestConfig

    bad_shape = types.SimpleNamespace(by_key={
        ("train", 0, "a"): types.SimpleNamespace(metrics={"trades": {"exit_reasons": ["x"]}}),
    })  # fmt: skip
    assert "error" in wf._exit_mix(bad_shape)  # type: ignore[arg-type]
    no_metrics = types.SimpleNamespace(by_key={("train", 0, "a"): types.SimpleNamespace()})
    assert "error" in wf._exit_mix(no_metrics)  # type: ignore[arg-type]
    huge = wf._power_reading({"p_star": 0.4, "n_trials": 10**400, "n_eff": 452}, BacktestConfig())
    assert huge is not None
    assert huge["error"].startswith("OverflowError")


def test_training_ceilings_per_fold_against_n_min() -> None:
    """Independent H1 feasibility review, finding 4: `select` needs n >= n_min trades in every
    training window, and the plan did not check it. Anchored windows grow fold by fold."""
    months = {"2022-03": 100, "2022-06": 150, "2023-01": 60, "2023-04": 40}
    feas = _feas({"spot:BTCUSDT": 350}, months=months)
    windows = [(ms(2022, 3), ms(2023, 1)), (ms(2022, 3), ms(2023, 4)), (ms(2022, 3), ms(2023, 7))]
    out = pp.training_ceilings(feas, windows, n_min=300)
    got = [(f["candidates_ceiling"], f["counted_share"], f["reaches_n_min"]) for f in out["folds"]]
    assert got == [(250.0, 1.0, False), (310.0, 1.0, True), (350.0, 1.0, True)]
    assert (out["folds_reaching_n_min"], out["folds_below_n_min"]) == (2, 1)
    assert out["selection_possible_in_every_fold"] is False
    with pytest.raises(ValueError, match="n_min"):
        pp.training_ceilings(feas, windows, n_min=0)


def test_a_window_the_count_does_not_cover_is_decided_only_when_it_must_be() -> None:
    """The count starts 2022-03-01 while the anchor is 2022-02-01 (review finding 5): February is
    not counted. Below n_min is stated only if February at the busiest counted month's rate
    could not close the gap; otherwise the fold is undetermined (None), never a guess."""
    feas = _feas({"spot:BTCUSDT": 200}, months={"2022-03": 31, "2022-04": 169})
    low = pp.training_ceilings(feas, [(ms(2022, 2), ms(2022, 4))], n_min=300)["folds"][0]
    assert low["candidates_ceiling"] == 31.0
    assert low["counted_share"] == pytest.approx(31 / 59, abs=1e-4)  # March of Feb+March
    assert low["reaches_n_min"] is False  # 31 + 28 days at April's 169/30 per day < 300
    near = pp.training_ceilings(feas, [(ms(2022, 2), ms(2022, 5))], n_min=300)["folds"][0]
    assert near["candidates_ceiling"] == 200.0
    assert near["reaches_n_min"] is None  # 200 + 28 x 169/30 = 357.7 could reach 300
    assert (
        pp.training_ceilings(feas, [(ms(2022, 2), ms(2022, 5))], n_min=300)[
            "selection_possible_in_every_fold"
        ]
        is None
    )
    old = _feas({"spot:BTCUSDT": 200})  # a count without month totals
    assert (
        pp.training_ceilings(old, [(ms(2022, 3), ms(2022, 5))], n_min=300)["folds"][0][
            "reaches_n_min"
        ]
        is None
    )


def test_the_plan_and_cli_carry_the_training_ceilings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tsclaude.cli import main

    feas = _feas({"spot:BTCUSDT": 120}, months={"2022-06": 60, "2023-06": 60})
    path = tmp_path / "f.json"
    path.write_text(json.dumps(feas), encoding="utf-8")
    assert main(["backtest", "power-plan", "--feasibility", str(path), "--anchor", "2022-02-01",
                 "--n-min", "50"]) == 0  # fmt: skip
    out = json.loads(capsys.readouterr().out)
    tw = out["training_windows"]
    assert tw["n_min"] == 50
    assert len(tw["folds"]) == out["trials"]["folds"] == 7
    assert [f["train_from_ms"] for f in tw["folds"]] == [ms(2022, 2)] * 7  # anchored
    assert tw["folds"][0]["candidates_ceiling"] == 60.0  # 2022-02 .. 2023-02 holds June 2022
    assert tw["folds"][0]["counted_share"] < 1.0  # February 2022 is outside the count
    assert out["count_range"] == {"from_ms": ms(2022, 3), "to_ms": ms(2025, 1)}
    plan = pp.plan_from_feasibility(feas, test_windows=HALF, configurations=24,
                                    geometries=[("2R", 2.0)], cost_r=0.2)  # fmt: skip
    assert plan["training_windows"] is None  # only when the caller passes training windows
