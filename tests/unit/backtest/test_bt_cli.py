"""`tsclaude backtest run | show | list | trials` through the real CLI entry point."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bt_helpers import JsonLinesCodec, ms, synthetic_rows, write_lake

from tsclaude.cli import main
from tsclaude.contracts.market import Market
from tsclaude.research import backtest_cli
from tsclaude.store.holdout import HoldoutGuard
from tsclaude.store.lake_reader import LakeReader

PARAMS = (
    '{"atr_n": 5, "rank_m": 10, "rank_max": 0.6, "range_n": 6, "vol_n": 6, "vol_z_min": 0.0,'
    ' "ema_span": 5, "rv_n": 5, "max_cost_r": 0.5}'
)


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "data"
    write_lake(d, [(Market.SPOT, "BTCUSDT", synthetic_rows(ms(2024, 3, 1), ms(2024, 3, 6)))])
    monkeypatch.setenv("TSC_DATA_DIR", str(d))

    def reader(data: Path, guard: HoldoutGuard) -> LakeReader:
        return LakeReader.open(data, guard, JsonLinesCodec())

    monkeypatch.setattr(backtest_cli, "_default_reader", reader)
    cfg = tmp_path / "bt.yaml"
    cfg.write_text(
        "n_resamples: 2000\ninstruments:\n  spot:\n    default: "
        "{tick_size: '0.01', lot_size: '0.0001', min_notional: '5'}\n",
        encoding="utf-8",
    )
    return d


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, object]:
    code = main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_cli_run_show_list_trials(
    data_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = str(tmp_path / "bt.yaml")
    code, out = _run(
        capsys,
        "backtest", "run", "--strategy", "bracket_reference", "--params", PARAMS,
        "--symbols", "spot:BTCUSDT", "--from", "2024-03-03", "--to", "2024-03-06",
        "--seed", "7", "--config", cfg,
    )  # fmt: skip
    assert code == 0
    assert isinstance(out, dict)
    assert out["ok"] is True
    assert out["provenance"] == "BACKTEST"
    assert out["n_trials"] == 1
    run_id = out["run_id"]
    assert (data_dir / "runs" / run_id / "manifest.json").is_file()
    code, shown = _run(capsys, "backtest", "show", run_id)
    assert code == 0
    assert isinstance(shown, dict)
    assert shown["run_id"] == run_id
    assert shown["metrics_verified"] is True
    assert shown["evidence_grade"] == out["evidence_grade"]
    code, full = _run(capsys, "backtest", "show", run_id, "--full")
    assert isinstance(full, dict)
    assert full["manifest"]["seed"] == 7
    code, listed = _run(capsys, "backtest", "list")
    assert isinstance(listed, list)
    assert [r["run_id"] for r in listed] == [run_id]
    code, trials = _run(capsys, "backtest", "trials")
    assert isinstance(trials, list)
    assert len(trials) == 1
    code, _ = _run(capsys, "backtest", "show", "bt-nope")
    assert code == 2


def test_cli_refuses_holdout(
    data_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _run(
        capsys,
        "backtest", "run", "--strategy", "bracket_reference", "--symbols", "BTCUSDT",
        "--from", "2026-02-01", "--to", "2026-02-03", "--config", str(tmp_path / "bt.yaml"),
    )  # fmt: skip
    assert code == 3
    assert isinstance(out, dict)
    assert out["refused"] == "holdout"


def test_time_and_symbol_parsing() -> None:
    assert backtest_cli.parse_time("2024-03-01") == ms(2024, 3, 1)
    assert backtest_cli.parse_time("2024-03-01T12:00:00Z") == ms(2024, 3, 1, 12)
    assert backtest_cli.parse_symbols("spot:btcusdt, ETHUSDT", "usdm") == [
        (Market.SPOT, "BTCUSDT"),
        (Market.USDM, "ETHUSDT"),
    ]
    with pytest.raises(ValueError, match="no symbols"):
        backtest_cli.parse_symbols(" , ", "spot")
