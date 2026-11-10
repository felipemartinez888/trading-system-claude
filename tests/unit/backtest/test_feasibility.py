"""P&L-free feasibility count (audit H1-1): stage counts on development data, no outcome."""

from __future__ import annotations

import argparse
import itertools
import json
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

import pytest
from bt_helpers import (
    FAST_PARAMS,
    MIN,
    JsonLinesCodec,
    fast_strategy,
    make_env,
    ms,
    synthetic_rows,
    write_lake,
)

from tsclaude.contracts.market import Bar, Market
from tsclaude.contracts.signals import SignalStatus
from tsclaude.features.core import BarArrays
from tsclaude.research import feasibility_cli
from tsclaude.research.feasibility import (
    FeasibilityHoldoutRefused,
    FeasibilityRefused,
    count_feasibility,
    iter_decisions,
    planning_costs_for,
)
from tsclaude.research.runner import _planning
from tsclaude.research.source import StrategySource
from tsclaude.sim.costs import CostModel
from tsclaude.store.holdout import HoldoutGuard, Purpose
from tsclaude.store.lake_reader import LakeReader, ReadResult
from tsclaude.strategies import BracketReference, PlanningCosts

SPOT_BTC = (Market.SPOT, "BTCUSDT")
COSTS_FOR = planning_costs_for(CostModel())
STAGES = [
    "warmup_ready", "features_defined", "compressed", "trend", "breakout", "volume_ok",
    "stop_valid", "cost_ok",
]  # fmt: skip
TOP_KEYS = {
    "kind", "version", "provenance", "pnl_free", "segment", "strategy", "range", "stages",
    "counting", "planning_costs", "symbols", "dataset_ids", "content_hash",
}  # fmt: skip
SYMBOL_KEYS = {"years", "total", "warmup", "conflicts", "bars_1m"}
YEAR_KEYS = {"decision_bars", "cumulative", "missing_minutes", "history_resets"}


class RecordingReader:
    """Wraps a `LakeReader` and records every `read_bars` call."""

    def __init__(self, inner: LakeReader) -> None:
        self.inner = inner
        self.calls: list[tuple[int, int, Any]] = []

    def read_bars(
        self,
        market: Market,
        symbol: str,
        start_ms: int,
        end_ms: int,
        *,
        purpose: Purpose | str,
        prereg_id: str | None = None,
        strategy_hash: str | None = None,
        dataset_ids: Collection[str] | None = None,
    ) -> ReadResult[Bar]:
        self.calls.append((start_ms, end_ms, dataset_ids))
        return self.inner.read_bars(
            market,
            symbol,
            start_ms,
            end_ms,
            purpose=purpose,
            prereg_id=prereg_id,
            strategy_hash=strategy_hash,
            dataset_ids=dataset_ids,
        )


def _lake(tmp_path: Path, rows: list[dict[str, Any]], name: str = "data") -> tuple[Path, list[str]]:
    d = tmp_path / name
    return d, write_lake(d, [(Market.SPOT, "BTCUSDT", rows)])


def _reader(data_dir: Path) -> RecordingReader:
    return RecordingReader(make_env(data_dir).reader)


def _cum(result: Mapping[str, Any], year: str = "2024") -> list[int]:
    return [c["bars"] for c in result["symbols"]["spot:BTCUSDT"]["years"][year]["cumulative"]]


@pytest.fixture(scope="module")
def march(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, list[str]]:
    tmp = tmp_path_factory.mktemp("feas")
    return _lake(tmp, synthetic_rows(ms(2024, 3, 1), ms(2024, 3, 6)))


