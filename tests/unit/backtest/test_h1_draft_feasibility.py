"""The DRAFT `h1_compression_breakout` through the P&L-free feasibility path only (audit H1-1).

Synthetic lake (`bt_helpers`, never market data). No backtest of the draft is run and no trial is
recorded: the CLI builds it only with ``--draft``, refuses non-development ranges, and leaves the
trial registry empty."""

from __future__ import annotations

import argparse
import dataclasses
import itertools
import json
from pathlib import Path
from typing import Any

import pytest
from bt_helpers import JsonLinesCodec, make_env, ms, synthetic_rows, write_lake

from tsclaude.contracts.market import Market
from tsclaude.contracts.signals import SignalStatus
from tsclaude.research import feasibility_cli
from tsclaude.research.feasibility import count_feasibility, iter_decisions, planning_costs_for
from tsclaude.research.source import StrategySource
from tsclaude.research.trials import TrialRegistry
from tsclaude.sim.costs import CostModel
from tsclaude.store.holdout import HoldoutGuard, Purpose
from tsclaude.store.lake_reader import LakeReader
from tsclaude.strategies import H1CompressionBreakout, H1CompressionBreakoutParams
from tsclaude.strategies.h1_compression_breakout import DIAGNOSE_STAGES

MIN = 60_000
SPOT_BTC = (Market.SPOT, "BTCUSDT")
COSTS_FOR = planning_costs_for(CostModel())
FAST = H1CompressionBreakoutParams(
    atr_n=5, rank_m=10, rank_max=0.6, range_n=6, vol_n=6, vol_z_min=0.0, trend_n=4,
    max_cost_r=0.3, time_exit_bars=8,
)  # fmt: skip


def _strategy(**kw: Any) -> H1CompressionBreakout:
    return H1CompressionBreakout(dataclasses.replace(FAST, **kw))


def _lake(tmp: Path, rows: list[dict[str, Any]], name: str = "data") -> Path:
    d = tmp / name
    write_lake(d, [(Market.SPOT, "BTCUSDT", rows)])
    return d


def _cum(out: dict[str, Any], year: str = "2024") -> list[int]:
    return [c["bars"] for c in out["symbols"]["spot:BTCUSDT"]["years"][year]["cumulative"]]


def _source_run(data_dir: Path, strat: H1CompressionBreakout, lo: int, hi: int) -> StrategySource:
    """The backtest's own signal adapter over the same bars (no simulator, no outcome)."""
    src = StrategySource(strat, {SPOT_BTC: COSTS_FOR(*SPOT_BTC)}, trade_from_ms=lo, flat_from_ms=hi)
    res = make_env(data_dir).reader.read_bars(
        Market.SPOT, "BTCUSDT", lo - strat.window_bars * strat.interval.ms, hi,
        purpose=Purpose.RESEARCH,
    )  # fmt: skip
    for bar in res.items:
        src.on_bar(bar, None)  # type: ignore[arg-type]  # the view is not used for signals
    return src


