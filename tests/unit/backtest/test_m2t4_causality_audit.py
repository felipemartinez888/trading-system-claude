"""Causality-audit attack tests for M2-T4 (added by the Causality Auditor; expected to FAIL on
task/m2-t4 at 5daa769 until the findings are fixed). Synthetic lakes only, no network.

A1  trial registry undercount via a free-form hypothesis id (CLI ``--hypothesis``)
A2  seed shopping: the seed changes certification/control outcomes but not n_trials
A3  persisted reports keep a stale n_trials after later trials of the same hypothesis
A4  USD-M runs never apply funding and do not say so
A5  cost model slippage parameters are not in the trial key
A6  a data gap inside the warm-up is invisible to the integrity checks
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from pathlib import Path
from typing import Any

from bt_helpers import (
    MIN,
    TEST_CONFIG,
    fast_strategy,
    make_env,
    ms,
    request,
    synthetic_rows,
    write_lake,
)

from tsclaude.contracts.market import Market
from tsclaude.research.parquet import read_table
from tsclaude.research.runner import BacktestConfig, BacktestRequest, load_run, run_backtest
from tsclaude.sim.costs import CostModel, FixedSlippage

VAL_START, VAL_END = ms(2025, 3, 1), ms(2025, 3, 15)
VFROM, VTO = ms(2025, 3, 3), ms(2025, 3, 15)


def _val_lake(path: Path, market: Market = Market.SPOT, **kw: Any) -> Path:
    write_lake(path, [(market, "BTCUSDT", synthetic_rows(VAL_START, VAL_END, **kw))])
    return path


# --------------------------------------------------------------------------- A1
def test_a1_param_search_under_fresh_hypothesis_ids_still_counts_trials(tmp_path: Path) -> None:
    """Five parameter sets of the same strategy evaluated on the validation segment, each under
    its own unvalidated ``hypothesis_id`` (CLI ``--hypothesis``). Every run reports n_trials = 1,
    so certify() applies no multiple-testing correction to a 5-way search."""
    env = make_env(_val_lake(tmp_path / "lake"))
    seen = []
    for i, tp in enumerate((0.6, 0.8, 1.0, 1.2, 1.4)):
        out = run_backtest(
            request(VFROM, VTO, strategy=fast_strategy(tp_r=tp), hypothesis_id=f"H-search-{i}"),
            env,
            TEST_CONFIG,
        )
        seen.append(out.metrics["n_trials"])
    # configurations of one strategy evaluated on one segment must all be counted
    assert seen[-1] >= 5, f"n_trials reported per run: {seen}"


# --------------------------------------------------------------------------- A2
def test_a2_seed_changes_outcome_but_not_trial_count(tmp_path: Path) -> None:
    """The seed drives the bootstrap bounds and the random-entry control (which caps the grade),
    yet it is excluded from the trial key: one can re-roll seeds until a borderline run passes."""
    env = make_env(_val_lake(tmp_path / "lake"))
    outs = [run_backtest(request(VFROM, VTO, seed=s), env, TEST_CONFIG) for s in range(4)]

    def decision_view(m: dict[str, Any]) -> tuple[Any, ...]:
        c = m["certification"] or {}
        rc = m["random_control"]
        return (
            m["evidence_grade"],
            c.get("margin_lcb"),
            c.get("expectancy_lcb"),
            c.get("certified"),
            rc["edge_lcb_r"],
            rc["beats_control"],
        )

    views = [decision_view(o.metrics) for o in outs]
    trials = [o.metrics["n_trials"] for o in outs]
    if len(set(views)) > 1:  # the seed changed decision-relevant numbers ...
        # ... so each seed is a separate draw and must be counted (or the seed must be pinned)
        assert trials[-1] > trials[0], f"views differ across seeds {views}; n_trials {trials}"


# --------------------------------------------------------------------------- A3
def test_a3_stored_reports_do_not_show_stale_trial_counts(tmp_path: Path) -> None:
    """The first configuration of a search is persisted with n_trials = 1 forever; `list_runs`
    / `show` / the API keep presenting it with that count after more trials were recorded."""
    env = make_env(_val_lake(tmp_path / "lake"))
    first = run_backtest(request(VFROM, VTO), env, TEST_CONFIG)
    for tp in (0.7, 0.9, 1.3):
        run_backtest(request(VFROM, VTO, strategy=fast_strategy(tp_r=tp)), env, TEST_CONFIG)
    current = env.trials.n_trials("bracket_reference")
    assert current == 4
    shown = load_run(env.runs_dir, first.run_id)
    assert shown is not None
    s = shown["summary"]
    assert s["n_trials"] >= current or s.get("n_trials_stale") is True, s


# --------------------------------------------------------------------------- A4
def test_a4_usdm_run_applies_or_flags_funding(tmp_path: Path) -> None:
    """USD-M is an accepted market, but the runner never reads funding nor calls
    `Simulator.apply_funding_rate`; every trade books funding = 0 and nothing says so."""
    d = _val_lake(tmp_path / "lake", market=Market.USDM)
    cfg = BacktestConfig.from_mapping(
        {
            "n_resamples": 2000,
            "instruments": {
                "usdm": {"default": {"tick_size": "0.01", "lot_size": "0.001", "min_notional": "5"}}
            },
        }
    )
    req = BacktestRequest(
        strategy=fast_strategy(), symbols=[(Market.USDM, "BTCUSDT")], from_ms=VFROM, to_ms=VTO
    )
    out = run_backtest(req, make_env(d), cfg)
    _, rows = read_table(out.run_dir / "trades.parquet")
    base = [r for r in rows if r["book"] == "baseline"]
    spans_funding = [
        r
        for r in base
        if (r["exit_bar_open_ms"] // (8 * 3_600_000)) > (r["entry_bar_open_ms"] // (8 * 3_600_000))
    ]
    assert spans_funding, "fixture must hold positions across 8h funding timestamps"
    funded = any(Decimal(r["funding"]) != 0 for r in spans_funding)
    flagged = any("funding" in p for p in out.metrics["integrity"]["problems"]) or any(
        "funding" in r for r in out.metrics["grade_reasons"]
    )
    assert funded or flagged, "USD-M costs: funding neither applied nor flagged"


# --------------------------------------------------------------------------- A5
def test_a5_slippage_parameters_change_results_but_not_trial_key(tmp_path: Path) -> None:
    """`CostModel.describe()` records only the slippage *class name*; the trial key uses it, so a
    zero-slippage model with the default version string is the *same trial* as the pessimistic
    default while producing different results. (The manifest hash differs only because the
    planning-cost basis string happens to embed the zero-notional slippage estimate.)"""
    d = _val_lake(tmp_path / "lake")
    env_a = make_env(d)
    a = run_backtest(request(VFROM, VTO), env_a, TEST_CONFIG)
    env_b = dataclasses.replace(
        make_env(d), cost_model=CostModel(slippage=FixedSlippage(Decimal(0), Decimal(0)))
    )
    b = run_backtest(request(VFROM, VTO), env_b, TEST_CONFIG)
    assert a.metrics["trades"]["total_slippage"] != b.metrics["trades"]["total_slippage"]
    assert a.manifest["trials"]["trial_key"] != b.manifest["trials"]["trial_key"], (
        "different cost inputs, different evidence, same trial (n_trials not incremented: "
        f"{a.metrics['n_trials']} -> {b.metrics['n_trials']})"
    )


# --------------------------------------------------------------------------- A6
def test_a6_gap_in_warmup_is_reported(tmp_path: Path) -> None:
    """Missing minutes are counted only inside [from, to). A gap in the warm-up silently drops
    strategy buckets, so the first decisions use features computed over non-contiguous bars."""
    gap = [VFROM - 6 * 3_600_000 + i * MIN for i in range(120)]  # 2h gap, 6h before `from`
    d = _val_lake(tmp_path / "lake", skip=gap)
    out = run_backtest(request(VFROM, VTO), make_env(d), TEST_CONFIG)
    integ = out.metrics["integrity"]
    # whole missing buckets are not even counted as incomplete (no minute was ever pushed)
    assert integ["problems"] or integ.get("missing_minutes_in_warmup", 0) > 0, integ


# =========================================================================== guard tests
# The tests below PASS on 5daa769. They were added because the corresponding mutations survived
# the branch's own suite (killed only by the golden hashes, or not at all).


def test_g1_max_loss_is_measured_on_development_not_on_the_evaluated_range(
    tmp_path: Path,
) -> None:
    """Kills: measure_max_loss reading the run's own range instead of the development segment."""
    dev_lo, dev_hi = ms(2024, 12, 1), ms(2024, 12, 15)
    jumps = {dev_lo + k * 2 * 3_600_000: 0.985 for k in range(14 * 12)}
    d = tmp_path / "lake"
    write_lake(
        d,
        [
            (Market.SPOT, "BTCUSDT", synthetic_rows(dev_lo, dev_hi, jumps=jumps)),
            (Market.SPOT, "BTCUSDT", synthetic_rows(VAL_START, VAL_END, seed=3)),
        ],
    )
    out = run_backtest(request(VFROM, VTO), make_env(d), TEST_CONFIG)
    meas = out.metrics["certification_inputs"]["max_loss_measurement"]
    assert out.manifest["segment"] == "validation"
    assert meas["measured"] is True
    assert meas["dev_gap_fill_stops"] >= 1
    assert meas["worst_stop_loss_r"] < -meas["planned_stop_loss_r"]
    assert set(out.manifest["datasets"]["max_loss"]).isdisjoint(out.manifest["datasets"]["main"])