def test_counts_match_the_backtest_signal_path_and_are_monotone(
    march: tuple[Path, list[str]],
) -> None:
    data_dir, ids = march
    strat = fast_strategy(max_cost_r=0.3)
    lo, hi = ms(2024, 3, 2), ms(2024, 3, 6)
    out = count_feasibility(_reader(data_dir), strat, [SPOT_BTC], lo, hi, costs_for=COSTS_FOR)
    assert out["stages"] == STAGES
    year = out["symbols"]["spot:BTCUSDT"]["years"]["2024"]
    assert year["decision_bars"] == 4 * 96
    cum = _cum(out)
    assert all(a >= b for a, b in itertools.pairwise(cum))  # monotone non-increasing
    assert year["decision_bars"] >= cum[0]
    assert cum[-2] > cum[-1] > 0  # the cost filter removes some candidates on this data
    assert out["symbols"]["spot:BTCUSDT"]["total"] == year
    assert out["dataset_ids"] == sorted(ids)
    assert out["provenance"] == "BACKTEST"
    assert out["kind"] == "feasibility_count"
    assert out["pnl_free"] is True

    # the same bars through the backtest's own signal source (StrategySource): the last stage
    # count equals its CONFIRMED proposals and the warm-up stage its tradable decision bars
    from tsclaude.research.runner import BacktestRequest

    req = BacktestRequest(strategy=strat, symbols=[SPOT_BTC], from_ms=lo, to_ms=hi)
    assert _planning(make_env(data_dir), req)[SPOT_BTC] == COSTS_FOR(*SPOT_BTC)
    src = StrategySource(strat, {SPOT_BTC: COSTS_FOR(*SPOT_BTC)}, trade_from_ms=lo, flat_from_ms=hi)
    res = make_env(data_dir).reader.read_bars(
        Market.SPOT, "BTCUSDT", lo - strat.window_bars * strat.interval.ms, hi,
        purpose=Purpose.RESEARCH,
    )  # fmt: skip
    for bar in res.items:
        src.on_bar(bar, None)  # type: ignore[arg-type]  # the view is not used for signals
    confirmed = [r for r in src.records if r.proposal.stage is SignalStatus.CONFIRMED]
    assert cum[-1] == len(confirmed)
    assert cum[0] == len(src.decision_bars[SPOT_BTC])


def test_output_key_set_has_no_outcome_fields(march: tuple[Path, list[str]]) -> None:
    """Nothing derived from prices after a decision bar can be in the output: the key sets are
    exactly counts, coverage and identity (no trade, exit, R, win/loss, equity)."""
    data_dir, _ = march
    out = count_feasibility(
        _reader(data_dir), fast_strategy(), [SPOT_BTC], ms(2024, 3, 2), ms(2024, 3, 4),
        costs_for=COSTS_FOR,
    )  # fmt: skip
    assert set(out) == TOP_KEYS
    sym = out["symbols"]["spot:BTCUSDT"]
    assert set(sym) == SYMBOL_KEYS
    assert set(sym["warmup"]) == {"missing_minutes", "history_resets"}
    for y in [*sym["years"].values(), sym["total"]]:
        assert set(y) == YEAR_KEYS
        assert all(set(c) == {"stage", "bars"} for c in y["cumulative"])
    assert set(out["range"]) == {"from_ms", "to_ms", "from", "to", "read_from_ms", "warmup_ms"}
    assert set(out["planning_costs"]["spot:BTCUSDT"]) == {"round_trip_frac", "basis"}
    keys = _all_keys({k: v for k, v in out.items() if k != "strategy"})  # params are inputs
    for word in ("pnl", "equity", "win", "trade", "exit", "expectancy", "return", "price", "r_"):
        assert not [k for k in keys if word in k.lower() and k != "pnl_free"], word


def _all_keys(obj: Any) -> set[str]:
    if isinstance(obj, dict):
        return set(obj) | {k for v in obj.values() for k in _all_keys(v)}
    if isinstance(obj, list):
        return {k for v in obj for k in _all_keys(v)}
    return set()