@pytest.fixture(scope="module")
def march(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _lake(tmp_path_factory.mktemp("h1"), synthetic_rows(ms(2024, 3, 1), ms(2024, 3, 6)))


@pytest.mark.parametrize("interval", ["15m", "1h"])
def test_count_matches_the_signal_source_and_is_monotone(march: Path, interval: str) -> None:
    strat = _strategy(interval=interval)
    lo, hi = ms(2024, 3, 3), ms(2024, 3, 6)
    out = count_feasibility(make_env(march).reader, strat, [SPOT_BTC], lo, hi, costs_for=COSTS_FOR)
    assert out["stages"] == list(DIAGNOSE_STAGES)
    assert out["strategy"]["version"] == "0.1.0-draft"
    assert out["pnl_free"] is True
    year = out["symbols"]["spot:BTCUSDT"]["years"]["2024"]
    per_day = 96 if interval == "15m" else 24
    assert year["decision_bars"] == 3 * per_day
    cum = _cum(out)
    assert all(a >= b for a, b in itertools.pairwise(cum))
    assert cum[0] == year["decision_bars"]  # the warm-up lies inside the read range
    src = _source_run(march, strat, lo, hi)
    confirmed = [r for r in src.records if r.proposal.stage is SignalStatus.CONFIRMED]
    assert cum[-1] == len(confirmed)
    assert cum[0] == len(src.decision_bars[SPOT_BTC])
    if interval == "15m":
        assert cum[-1] > 0  # the rule is exercised on this data


def test_gap_resets_history_and_the_warmup_restarts(tmp_path: Path) -> None:
    gap = ms(2024, 3, 4, 12, 0)
    rows = synthetic_rows(ms(2024, 3, 1), ms(2024, 3, 6), skip=[gap, gap + MIN])
    data_dir = _lake(tmp_path, rows)
    strat = _strategy()
    lo, hi = ms(2024, 3, 3), ms(2024, 3, 6)
    out = count_feasibility(
        make_env(data_dir).reader, strat, [SPOT_BTC], lo, hi, costs_for=COSTS_FOR
    )
    year = out["symbols"]["spot:BTCUSDT"]["years"]["2024"]
    assert (year["missing_minutes"], year["history_resets"]) == (2, 1)
    assert year["decision_bars"] == 3 * 96 - 1  # the incomplete bucket is no decision bar
    assert year["decision_bars"] - year["cumulative"][0]["bars"] == strat.warmup_bars - 1
    # the backtest's signal adapter: no decision bar and no proposal in the restarted warm-up
    src = _source_run(data_dir, strat, lo, hi)
    assert src.breaks == 1
    after = [d for d in src.decision_bars[SPOT_BTC] if d > gap]
    step = strat.interval.ms
    first_ready = gap + strat.warmup_bars * step + step - 1  # bucket after the gap = bar 1
    assert after[0] == first_ready
    assert not [r for r in src.records if gap < r.decision_ms < first_ready]


def test_causality_canary_through_the_lake(tmp_path: Path) -> None:
    """Rewriting every 1m bar from `cut` on leaves the stage flags of every decision bar closing
    before `cut` unchanged; cuts inside an hour also prove the forming hour is not read."""
    start, end = ms(2024, 3, 1), ms(2024, 3, 5)
    base = synthetic_rows(start, end, seed=4)
    strat = _strategy()
    lo, hi = ms(2024, 3, 2), end
    ref_dir = _lake(tmp_path, base, "ref")
    ref = list(iter_decisions(make_env(ref_dir).reader, strat, [SPOT_BTC], lo, hi,
                              costs_for=COSTS_FOR))  # fmt: skip
    changed_after = 0
    for k, cut in enumerate((ms(2024, 3, 2, 6, 15), ms(2024, 3, 3, 13, 30), ms(2024, 3, 4, 20))):
        last = next(r for r in reversed(base) if r["open_time_ms"] < cut)
        future = synthetic_rows(cut, end, seed=100 + k, price=last["close"] * 1.07)
        rows = [r for r in base if r["open_time_ms"] < cut] + future
        mut_dir = _lake(tmp_path, rows, f"mut{k}")
        mut = list(iter_decisions(make_env(mut_dir).reader, strat, [SPOT_BTC], lo, hi,
                                  costs_for=COSTS_FOR))  # fmt: skip
        assert [d.decision_ms for d in mut] == [d.decision_ms for d in ref]
        for a, b in zip(ref, mut, strict=True):
            if a.decision_ms < cut:
                assert a.stages == b.stages, a.decision_ms
            elif a.stages != b.stages:
                changed_after += 1
    assert changed_after > 0


# ---------------------------------------------------------------- CLI --draft
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    bsub = sub.add_parser("backtest").add_subparsers(dest="backtest_command", required=True)
    feasibility_cli.register_feasibility(bsub)
    return parser


PARAMS = json.dumps(dataclasses.asdict(FAST))


@pytest.fixture
def cli_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "cli"
    write_lake(d, [(Market.SPOT, "BTCUSDT", synthetic_rows(ms(2024, 3, 1), ms(2024, 3, 4)))])
    monkeypatch.setenv("TSC_DATA_DIR", str(d))

    def reader(data: Path, guard: HoldoutGuard) -> LakeReader:
        return LakeReader.open(data, guard, JsonLinesCodec())

    monkeypatch.setattr(feasibility_cli, "_default_reader", reader)
    return d


def _cli(capsys: pytest.CaptureFixture[str], tmp_path: Path, *argv: str) -> tuple[int, Any]:
    base = ["backtest", "feasibility", "--strategy", "h1_compression_breakout",
            "--symbols", "spot:BTCUSDT", "--config", str(tmp_path / "missing.yaml"),
            "--repo-root", str(tmp_path), "--params", PARAMS]  # fmt: skip
    args = _parser().parse_args([*base, *argv])
    code = args.func(args)
    return code, json.loads(capsys.readouterr().out)


def _no_trials(d: Path) -> None:
    assert TrialRegistry(d / "state.db").list() == []
    assert not (d / "runs").exists()


def test_cli_draft_counts_and_records_no_trial(
    cli_data: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _cli(capsys, tmp_path, "--draft", "--from", "2024-03-02", "--to", "2024-03-04")
    assert code == 0, out
    assert out["ok"] is True
    assert out["draft"] is True
    assert out["trials_recorded"] == 0
    assert out["pnl_free"] is True
    (res,) = out["results"]
    written = json.loads(Path(res["path"]).read_text(encoding="utf-8"))
    assert written["draft"] is True
    assert written["stages"] == list(DIAGNOSE_STAGES)
    assert written["strategy"]["version"] == "0.1.0-draft"
    _no_trials(cli_data)


def test_cli_without_draft_refuses_the_draft_strategy(
    cli_data: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _cli(capsys, tmp_path, "--from", "2024-03-02", "--to", "2024-03-04")
    assert code == 2
    assert out["refused"] == "feasibility"
    assert "unknown strategy" in out["error"]
    assert not (cli_data / "feasibility").exists()


@pytest.mark.parametrize(
    ("argv", "code", "refused"),
    [
        (["--from", "2026-02-01", "--to", "2026-03-01"], 3, "holdout"),
        (["--from", "2025-12-01", "--to", "2026-01-15"], 3, "holdout"),
        (["--from", "2025-02-01", "--to", "2025-03-01"], 2, "feasibility"),
        (["--from", "2024-12-01", "--to", "2025-02-01"], 2, "feasibility"),
    ],
)
def test_cli_draft_refuses_non_development_ranges(
    cli_data: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    code: int,
    refused: str,
) -> None:
    got, out = _cli(capsys, tmp_path, "--draft", *argv)
    assert got == code
    assert out["ok"] is False
    assert out["refused"] == refused
    assert not (cli_data / "feasibility").exists()
    _no_trials(cli_data)
