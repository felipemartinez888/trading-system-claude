"""Tests for the M2-T4 causality-audit fixes (beyond the auditor's A1-A6 / g1-g3)."""

from __future__ import annotations

import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from bt_helpers import (
    MIN,
    TEST_CONFIG,
    JsonLinesCodec,
    fast_strategy,
    make_env,
    ms,
    request,
    synthetic_rows,
    write_lake,
)

from tsclaude.contracts.market import Market
from tsclaude.research import runner
from tsclaude.research.parquet import read_table
from tsclaude.research.prereg import PreregistrationRefused
from tsclaude.research.runner import BacktestConfig, BacktestRequest, load_run, run_backtest
from tsclaude.store.lake import LakeKind
from tsclaude.store.lake_writer import LakeWriter

VAL_START, VAL_END = ms(2025, 3, 1), ms(2025, 3, 15)
VFROM, VTO = ms(2025, 3, 3), ms(2025, 3, 15)
H8 = 8 * 3_600_000
USDM_CFG = BacktestConfig.from_mapping(
    {
        "n_resamples": 2000,
        "instruments": {
            "usdm": {"default": {"tick_size": "0.01", "lot_size": "0.001", "min_notional": "5"}}
        },
    }
)


def _val_lake(path: Path) -> Path:
    write_lake(path, [(Market.SPOT, "BTCUSDT", synthetic_rows(VAL_START, VAL_END))])
    return path


# ---------------------------------------------------------------- P1: the timing check is wired
def test_timing_check_result_reaches_the_grade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting the runner's call to `_timing_problems` must fail this test."""
    calls: list[int] = []

    def fake(source: object, books: object, latency_ms: int) -> list[str]:
        calls.append(latency_ms)
        return ["baseline:t1 filled before its decision time"]

    monkeypatch.setattr(runner, "_timing_problems", fake)
    out = run_backtest(request(VFROM, VTO), make_env(_val_lake(tmp_path / "l")), TEST_CONFIG)
    assert len(calls) == 2  # strategy books and control book
    assert out.metrics["evidence_grade"] == "X"
    assert any("before its decision" in p for p in out.metrics["integrity"]["problems"])


# ---------------------------------------------------------------- funding (finding 3)
def _usdm_lake(path: Path, *, drop_funding_at: int | None = None) -> Path:
    write_lake(path, [(Market.USDM, "BTCUSDT", synthetic_rows(VAL_START, VAL_END))])
    rows = []
    t = VAL_START
    while t < VAL_END:
        if t != drop_funding_at:
            rows.append(
                {
                    "funding_time_ms": t,
                    "funding_rate": 0.0005,
                    "mark_price": 100.0,
                    "source": "archive",
                    "ingested_at_ms": t + 1,
                }
            )
        t += H8
    staging = LakeWriter(path, JsonLinesCodec()).begin()
    staging.write_parts(Market.USDM, LakeKind.FUNDING, "BTCUSDT", rows)
    staging.commit({"market": "usdm", "kind": "funding", "symbol": "BTCUSDT"}, None)
    return path


def _usdm_request() -> BacktestRequest:
    return BacktestRequest(
        strategy=fast_strategy(), symbols=[(Market.USDM, "BTCUSDT")], from_ms=VFROM, to_ms=VTO
    )


def test_usdm_funding_is_applied_at_funding_timestamps(tmp_path: Path) -> None:
    out = run_backtest(_usdm_request(), make_env(_usdm_lake(tmp_path / "l")), USDM_CFG)
    assert out.metrics["integrity"]["problems"] == []
    assert out.metrics["integrity"]["funding_applied"] > 0
    _, rows = read_table(out.run_dir / "trades.parquet")
    base = [r for r in rows if r["book"] == "baseline"]
    for r in base:
        crossed = (r["exit_bar_open_ms"] - 1) // H8 - r["entry_bar_open_ms"] // H8
        funding = Decimal(r["funding"])
        if r["exit_at_open"] and r["exit_bar_open_ms"] % H8 == 0:
            crossed = (r["exit_bar_open_ms"]) // H8 - r["entry_bar_open_ms"] // H8
        if crossed > 0 and r["entry_bar_open_ms"] % H8 != 0:
            assert funding < 0, r  # longs pay a positive rate
        if crossed == 0:
            assert funding == 0, r
    assert any(Decimal(r["funding"]) < 0 for r in base)
    assert out.manifest["funding"]["applied"] == out.metrics["integrity"]["funding_applied"]


def test_usdm_missing_funding_is_grade_x(tmp_path: Path) -> None:
    lake = _usdm_lake(tmp_path / "l", drop_funding_at=VAL_START + 20 * H8)
    out = run_backtest(_usdm_request(), make_env(lake), USDM_CFG)
    assert out.metrics["evidence_grade"] == "X"
    assert any("funding data missing" in p for p in out.metrics["integrity"]["problems"])