def test_deterministic_hash(march: tuple[Path, list[str]]) -> None:
    data_dir, ids = march
    args = ([SPOT_BTC], ms(2024, 3, 2), ms(2024, 3, 5))
    a = count_feasibility(_reader(data_dir), fast_strategy(), *args, costs_for=COSTS_FOR)
    b = count_feasibility(_reader(data_dir), fast_strategy(), *args, costs_for=COSTS_FOR)
    assert a == b
    assert a["content_hash"] == b["content_hash"]
    pinned = count_feasibility(
        _reader(data_dir), fast_strategy(), *args, costs_for=COSTS_FOR, dataset_ids=ids
    )
    assert pinned["content_hash"] == a["content_hash"]
    other = count_feasibility(
        _reader(data_dir), fast_strategy(max_cost_r=0.3), *args, costs_for=COSTS_FOR
    )
    assert other["content_hash"] != a["content_hash"]


def test_gap_resets_history_and_is_counted_per_year(tmp_path: Path) -> None:
    gap = ms(2024, 1, 1, 12, 0)
    rows = synthetic_rows(ms(2023, 12, 30), ms(2024, 1, 3), skip=[gap, gap + MIN, gap + 2 * MIN])
    data_dir, _ = _lake(tmp_path, rows)
    out = count_feasibility(
        _reader(data_dir), fast_strategy(), [SPOT_BTC], ms(2023, 12, 31), ms(2024, 1, 3),
        costs_for=COSTS_FOR,
    )  # fmt: skip
    years = out["symbols"]["spot:BTCUSDT"]["years"]
    assert set(years) == {"2023", "2024"}
    y23, y24 = years["2023"], years["2024"]
    assert (y23["missing_minutes"], y23["history_resets"]) == (0, 0)
    assert (y24["missing_minutes"], y24["history_resets"]) == (3, 1)
    assert y23["decision_bars"] == 96
    assert y24["decision_bars"] == 2 * 96 - 1  # the incomplete bucket is no decision bar
    # after the reset the warm-up restarts: window_bars - ... bars are not ready
    not_ready = y24["decision_bars"] - y24["cumulative"][0]["bars"]
    assert not_ready == fast_strategy().warmup_bars - 1
    assert y23["cumulative"][0]["bars"] == 96
    total = out["symbols"]["spot:BTCUSDT"]["total"]
    assert total["decision_bars"] == y23["decision_bars"] + y24["decision_bars"]
    assert total["history_resets"] == 1
    assert total["missing_minutes"] == 3


def test_missing_month_gives_missing_minutes_not_an_error(tmp_path: Path) -> None:
    data_dir, _ = _lake(tmp_path, synthetic_rows(ms(2024, 3, 1), ms(2024, 3, 4)))
    strat = fast_strategy()
    out = count_feasibility(
        _reader(data_dir), strat, [SPOT_BTC], ms(2024, 2, 28), ms(2024, 3, 3), costs_for=COSTS_FOR
    )
    sym = out["symbols"]["spot:BTCUSDT"]
    assert sym["years"]["2024"]["missing_minutes"] == 2 * 1440  # 28 and 29 February
    assert sym["warmup"]["missing_minutes"] == strat.window_bars * 15
    assert sym["years"]["2024"]["decision_bars"] == 2 * 96
    assert sym["years"]["2024"]["cumulative"][0]["bars"] == 2 * 96 - (strat.warmup_bars - 1)


@pytest.mark.parametrize(
    ("lo", "hi", "holdout"),
    [
        (ms(2025, 2, 1), ms(2025, 3, 1), False),  # validation
        (ms(2024, 12, 1), ms(2025, 2, 1), False),  # mixed development / validation
        (ms(2021, 6, 1), ms(2021, 7, 1), False),  # before the development segment
        (ms(2026, 2, 1), ms(2026, 3, 1), True),  # holdout
        (ms(2024, 6, 1), ms(2026, 2, 1), True),  # mixed, touches the holdout
        (ms(2025, 12, 31, 12), ms(2026, 1, 1, 12), True),
    ],
)
def test_refuses_non_development_ranges_before_any_read(
    march: tuple[Path, list[str]], lo: int, hi: int, holdout: bool
) -> None:
    reader = _reader(march[0])
    exc = FeasibilityHoldoutRefused if holdout else FeasibilityRefused
    with pytest.raises(exc):
        count_feasibility(reader, fast_strategy(), [SPOT_BTC], lo, hi, costs_for=COSTS_FOR)
    with pytest.raises(exc):
        list(iter_decisions(reader, fast_strategy(), [SPOT_BTC], lo, hi, costs_for=COSTS_FOR))
    assert reader.calls == []