def test_g2_no_trade_from_warmup_and_max_win_is_tp_net_of_fees(tmp_path: Path) -> None:
    """Kills: entries decided inside the warm-up; a max_win_r smaller than TP net of fees."""
    env = make_env(_val_lake(tmp_path / "lake"))
    out = run_backtest(request(VFROM, VTO), env, TEST_CONFIG)
    _, rows = read_table(out.run_dir / "trades.parquet")
    assert rows
    assert all(r["entry_bar_open_ms"] > VFROM for r in rows), "entry decided before `from`"
    fee = Decimal("0.001")  # spot VIP-0, taker entry, maker TP (tp_as_taker False)
    best = max(
        (
            Decimal(r["take_profit_price"])
            - Decimal(r["entry_price"]) * (1 + fee)
            - Decimal(r["take_profit_price"]) * fee
        )
        * Decimal(r["qty"])
        / Decimal(r["risk_amount"])
        for r in rows
        if r["book"] == "baseline"
    )
    got = out.metrics["certification_inputs"]["max_win_r"]
    assert abs(got - float(best)) < 1e-9, (got, best)


def test_g3_timing_check_flags_a_fill_at_or_before_the_decision() -> None:
    """Kills: deleting the runner's causality check (it is otherwise never exercised)."""
    from types import SimpleNamespace

    from tsclaude.research.runner import _timing_problems

    rec = SimpleNamespace(request_id="s:x:1", decision_ms=10 * MIN - 1, delivered_ms=10 * MIN - 1)
    ok = SimpleNamespace(request_id="s:x:1", trade_id="t1", entry_bar_open_ms=10 * MIN)
    bad = SimpleNamespace(request_id="s:x:1", trade_id="t2", entry_bar_open_ms=9 * MIN)
    src = SimpleNamespace(records=[rec])
    assert _timing_problems(src, [SimpleNamespace(name="b", trades=[ok])], 0) == []  # type: ignore[arg-type]
    probs = _timing_problems(src, [SimpleNamespace(name="b", trades=[bad])], 0)  # type: ignore[arg-type]
    assert probs
    assert "before its decision" in probs[0]
