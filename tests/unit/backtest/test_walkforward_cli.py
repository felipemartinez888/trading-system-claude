"""`tsclaude backtest walkforward` through the real CLI entry point (refusals and a full run)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from bt_helpers import FAST_PARAMS, JsonLinesCodec, ms, synthetic_rows, write_lake

from tsclaude.cli import main
from tsclaude.contracts.market import Market
from tsclaude.research import backtest_cli
from tsclaude.store.holdout import HoldoutGuard
from tsclaude.store.lake_reader import LakeReader


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    d = tmp_path / "data"
    # a month of data before the first possible anchor: the runs' warm-up lies inside the data
    write_lake(d, [(Market.SPOT, "BTCUSDT", synthetic_rows(ms(2023, 12, 1), ms(2024, 3, 1)))])
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
    grid = tmp_path / "grid.json"
    base = dataclasses.asdict(FAST_PARAMS)
    grid.write_text(json.dumps([{**base, "tp_r": 1.0}, {**base, "tp_r": 0.5}]), encoding="utf-8")
    return {"data": d, "cfg": cfg, "grid": grid}


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, object]]:
    code = main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def _argv(env: dict[str, Path], *extra: str) -> list[str]:
    return [
        "backtest", "walkforward", "--strategy", "bracket_reference",
        "--grid", str(env["grid"]), "--symbols", "spot:BTCUSDT",
        "--hypothesis", "WF-CLI", "--config", str(env["cfg"]), *extra,
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (("--from", "2025-01-01", "--to", "2025-07-01"), "development segment only"),
        (("--from", "2024-10-01", "--to", "2025-04-01"), "development segment only"),
        (("--max-configs", "1"), "exceed the trial budget"),
        (("--from", "2024-01-15", "--to", "2024-03-01"), "not a UTC month start"),
        (("--embargo-minutes", "0"), "at least one minute"),
        # WF-2: the data starts 2023-12-01 and the warm-up is about 20 hours
        (("--from", "2023-12-01", "--to", "2024-03-01"), "earliest valid anchor is 2024-01-01"),
        (("--symbols", "spot:ETHUSDT"), "no stored 1m bars"),
    ],
)
def test_refusals_happen_before_anything_runs(
    env: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    extra: tuple[str, ...],
    message: str,
) -> None:
    code, out = _run(capsys, *_argv(env, *extra))
    assert code == 2
    assert out["ok"] is False
    assert message in str(out["error"])
    assert not (env["data"] / "runs").exists()  # no run was recorded
    assert not (env["data"] / "walkforward").exists()


def test_a_malformed_grid_is_refused(
    env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    env["grid"].write_text('{"a": 1}', encoding="utf-8")
    code, out = _run(capsys, *_argv(env))
    assert code == 2
    assert "grid must be" in str(out["error"])


def test_a_grid_with_an_unknown_parameter_is_refused(
    env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    env["grid"].write_text(json.dumps([{"no_such_param": 1}]), encoding="utf-8")
    code, out = _run(capsys, *_argv(env))
    assert code == 2
    assert out["ok"] is False
    assert "no_such_param" in str(out["error"])
    assert not (env["data"] / "runs").exists()


def test_a_full_walk_forward_writes_its_report(
    env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """Without --from the anchor is the first month start whose warm-up is in the data."""
    code, out = _run(
        capsys,
        *_argv(
            env,
            "--to", "2024-03-01",
            "--initial-train-months", "1", "--test-months", "1", "--n-min", "5",
        ),
    )  # fmt: skip
    assert code == 0
    assert out["warmup"]["anchor"] == "2024-01-01T00:00:00Z"  # type: ignore[index]
    assert out["ok"] is True
    assert out["folds"] == 1
    assert out["n_train_evaluations"] == 2
    assert out["provenance"] == "BACKTEST"
    report = json.loads(Path(str(out["report"])).read_text(encoding="utf-8"))
    assert report["oos"]["verdict"] == out["oos"]["verdict"]  # type: ignore[index]
    assert "Validation (2025" in report["oos"]["not_validation"]