def test_refuses_misaligned_range_and_strategy_without_stages(
    march: tuple[Path, list[str]],
) -> None:
    class NoStages(BracketReference):
        def diagnose(self, window: BarArrays, costs: PlanningCosts) -> None:
            return None

    reader = _reader(march[0])
    with pytest.raises(FeasibilityRefused, match="no entry stages"):
        count_feasibility(
            reader, NoStages(FAST_PARAMS), [SPOT_BTC], ms(2024, 3, 2), ms(2024, 3, 3),
            costs_for=COSTS_FOR,
        )  # fmt: skip
    with pytest.raises(FeasibilityRefused, match="aligned"):
        count_feasibility(
            reader, fast_strategy(), [SPOT_BTC], ms(2024, 3, 2, 0, 7), ms(2024, 3, 3),
            costs_for=COSTS_FOR,
        )  # fmt: skip
    assert reader.calls == []


def test_reads_are_guarded_research_reads_inside_the_read_range(
    march: tuple[Path, list[str]],
) -> None:
    reader = _reader(march[0])
    strat = fast_strategy()
    lo, hi = ms(2024, 3, 2), ms(2024, 3, 3)
    count_feasibility(reader, strat, [SPOT_BTC], lo, hi, costs_for=COSTS_FOR)
    read_from = lo - strat.window_bars * strat.interval.ms
    assert reader.calls[0] == (read_from, hi, [])  # guard check, nothing selected
    assert all(read_from <= a < b <= hi for a, b, _ in reader.calls)


def test_causality_canary_future_bars_never_change_past_stage_flags(tmp_path: Path) -> None:
    """Rewriting every 1m bar from `cut` on (different prices and volumes) leaves the stage
    flags of every decision bar closing before `cut` unchanged."""
    start, end = ms(2024, 3, 1), ms(2024, 3, 5)
    base = synthetic_rows(start, end, seed=4)
    strat = fast_strategy(max_cost_r=0.3)
    lo, hi = ms(2024, 3, 2), end
    ref_dir, _ = _lake(tmp_path, base, "ref")
    ref = list(iter_decisions(_reader(ref_dir), strat, [SPOT_BTC], lo, hi, costs_for=COSTS_FOR))
    changed_after = 0
    for k, cut in enumerate((ms(2024, 3, 2, 6), ms(2024, 3, 3, 13, 15), ms(2024, 3, 4, 20))):
        last = next(r for r in reversed(base) if r["open_time_ms"] < cut)
        future = synthetic_rows(cut, end, seed=100 + k, price=last["close"] * 1.07)
        rows = [r for r in base if r["open_time_ms"] < cut] + future
        mut_dir, _ = _lake(tmp_path, rows, f"mut{k}")
        mut = list(iter_decisions(_reader(mut_dir), strat, [SPOT_BTC], lo, hi, costs_for=COSTS_FOR))
        assert [d.decision_ms for d in mut] == [d.decision_ms for d in ref]
        for a, b in zip(ref, mut, strict=True):
            if a.decision_ms < cut:
                assert a.stages == b.stages, a.decision_ms
            elif a.stages != b.stages:
                changed_after += 1
    assert changed_after > 0  # the mutation is visible after the cut (the canary is live)


# ---------------------------------------------------------------- CLI
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    bsub = sub.add_parser("backtest").add_subparsers(dest="backtest_command", required=True)
    feasibility_cli.register_feasibility(bsub)
    return parser


PARAMS = json.dumps(
    {
        "atr_n": 5, "rank_m": 10, "rank_max": 0.6, "range_n": 6, "vol_n": 6, "vol_z_min": 0.0,
        "ema_span": 5, "rv_n": 5, "max_cost_r": 0.5,
    }
)  # fmt: skip