# ---------------------------------------------------------------- prereg ids (finding 1)
def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.invalid", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        check=True,
        capture_output=True,
    )  # fmt: skip


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_prereg_id_needs_a_committed_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "preregistrations").mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / "preregistrations" / "P1.yaml").write_text("prereg_id: P1\n", encoding="utf-8")
    _git(repo, "add", "preregistrations/P1.yaml")
    _git(repo, "commit", "-q", "-m", "P1")
    (repo / "preregistrations" / "P2.yaml").write_text("prereg_id: P2\n", encoding="utf-8")
    env = make_env(_val_lake(tmp_path / "l"))
    env.repo_root = repo
    with pytest.raises(PreregistrationRefused, match="not committed"):
        run_backtest(request(VFROM, VTO, prereg_id="P2"), env, TEST_CONFIG)
    with pytest.raises(PreregistrationRefused, match="does not exist"):
        run_backtest(request(VFROM, VTO, prereg_id="P3"), env, TEST_CONFIG)
    with pytest.raises(PreregistrationRefused, match="invalid"):
        run_backtest(request(VFROM, VTO, prereg_id="../x"), env, TEST_CONFIG)
    assert env.trials.n_trials("bracket_reference") == 0  # refused before anything is recorded
    out = run_backtest(request(VFROM, VTO, prereg_id="P1"), env, TEST_CONFIG)
    assert out.manifest["prereg_id"] == "P1"
    (repo / "preregistrations" / "P1.yaml").write_text("prereg_id: P1\nedited\n", encoding="utf-8")
    with pytest.raises(PreregistrationRefused, match="uncommitted"):
        run_backtest(request(VFROM, VTO, prereg_id="P1"), env, TEST_CONFIG)


# ---------------------------------------------------------------- N1: recomputed reports
def test_show_recomputes_with_current_trial_count(tmp_path: Path) -> None:
    env = make_env(_val_lake(tmp_path / "l"))
    first = run_backtest(request(VFROM, VTO), env, TEST_CONFIG)
    before = load_run(env.runs_dir, first.run_id)
    assert before is not None
    assert before["summary"]["recomputed"] is False
    assert before["current"] is None
    run_backtest(request(VFROM, VTO, strategy=fast_strategy(tp_r=0.7)), env, TEST_CONFIG)
    after = load_run(env.runs_dir, first.run_id)
    assert after is not None
    s, cur = after["summary"], after["current"]
    assert s["recomputed"] is True
    assert s["n_trials"] == 2
    assert s["n_trials_recorded"] == 1
    assert cur["certification"]["n_trials"] == 2
    assert cur["certification"]["alpha_adj"] == pytest.approx(TEST_CONFIG.alpha / 2)
    assert cur["random_control"]["alpha_adj"] == pytest.approx(TEST_CONFIG.alpha / 2)
    # the stored evidence is untouched
    assert after["metrics"]["n_trials"] == 1
    assert after["summary"]["metrics_verified"] is True


# ---------------------------------------------------------------- N3: history breaks at gaps
def test_feature_history_restarts_after_an_in_range_gap(tmp_path: Path) -> None:
    gap_start = ms(2025, 3, 8, 12)
    gap = [gap_start + i * MIN for i in range(45)]
    lake = tmp_path / "l"
    write_lake(lake, [(Market.SPOT, "BTCUSDT", synthetic_rows(VAL_START, VAL_END, skip=gap))])
    out = run_backtest(request(VFROM, VTO), make_env(lake), TEST_CONFIG)
    assert out.metrics["evidence_grade"] == "X"
    assert out.metrics["signals"]["history_breaks"] >= 1
    warm_ms = fast_strategy().warmup_bars * 15 * MIN
    gap_end = gap_start + 45 * MIN
    _, rows = read_table(out.run_dir / "trades.parquet")
    assert not [
        r for r in rows
        if r["book"] == "baseline" and gap_start <= r["entry_bar_open_ms"] < gap_end + warm_ms
    ]  # fmt: skip


# ---------------------------------------------------------------- N2 / seed (finding 2)
def test_explicit_seed_is_a_trial_and_default_seed_is_derived(tmp_path: Path) -> None:
    env = make_env(_val_lake(tmp_path / "l"))
    a = run_backtest(request(VFROM, VTO), env, TEST_CONFIG)
    a2 = run_backtest(request(VFROM, VTO), env, TEST_CONFIG)
    assert a.manifest["seed"] == a2.manifest["seed"]
    assert a.manifest["seed_source"] == "trial key"
    assert a2.metrics["n_trials"] == 1
    b = run_backtest(request(VFROM, VTO, seed=a.manifest["seed"]), env, TEST_CONFIG)
    assert b.metrics["n_trials"] == 2  # explicit seed: its own trial, even with the same value
    assert b.manifest["seed_source"].startswith("explicit")
    assert "slippage" in a.manifest["cost_model"]
    assert a.manifest["cost_model"]["slippage"]["half_spread_bps"] == "2"
