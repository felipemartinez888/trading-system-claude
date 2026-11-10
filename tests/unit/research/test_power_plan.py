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


def _feas(candidates_per_symbol: dict[str, int]) -> dict[str, Any]:
    return {
        "kind": "feasibility_count",
        "pnl_free": True,
        "content_hash": "h",
        "range": {"from_ms": ms(2022, 3), "to_ms": ms(2025, 1)},
        "symbols": {
            s: {"total": {"cumulative": [{"stage": "x", "bars": n + 5}, {"stage": "y", "bars": n}]}}
            for s, n in candidates_per_symbol.items()
        },
    }


def test_plan_from_feasibility_sums_the_last_stage_and_bounds_the_reading() -> None:
    feas = _feas({"spot:BTCUSDT": 400, "spot:ETHUSDT": 600})
    out = pp.plan_from_feasibility(
        feas, oos_share=0.5, configurations=24, folds=7, geometries=[("2R", 2.0), ("3R", 3.0)],
        cost_r=0.2,
    )  # fmt: skip
    assert out["candidates_in_range"] == 1000
    assert out["oos_candidates_upper_bound"] == 500
    assert out["trials"]["registry_rows_upper_bound"] == 199
    assert out["trials"]["alpha_adj"] == pytest.approx(0.05 / 199)
    two, three = out["geometries"]
    assert two["p_star_planned"] == pytest.approx(0.4)
    full = two["by_n_eff"][0]
    assert full["n_eff"] == 500
    assert full["below_certification_floor"] is False
    assert full["min_detectable_margin"] == pp.min_detectable_margin(500, 0.4, 0.05 / 199)
    assert full["edge_r_equivalent"] == pytest.approx(full["min_detectable_margin"] * 3, abs=1e-3)
    half = two["by_n_eff"][2]
    assert half["n_eff"] == 250
    assert half["below_certification_floor"] is True
    assert three["p_star_planned"] == pytest.approx(0.3)
    assert out["pnl_free"] is True
    assert "never used by selection" in out["note"]
    with pytest.raises(ValueError, match="not a P&L-free"):
        pp.plan_from_feasibility({"kind": "run"}, oos_share=0.5, configurations=1, folds=1,
                                 geometries=[("2R", 2.0)], cost_r=0.2)  # fmt: skip


def test_oos_share_of_the_planned_folds() -> None:
    from tsclaude.research.walkforward import plan_folds

    folds = plan_folds(ms(2022, 2), ms(2025, 1), initial_train_months=12, test_months=3,
                       embargo_ms=1440 * 60_000)  # fmt: skip
    share = pp.oos_share_of(folds, ms(2022, 3), ms(2025, 1))
    covered = sum(f.test_to_ms - f.test_from_ms for f in folds)
    assert share == pytest.approx(covered / (ms(2025, 1) - ms(2022, 3)))
    assert 0.5 < share < 0.7  # 7 test windows of about 3 months in 34 months


def test_read_power_uses_n_eff_and_the_runs_trial_count() -> None:
    oos = {"p_star": 0.40, "n_trials": 199, "oos_trades": 900, "n_eff": 452}
    r = pp.read_power(oos, alpha=0.05)
    assert r is not None
    assert r["n_basis"] == "n_eff"
    assert r["min_detectable_margin"] == pytest.approx(0.10)
    assert "does not rule out" in r["reading"]
    no_eff = pp.read_power({**oos, "n_eff": None}, alpha=0.05)
    assert no_eff is not None
    assert no_eff["n_basis"] == "oos_trades"
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
    assert out["trials"]["registry_rows_upper_bound"] == 199
    assert [g["tp_r"] for g in out["geometries"]] == [2.5, 3.0, 3.5]
    assert len(out["folds"]) == 7
    assert main(["backtest", "power-plan", "--feasibility", str(tmp_path / "none.json")]) == 2
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