@pytest.fixture
def cli_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[str]]:
    d = tmp_path / "cli"
    ids = write_lake(d, [(Market.SPOT, "BTCUSDT", synthetic_rows(ms(2024, 3, 1), ms(2024, 3, 4)))])
    monkeypatch.setenv("TSC_DATA_DIR", str(d))

    def reader(data: Path, guard: HoldoutGuard) -> LakeReader:
        return LakeReader.open(data, guard, JsonLinesCodec())

    monkeypatch.setattr(feasibility_cli, "_default_reader", reader)
    return d, ids


def _cli(capsys: pytest.CaptureFixture[str], tmp_path: Path, *argv: str) -> tuple[int, Any]:
    base = ["backtest", "feasibility", "--strategy", "bracket_reference",
            "--symbols", "spot:BTCUSDT", "--config", str(tmp_path / "missing.yaml"),
            "--repo-root", str(tmp_path)]  # fmt: skip
    args = _parser().parse_args([*base, *argv])
    code = args.func(args)
    return code, json.loads(capsys.readouterr().out)


def test_cli_writes_one_count_per_configuration(
    cli_data: tuple[Path, list[str]], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    d, ids = cli_data
    code, out = _cli(
        capsys, tmp_path, "--params", PARAMS, "--from", "2024-03-02", "--to", "2024-03-04"
    )
    assert code == 0
    assert out["ok"] is True
    assert out["pnl_free"] is True
    assert out["trials_recorded"] == 0
    assert out["provenance"] == "BACKTEST"
    (res,) = out["results"]
    path = Path(res["path"])
    assert path == d / "feasibility" / f"feas-{res['content_hash'][:12]}" / "feasibility.json"
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["content_hash"] == res["content_hash"]
    assert written["stages"] == STAGES
    assert written["dataset_ids"] == sorted(ids)

    grid = tmp_path / "grid.json"
    p = json.loads(PARAMS)
    grid.write_text(json.dumps([p, {**p, "max_cost_r": 0.3}]), encoding="utf-8")
    ds = tmp_path / "ds.json"
    ds.write_text(json.dumps({"dataset_ids": ids}), encoding="utf-8")
    code, out = _cli(
        capsys, tmp_path, "--grid", str(grid), "--datasets", str(ds),
        "--from", "2024-03-02", "--to", "2024-03-04",
    )  # fmt: skip
    assert code == 0
    assert len(out["results"]) == 2
    assert len({r["content_hash"] for r in out["results"]}) == 2
    assert res["content_hash"] in {r["content_hash"] for r in out["results"]}  # pinned = same


@pytest.mark.parametrize(
    ("argv", "code", "refused"),
    [
        (["--from", "2025-02-01", "--to", "2025-03-01"], 2, "feasibility"),
        (["--from", "2024-12-01", "--to", "2025-02-01"], 2, "feasibility"),
        (["--from", "2026-02-01", "--to", "2026-03-01"], 3, "holdout"),
        (["--from", "2024-03-02", "--to", "2024-03-04", "--datasets", "UNKNOWN"], 2, "feasibility"),
    ],
)
def test_cli_refusals_write_nothing(
    cli_data: tuple[Path, list[str]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    code: int,
    refused: str,
) -> None:
    d, _ = cli_data
    if "UNKNOWN" in argv:
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps(["0" * 64]), encoding="utf-8")
        argv = [str(bad) if a == "UNKNOWN" else a for a in argv]
    got, out = _cli(capsys, tmp_path, "--params", PARAMS, *argv)
    assert got == code
    assert out["ok"] is False
    assert out["refused"] == refused
    assert not (d / "feasibility").exists()


def test_cli_refuses_both_params_and_grid(
    cli_data: tuple[Path, list[str]], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    grid = tmp_path / "g.json"
    grid.write_text("[{}]", encoding="utf-8")
    code, out = _cli(
        capsys, tmp_path, "--params", PARAMS, "--grid", str(grid),
        "--from", "2024-03-02", "--to", "2024-03-04",
    )  # fmt: skip
    assert code == 2
    assert out["refused"] == "feasibility"
