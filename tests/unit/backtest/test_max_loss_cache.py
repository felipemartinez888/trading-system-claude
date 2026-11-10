"""Verified max-loss cache (audit WF-4): identical results, verified entries, fail-closed keys."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from bt_helpers import TEST_CONFIG, fast_strategy, make_env, ms, request, synthetic_rows, write_lake

from tsclaude.contracts.market import Market
from tsclaude.research import runner
from tsclaude.research.max_loss_cache import MaxLossCache
from tsclaude.research.runner import RunEnv, RunOutcome, run_backtest

START, END = ms(2024, 3, 1), ms(2024, 3, 15)
LO, HI = ms(2024, 3, 3), ms(2024, 3, 8)


def _env(tmp_path: Path, **kw: Any) -> RunEnv:
    write_lake(tmp_path, [(Market.SPOT, "BTCUSDT", synthetic_rows(START, END))])
    env = make_env(tmp_path)
    return dataclasses.replace(env, max_loss_cache=MaxLossCache(tmp_path / "cache"), **kw)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Counts the real development-segment measurements."""
    seen: list[int] = []
    real = runner.measure_max_loss

    def spy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        seen.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(runner, "measure_max_loss", spy)
    return seen


def _hashes(out: RunOutcome) -> tuple[str, str, str]:
    return (
        out.manifest["manifest_hash"],
        out.metrics_hash,
        out.manifest["outputs"]["trades_parquet_sha256"],
    )


def test_a_cache_hit_gives_the_same_hashes_as_no_cache(tmp_path: Path, calls: list[int]) -> None:
    plain = run_backtest(request(LO, HI), make_env(_lake(tmp_path / "plain")), TEST_CONFIG)
    env = _env(tmp_path / "cached")
    first = run_backtest(request(LO, HI), env, TEST_CONFIG)
    again = run_backtest(request(LO, HI), env, TEST_CONFIG)
    assert first.manifest["max_loss_cache"] == "miss"
    assert again.manifest["max_loss_cache"] == "hit"
    assert plain.manifest["max_loss_cache"] == "off"
    assert _hashes(first) == _hashes(again) == _hashes(plain)
    assert len(calls) == 2  # the plain run and the first cached run; the hit measured nothing
    assert len(list((tmp_path / "cached" / "cache").glob("*.json"))) == 1


def _lake(path: Path) -> Path:
    write_lake(path, [(Market.SPOT, "BTCUSDT", synthetic_rows(START, END))])
    return path


def test_one_entry_serves_every_window_of_a_configuration(tmp_path: Path, calls: list[int]) -> None:
    env = _env(tmp_path)
    a = run_backtest(request(LO, HI), env, TEST_CONFIG)
    b = run_backtest(request(ms(2024, 3, 8), ms(2024, 3, 14)), env, TEST_CONFIG)
    assert (a.manifest["max_loss_cache"], b.manifest["max_loss_cache"]) == ("miss", "hit")
    assert a.manifest["max_loss"] == b.manifest["max_loss"]
    assert len(calls) == 1
    other = run_backtest(request(LO, HI, strategy=fast_strategy(tp_r=0.7)), env, TEST_CONFIG)
    assert other.manifest["max_loss_cache"] == "miss"  # another configuration, another key
    assert len(calls) == 2


@pytest.mark.parametrize(
    "code",
    [{"commit": "test", "dirty": True}, {"commit": "unknown", "dirty": None}, {}],
)
def test_unidentified_code_is_never_cached(
    tmp_path: Path, calls: list[int], code: dict[str, Any]
) -> None:
    env = _env(tmp_path, code_version=lambda: dict(code))
    for _ in range(2):
        out = run_backtest(request(LO, HI), env, TEST_CONFIG)
        assert out.manifest["max_loss_cache"] == "off"
    assert len(calls) == 2
    assert not (tmp_path / "cache").exists()


def test_a_tampered_entry_is_a_miss_and_is_replaced(tmp_path: Path, calls: list[int]) -> None:
    env = _env(tmp_path)
    first = run_backtest(request(LO, HI), env, TEST_CONFIG)
    (entry,) = (tmp_path / "cache").glob("*.json")
    data = json.loads(entry.read_text(encoding="utf-8"))
    data["value"]["value"] = 0.01  # a smaller loss bound would loosen certification
    entry.write_text(json.dumps(data), encoding="utf-8")
    again = run_backtest(request(LO, HI), env, TEST_CONFIG)
    assert again.manifest["max_loss_cache"] == "miss"
    assert _hashes(again) == _hashes(first)
    assert len(calls) == 2
    third = run_backtest(request(LO, HI), env, TEST_CONFIG)
    assert third.manifest["max_loss_cache"] == "hit"  # the fresh measurement replaced it
    entry.write_text("{not json", encoding="utf-8")
    assert run_backtest(request(LO, HI), env, TEST_CONFIG).manifest["max_loss_cache"] == "miss"


def test_new_lake_data_in_the_development_segment_changes_the_key(
    tmp_path: Path, calls: list[int]
) -> None:
    env = _env(tmp_path)
    run_backtest(request(LO, HI), env, TEST_CONFIG)
    write_lake(tmp_path, [(Market.SPOT, "BTCUSDT", synthetic_rows(ms(2024, 1, 1), ms(2024, 1, 3)))])
    env.reader.refresh()
    out = run_backtest(request(LO, HI), env, TEST_CONFIG)
    assert out.manifest["max_loss_cache"] == "miss"
    assert len(calls) == 2
    assert len(list((tmp_path / "cache").glob("*.json"))) == 2


def test_a_preregistration_override_is_applied_after_the_cache(
    tmp_path: Path, calls: list[int]
) -> None:
    env = _env(tmp_path)
    base = run_backtest(request(LO, HI), env, TEST_CONFIG)
    measured = float(base.manifest["max_loss"]["value"])
    raised = run_backtest(request(LO, HI, max_loss_r_override=measured + 1.0), env, TEST_CONFIG)
    assert raised.manifest["max_loss_cache"] == "hit"
    assert raised.manifest["max_loss"]["value"] == measured + 1.0
    assert "preregistration override" in raised.manifest["max_loss"]["source"]
    (entry,) = (tmp_path / "cache").glob("*.json")
    assert json.loads(entry.read_text(encoding="utf-8"))["value"]["value"] == measured
    with pytest.raises(ValueError, match="may only raise"):
        run_backtest(request(LO, HI, max_loss_r_override=measured / 2), env, TEST_CONFIG)
    assert len(calls) == 1
