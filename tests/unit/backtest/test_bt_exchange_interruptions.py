"""M2-E1 runner integration: registered exchange interruptions (ADR-0013, D-09/D-10) are reported as
downtime, never as missing data; every other missing minute keeps the zero-tolerance rule.
Synthetic lake only (no historical research run)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from bt_helpers import MIN, make_env, ms, request, synthetic_rows, write_lake
from test_bt_runner import END, FROM, START, TEST_CONFIG, TO, _lake

from tsclaude.contracts.market import Market
from tsclaude.quality.interruptions import (
    EMPTY_REGISTRY,
    InterruptionRegistryError,
    load_registry,
    parse_registry,
)
from tsclaude.research.parquet import read_table
from tsclaude.research.runner import REQUIRED_MANIFEST_FIELDS, RUNNER_VERSION, run_backtest

GAP_START = ms(2024, 3, 5, 12)
GAP = [GAP_START + i * MIN for i in range(30)]


def registry(lo: int, hi: int, market: str = "spot", entry_id: str = "test-halt") -> Any:
    def iso(t: int) -> str:
        from datetime import UTC, datetime

        return datetime.fromtimestamp(t / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    data = {
        "schema": 1,
        "interruptions": [
            {
                "id": entry_id,
                "market": market,
                "from": iso(lo),
                "to": iso(hi),
                "scope": "exchange-wide",
                "signature": {"symbols_checked": ["BTCUSDT"]},
                "external_sources": ["test"],
                "approved_by": "test",
            }
        ],
    }
    return parse_registry(data, sha256=f"sha-{entry_id}-{lo}-{hi}", source="test")


def _run(tmp_path: Path, reg: Any) -> Any:
    d = _lake(tmp_path / "lake", skip=GAP)
    env = dataclasses.replace(make_env(d), exchange_interruptions=reg)
    return run_backtest(request(FROM, TO), env, TEST_CONFIG)


def test_a_registered_interruption_is_downtime_not_missing_data(tmp_path: Path) -> None:
    out = _run(tmp_path, registry(GAP_START, GAP_START + 30 * MIN))
    integrity = out.metrics["integrity"]
    assert integrity["missing_minutes_in_range"] == 0
    assert integrity["exchange_downtime_minutes_in_range"] == 30
    assert integrity["exchange_interruptions_in_range"] == ["test-halt"]
    assert not any("data gaps" in r for r in out.metrics["grade_reasons"])
    assert out.metrics["evidence_grade"] != "X"
    assert integrity["exchange_interruptions_in_warmup"] == []
    manifest = out.manifest
    assert manifest["runner_version"] == RUNNER_VERSION == "runner-v2"
    assert manifest["sim_engine_version"] == "sim-v1"
    assert (
        manifest["exchange_interruptions"]["sha256"]
        == f"sha-test-halt-{GAP_START}-{GAP_START + 30 * MIN}"
    )
    assert "exchange_interruptions" in REQUIRED_MANIFEST_FIELDS
    schema, _ = read_table(out.run_dir / "trades.parquet")
    names = [n for n, _ in schema]
    assert {"deferred_no_trade", "spans_exchange_downtime", "spans_gap"} <= set(names)


def test_registered_minutes_in_the_warm_up_are_downtime_too(tmp_path: Path) -> None:
    warm = FROM - 120 * MIN  # inside the warm-up read before 'from'
    d = _lake(tmp_path / "lake", skip=[warm + i * MIN for i in range(15)])
    reg = registry(warm, warm + 15 * MIN)
    env = dataclasses.replace(make_env(d), exchange_interruptions=reg)
    out = run_backtest(request(FROM, TO), env, TEST_CONFIG)
    integrity = out.metrics["integrity"]
    assert integrity["missing_minutes_in_warmup"] == 0
    assert integrity["exchange_downtime_minutes_in_warmup"] == 15
    assert integrity["exchange_interruptions_in_warmup"] == ["test-halt"]
    assert integrity["exchange_interruptions_in_range"] == []
    assert not any("warm-up" in r for r in out.metrics["grade_reasons"])
    assert out.metrics["evidence_grade"] != "X"


def test_an_unregistered_warm_up_gap_still_grades_x(tmp_path: Path) -> None:
    warm = FROM - 120 * MIN
    d = _lake(tmp_path / "lake", skip=[warm + i * MIN for i in range(15)])
    out = run_backtest(request(FROM, TO), make_env(d), TEST_CONFIG)
    assert out.metrics["integrity"]["missing_minutes_in_warmup"] == 15
    assert out.metrics["evidence_grade"] == "X"


def test_minutes_outside_the_registered_interval_still_grade_x(tmp_path: Path) -> None:
    out = _run(tmp_path, registry(GAP_START, GAP_START + 20 * MIN))  # covers 20 of 30
    integrity = out.metrics["integrity"]
    assert integrity["exchange_downtime_minutes_in_range"] == 20
    assert integrity["missing_minutes_in_range"] == 10
    assert out.metrics["evidence_grade"] == "X"
    assert any("data gaps: 10 missing" in r for r in out.metrics["grade_reasons"])


def test_an_interruption_of_another_market_does_not_apply(tmp_path: Path) -> None:
    out = _run(tmp_path, registry(GAP_START, GAP_START + 30 * MIN, market="usdm"))
    assert out.metrics["integrity"]["missing_minutes_in_range"] == 30
    assert out.metrics["integrity"]["exchange_interruptions_in_range"] == []
    assert out.metrics["evidence_grade"] == "X"


def test_the_run_environment_defaults_to_the_empty_registry(tmp_path: Path) -> None:
    assert make_env(_lake(tmp_path / "lake")).exchange_interruptions is EMPTY_REGISTRY


def test_without_a_registry_the_gap_still_grades_x_and_the_manifest_says_so(tmp_path: Path) -> None:
    out = _run(tmp_path, EMPTY_REGISTRY)
    assert out.metrics["evidence_grade"] == "X"
    assert out.manifest["exchange_interruptions"] == {
        "schema": 1,
        "source": None,
        "sha256": None,
        "entries": [],
    }


def test_a_different_registry_changes_the_manifest_hash(tmp_path: Path) -> None:
    a = _run(tmp_path / "a", registry(GAP_START, GAP_START + 30 * MIN, entry_id="v1"))
    b = _run(tmp_path / "b", registry(GAP_START, GAP_START + 30 * MIN, entry_id="v2"))
    assert a.manifest["manifest_hash"] != b.manifest["manifest_hash"]


def test_zero_trade_minutes_in_range_are_reported_per_symbol_and_never_counted_missing(
    tmp_path: Path,
) -> None:
    """ADR-0014 rule 5: published zero-trade candles are real observations, reported as a share
    of the evaluated range; they are never missing data and never grade X."""
    rows = synthetic_rows(START, END)
    zero_at = {GAP_START + i * MIN for i in range(4)} | {FROM - 10 * MIN}  # 4 in range, 1 warm-up
    prev = None
    for r in rows:
        if r["open_time_ms"] in zero_at and prev is not None:
            flat = prev["close"]
            r.update(open=flat, high=flat, low=flat, close=flat, trades=0)
            r.update(volume=0.0, quote_volume=0.0, taker_buy_volume=0.0, taker_buy_quote_volume=0.0)
        prev = r
    d = tmp_path / "lake"
    write_lake(d, [(Market.SPOT, "BTCUSDT", rows)])
    out = run_backtest(request(FROM, TO), make_env(d), TEST_CONFIG)
    integrity = out.metrics["integrity"]
    assert integrity["zero_trade_minutes_in_range"] == 4
    assert integrity["zero_trade_minutes_by_symbol"] == {"spot:BTCUSDT": 4}
    assert integrity["zero_trade_share"] == pytest.approx(4 / ((TO - FROM) // MIN))
    assert integrity["missing_minutes_in_range"] == 0
    assert out.metrics["evidence_grade"] != "X"


# ---------------------------------------------------------------------- the registry itself
def test_the_repository_registry_holds_exactly_the_approved_2023_halt() -> None:
    reg = load_registry(Path("configs/exchange_interruptions.yaml"))
    assert [(e.id, str(e.market), e.from_ms, e.to_ms) for e in reg.entries] == [
        ("binance-spot-2023-03-24", "spot", ms(2023, 3, 24, 11, 28), ms(2023, 3, 24, 14, 0))
    ]
    assert reg.sha256 is not None
    assert reg.source == "exchange_interruptions.yaml"  # file name only, never a local path
    inside, outside, ids = reg.split_missing(
        reg.entries[0].market, ms(2023, 3, 24, 12, 39), ms(2023, 3, 24, 14, 0)
    )
    assert (inside, outside, ids) == (81, 0, ["binance-spot-2023-03-24"])


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"scope": "single-symbol"}, "scope"),
        ({"approved_by": ""}, "approved_by"),
        ({"external_sources": []}, "external_sources"),
        ({"signature": None}, "signature"),
        ({"market": "coinm"}, "unknown market"),
        ({"from": "2023-03-24T11:28:30Z"}, "minute-aligned"),
        ({"to": "2023-03-24T11:00:00Z"}, "after"),
        ({"from": "2023-03-24 11:28"}, "ending in Z"),
    ],
)
def test_an_invalid_registry_fails_closed(change: dict[str, Any], message: str) -> None:
    entry: dict[str, Any] = {
        "id": "x",
        "market": "spot",
        "from": "2023-03-24T11:28:00Z",
        "to": "2023-03-24T14:00:00Z",
        "scope": "exchange-wide",
        "signature": {"a": 1},
        "external_sources": ["s"],
        "approved_by": "owner",
    }
    entry.update(change)
    with pytest.raises(InterruptionRegistryError, match=message):
        parse_registry({"schema": 1, "interruptions": [entry]}, sha256=None, source=None)


def test_duplicate_or_overlapping_entries_fail_closed() -> None:
    base = {
        "market": "spot", "scope": "exchange-wide", "signature": {"a": 1},
        "external_sources": ["s"], "approved_by": "owner",
    }  # fmt: skip
    a = {**base, "id": "a", "from": "2023-03-24T11:00:00Z", "to": "2023-03-24T12:00:00Z"}
    b = {**base, "id": "b", "from": "2023-03-24T11:30:00Z", "to": "2023-03-24T12:30:00Z"}
    with pytest.raises(InterruptionRegistryError, match="overlap"):
        parse_registry({"schema": 1, "interruptions": [a, b]}, sha256=None, source=None)
    with pytest.raises(InterruptionRegistryError, match="duplicate"):
        parse_registry({"schema": 1, "interruptions": [a, {**a}]}, sha256=None, source=None)
    with pytest.raises(InterruptionRegistryError, match="schema"):
        parse_registry({"schema": 2, "interruptions": []}, sha256=None, source=None)


def test_a_missing_registry_file_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(InterruptionRegistryError, match="cannot read"):
        load_registry(tmp_path / "nope.yaml")
    (tmp_path / "bad.yaml").write_text("schema: [", encoding="utf-8")
    with pytest.raises(InterruptionRegistryError, match="not valid YAML"):
        load_registry(tmp_path / "bad.yaml")
    assert json.dumps(EMPTY_REGISTRY.manifest())  # serializable
