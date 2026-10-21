"""Backtest runner end to end on synthetic lakes (no network): artifacts, reproducibility, holdout
refusal, trial counting, random control, look-ahead canary, integrity grading, max_loss_r."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
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
from tsclaude.research.runner import (
    REQUIRED_MANIFEST_FIELDS,
    RunOutcome,
    load_run,
    request_from_manifest,
    run_backtest,
)
from tsclaude.store.holdout import HoldoutLocked

START, END = ms(2024, 3, 1), ms(2024, 3, 15)
FROM, TO = ms(2024, 3, 3), ms(2024, 3, 15)


def _lake(path: Path, **kw: Any) -> Path:
    write_lake(path, [(Market.SPOT, "BTCUSDT", synthetic_rows(START, END, **kw))])
    return path


@pytest.fixture(scope="module")
def run1(tmp_path_factory: pytest.TempPathFactory) -> tuple[RunOutcome, Path]:
    d = _lake(tmp_path_factory.mktemp("lake1"))
    env = make_env(d)
    return run_backtest(request(FROM, TO), env, TEST_CONFIG), d


def test_end_to_end_writes_manifest_trades_equity_metrics(run1: tuple[RunOutcome, Path]) -> None:
    out, _ = run1
    files = sorted(p.name for p in out.run_dir.iterdir())
    assert files == ["equity.parquet", "manifest.json", "metrics.json", "trades.parquet"]
    manifest = json.loads((out.run_dir / "manifest.json").read_text(encoding="utf-8"))
    for f in REQUIRED_MANIFEST_FIELDS:
        assert f in manifest, f
    assert manifest["strategy"]["strategy_id"] == "bracket_reference"
    assert manifest["strategy"]["params_hash"] == fast_strategy().params_hash
    assert manifest["datasets"]["main"]  # dataset ids recorded
    assert manifest["seed"] == 0
    assert manifest["timezone"] == "UTC"
    assert manifest["provenance"] == "BACKTEST"
    assert manifest["cost_model"]["version"]
    assert manifest["trials"]["n_trials"] == 1
    m = out.metrics
    assert m["provenance"] == "BACKTEST"
    assert m["trades"]["n"] >= 10
    assert m["integrity"]["problems"] == []
    assert m["integrity"]["open_positions_at_end"] == 0
    assert m["evidence_grade"] in {"C", "D"}  # development segment: never above C
    assert any("development segment" in r for r in m["grade_reasons"]) or m["evidence_grade"] == "D"
    # report fields required by the brief
    cert = m["certification"]
    for key in (
        "p_hat",
        "ci",
        "p_star",
        "margin_lcb",
        "n",
        "n_eff",
        "n_trials",
        "expectancy_lcb",
        "expectancy_ci",
        "worst_trade_r",
        "count_beyond_stop",
        "max_loss_r",
        "max_loss_source",
    ):
        assert key in cert, key
    for key in ("ambiguous_share", "spans_gap_share", "cost_share", "win_rate_ci"):
        assert key in m["trades"], key
    assert set(m["cost_stress"]) == {"x1.5", "x2"}
    assert m["random_control"]["n"] > 0
    # metrics.json bytes hash to the recorded value
    text = (out.run_dir / "metrics.json").read_text(encoding="utf-8").rstrip("\n")
    import hashlib

    assert hashlib.sha256(text.encode()).hexdigest() == manifest["outputs"]["metrics_sha256"]


def test_trades_and_equity_parquet_round_trip(run1: tuple[RunOutcome, Path]) -> None:
    out, _ = run1
    _, rows = read_table(out.run_dir / "trades.parquet")
    assert len(rows) == out.manifest["outputs"]["trades_rows"]
    base = [r for r in rows if r["book"] == "baseline"]
    assert len(base) == out.metrics["trades"]["n"]
    assert sum(r["r"] for r in base) / len(base) == pytest.approx(
        out.metrics["trades"]["expectancy_r"], rel=1e-9
    )
    _, eq = read_table(out.run_dir / "equity.parquet")
    assert len(eq) == out.manifest["outputs"]["equity_rows"]
    assert {r["book"] for r in eq} == {"baseline", "stress_x1.5", "stress_x2"}


def test_pyarrow_reads_the_artifacts(run1: tuple[RunOutcome, Path]) -> None:
    pq = pytest.importorskip("pyarrow.parquet")
    out, _ = run1
    table = pq.read_table(str(out.run_dir / "trades.parquet"))
    _, rows = read_table(out.run_dir / "trades.parquet")
    assert table.num_rows == len(rows)
    assert table.to_pylist() == rows
    eq = pq.read_table(str(out.run_dir / "equity.parquet"))
    assert eq.num_rows == out.manifest["outputs"]["equity_rows"]


def test_reproducible_identical_metrics_hash(run1: tuple[RunOutcome, Path], tmp_path: Path) -> None:
    out, d = run1
    # an independent lake with the same data and a fresh registry
    d2 = _lake(tmp_path / "lake2")
    again = run_backtest(request(FROM, TO), make_env(d2), TEST_CONFIG)
    assert again.metrics_hash == out.metrics_hash
    assert (again.run_dir / "metrics.json").read_bytes() == (
        out.run_dir / "metrics.json"
    ).read_bytes()
    assert (again.run_dir / "trades.parquet").read_bytes() == (
        out.run_dir / "trades.parquet"
    ).read_bytes()
    # rerun from the recorded manifest (dataset ids pinned) in the original environment
    env = make_env(d)
    rerun = run_backtest(request_from_manifest(out.manifest, fast_strategy()), env, TEST_CONFIG)
    assert rerun.manifest["manifest_hash"] == out.manifest["manifest_hash"]
    assert rerun.metrics_hash == out.metrics_hash
    assert rerun.run_id != out.run_id  # a new run directory, same evidence
    loaded = load_run(env.runs_dir, rerun.run_id)
    assert loaded is not None
    assert loaded["summary"]["metrics_verified"] is True


def test_holdout_refused_without_unlock_and_nothing_recorded(tmp_path: Path) -> None:
    d = tmp_path / "lake"
    write_lake(
        d,
        [(Market.SPOT, "BTCUSDT", synthetic_rows(ms(2025, 12, 20), ms(2026, 1, 10)))],
    )
    env = make_env(d)
    with pytest.raises(HoldoutLocked):
        run_backtest(request(ms(2026, 1, 2), ms(2026, 1, 9)), env, TEST_CONFIG)
    # warm-up reaching into the holdout is refused too (run starts after holdout start)
    with pytest.raises(HoldoutLocked):
        run_backtest(request(ms(2026, 1, 1), ms(2026, 1, 5), prereg_id="P-none"), env, TEST_CONFIG)
    assert env.trials.n_trials("bracket_reference") == 0
    assert env.trials.n_trials("P-none") == 0
    assert not env.runs_dir.exists()


def test_trial_count_increments_per_distinct_configuration(tmp_path: Path) -> None:
    d = _lake(tmp_path / "lake")
    env = make_env(d)
    lo, hi = ms(2024, 3, 3), ms(2024, 3, 6)
    a = run_backtest(request(lo, hi), env, TEST_CONFIG)
    b = run_backtest(request(lo, hi, strategy=fast_strategy(tp_r=0.8)), env, TEST_CONFIG)
    a2 = run_backtest(request(lo, hi), env, TEST_CONFIG)
    assert (a.metrics["n_trials"], b.metrics["n_trials"], a2.metrics["n_trials"]) == (1, 2, 2)
    rows = env.trials.list("bracket_reference")
    assert len(rows) == 2
    assert sorted(r["evaluations"] for r in rows) == [1, 2]
    # n_trials flows into certify: alpha_adj = alpha / n_trials
    cert = b.metrics["certification"]
    if cert is not None:
        assert cert["n_trials"] == 2
        assert cert["alpha_adj"] == pytest.approx(TEST_CONFIG.alpha / 2)
    # a different hypothesis id has its own count
    c = run_backtest(request(lo, hi, hypothesis_id="H1-test"), env, TEST_CONFIG)
    assert c.metrics["n_trials"] == 1


def test_random_entry_control_matches_entries_per_hour(run1: tuple[RunOutcome, Path]) -> None:
    out, _ = run1
    _, rows = read_table(out.run_dir / "trades.parquet")
    hours = lambda book: Counter(  # noqa: E731
        (r["entry_bar_open_ms"] // 3_600_000) % 24 for r in rows if r["book"] == book
    )
    base, control = hours("baseline"), hours("control")
    assert sum(control.values()) > 0
    for h, k in control.items():  # never more control entries in an hour than the strategy
        assert k <= base[h]
    rc = out.metrics["random_control"]
    assert rc["scheduled_entries"] <= out.metrics["trades"]["n"]
    assert rc["edge_r"] == pytest.approx(
        out.metrics["trades"]["expectancy_r"] - rc["expectancy_r"], abs=1e-9
    )
    assert rc["beats_control"] in (True, False)


def test_lookahead_canary_future_mutation_never_changes_closed_past(tmp_path: Path) -> None:
    cut = ms(2024, 3, 9)
    base_rows = synthetic_rows(START, END)
    mutated = [dict(r) for r in base_rows]
    for r in mutated:
        if r["open_time_ms"] >= cut:  # a crash and a volume explosion in the future
            for k in ("open", "high", "low", "close"):
                r[k] = round(r[k] * 0.5, 2)
            r["volume"] *= 50
    d1, d2 = tmp_path / "a", tmp_path / "b"
    write_lake(d1, [(Market.SPOT, "BTCUSDT", base_rows)])
    write_lake(d2, [(Market.SPOT, "BTCUSDT", mutated)])
    o1 = run_backtest(request(FROM, TO), make_env(d1), TEST_CONFIG)
    o2 = run_backtest(request(FROM, TO), make_env(d2), TEST_CONFIG)

    def closed_before(o: RunOutcome) -> list[dict[str, Any]]:
        _, rows = read_table(o.run_dir / "trades.parquet")
        return [r for r in rows if r["book"] == "baseline" and r["exit_bar_open_ms"] < cut - MIN]

    past1, past2 = closed_before(o1), closed_before(o2)
    assert past1, "fixture must produce trades before the cut"
    assert past1 == past2
    assert o1.metrics_hash != o2.metrics_hash  # the future did change


def test_data_gap_in_range_is_grade_x(tmp_path: Path) -> None:
    gap = [ms(2024, 3, 5, 12) + i * MIN for i in range(30)]
    d = _lake(tmp_path / "lake", skip=gap)
    out = run_backtest(request(FROM, TO), make_env(d), TEST_CONFIG)
    assert out.metrics["evidence_grade"] == "X"
    assert out.metrics["integrity"]["missing_minutes_in_range"] == 30
    assert any("data gaps" in r for r in out.metrics["grade_reasons"])


def test_max_loss_from_development_gap_through_stops(tmp_path: Path) -> None:
    jumps = {ms(2024, 3, 1) + k * 2 * 3_600_000: 0.985 for k in range(14 * 12)}
    d = _lake(tmp_path / "lake", jumps=jumps)
    out = run_backtest(request(FROM, TO), make_env(d), TEST_CONFIG)
    meas = out.metrics["certification_inputs"]["max_loss_measurement"]
    assert meas["measured"] is True
    assert meas["dev_gap_fill_stops"] >= 1
    worst = meas["worst_stop_loss_r"]
    planned = meas["planned_stop_loss_r"]
    assert worst < -planned  # a gap-through beyond the planned stop loss
    assert meas["value"] == pytest.approx(1.5 * -worst)
    assert out.manifest["max_loss"]["value"] == pytest.approx(1.5 * -worst)
    assert "development-segment" in out.manifest["max_loss"]["source"]
    cert = out.metrics["certification"]
    assert cert["max_loss_r"] == pytest.approx(1.5 * -worst)
    assert cert["count_beyond_stop"] >= 1


def test_validation_run_without_dev_data_uses_fallback_and_caps(tmp_path: Path) -> None:
    d = tmp_path / "lake"
    write_lake(d, [(Market.SPOT, "BTCUSDT", synthetic_rows(ms(2025, 3, 1), ms(2025, 3, 8)))])
    out = run_backtest(request(ms(2025, 3, 3), ms(2025, 3, 8)), make_env(d), TEST_CONFIG)
    assert out.manifest["segment"] == "validation"
    meas = out.metrics["certification_inputs"]["max_loss_measurement"]
    assert meas["measured"] is False
    assert out.manifest["max_loss"]["source"].startswith("fallback")
    if out.metrics["evidence_grade"] not in ("D", "X"):
        assert any("max_loss_r not measured" in r for r in out.metrics["grade_reasons"])


def test_runner_rejects_unaligned_or_empty_ranges(tmp_path: Path) -> None:
    d = _lake(tmp_path / "lake")
    env = make_env(d)
    with pytest.raises(ValueError, match="aligned"):
        run_backtest(request(FROM + MIN, TO), env, TEST_CONFIG)
    with pytest.raises(ValueError, match="after"):
        run_backtest(request(TO, FROM), env, TEST_CONFIG)
    with pytest.raises(ValueError, match="instrument"):
        run_backtest(request(FROM, TO, symbols=("ETHUSDT",)), env, type(TEST_CONFIG)())


# Recorded on Linux (sandbox, numpy 2.5). SIMULATION §9: the same manifest must give the same
# metrics.json hash on Windows and Linux; CI runs this on both. If an intentional change to the
# runner, report, simulator, statistics or fixture alters the evidence, update these values in the
# same commit and say why.
GOLDEN_MANIFEST = "349aa232dadd510595539d51937d2241fb1605afd4adf16709891e92f47f6098"
GOLDEN_METRICS = "c7fc3a8a322d4d37f99719940bac738bad9411be487af06b768cea61e5f7993b"
GOLDEN_TRADES = "82eb900102f198e23ca2f9c17df835ddb7d9e14d28c7162454d57ead48ec7b22"


def test_golden_hashes_identical_across_platforms(run1: tuple[RunOutcome, Path]) -> None:
    out, _ = run1
    assert out.manifest["manifest_hash"] == GOLDEN_MANIFEST
    assert out.manifest["outputs"]["trades_parquet_sha256"] == GOLDEN_TRADES
    assert out.metrics_hash == GOLDEN_METRICS
