"""M2-E1 runner integration: registered exchange interruptions (ADR-0013, D-09/D-10) are reported as
downtime, never as missing data; every other missing minute keeps the zero-tolerance rule.
Synthetic lake only (no historical research run)."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
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


def iso(t: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(t / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def registry(
    lo: int,
    hi: int,
    market: str = "spot",
    entry_id: str = "test-halt",
    absent_from: int | None = None,
) -> Any:
    data = {
        "schema": 2,
        "interruptions": [
            {
                "id": entry_id,
                "market": market,
                "from": iso(lo),
                "to": iso(hi),
                "absent_from": iso(lo if absent_from is None else absent_from),
                "scope": "exchange-wide",
                "signature": {"symbols_checked": ["BTCUSDT"]},
                "external_sources": ["test"],
                "approved_by": "test",
                "approved_at": "2026-10-28",
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
    assert manifest["runner_version"] == RUNNER_VERSION == "runner-v3"
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


def test_missing_minutes_before_absent_from_are_genuine_missing_data(tmp_path: Path) -> None:
    """D-10A: the interval covers all 30 absent minutes, but its documented absence period
    starts 10 minutes in. Those first 10 minutes should have been published, so their absence is
    a data defect: missing, grade X; only the 20 documented minutes are downtime."""
    reg = registry(GAP_START, GAP_START + 30 * MIN, absent_from=GAP_START + 10 * MIN)
    out = _run(tmp_path, reg)
    integrity = out.metrics["integrity"]
    assert integrity["exchange_downtime_minutes_in_range"] == 20
    assert integrity["missing_minutes_in_range"] == 10
    assert integrity["exchange_interruptions_in_range"] == ["test-halt"]
    assert out.metrics["evidence_grade"] == "X"
    assert any("data gaps: 10 missing" in r for r in out.metrics["grade_reasons"])


def test_observed_fillers_then_documented_absence_is_downtime_only(tmp_path: Path) -> None:
    """The 2023-03-24 shape: zero-trade fillers published first (kept as observed), then the
    documented absence. Fillers count as zero-trade minutes, the absence as downtime, nothing
    as missing, and no candle is synthesized."""
    rows = synthetic_rows(START, END)
    fillers = {GAP_START + i * MIN for i in range(10)}
    absent = {GAP_START + i * MIN for i in range(10, 30)}
    prev = None
    kept = []
    for r in rows:
        if r["open_time_ms"] in absent:
            continue
        if r["open_time_ms"] in fillers and prev is not None:
            flat = prev["close"]
            r.update(open=flat, high=flat, low=flat, close=flat, trades=0)
            r.update(volume=0.0, quote_volume=0.0, taker_buy_volume=0.0, taker_buy_quote_volume=0.0)
        kept.append(r)
        prev = r
    d = tmp_path / "lake"
    write_lake(d, [(Market.SPOT, "BTCUSDT", kept)])
    reg = registry(GAP_START, GAP_START + 30 * MIN, absent_from=GAP_START + 10 * MIN)
    env = dataclasses.replace(make_env(d), exchange_interruptions=reg)
    out = run_backtest(request(FROM, TO), env, TEST_CONFIG)
    integrity = out.metrics["integrity"]
    assert integrity["exchange_downtime_minutes_in_range"] == 20
    assert integrity["zero_trade_minutes_in_range"] == 10
    assert integrity["missing_minutes_in_range"] == 0
    # the registry neither adds nor removes candles: the same lake without it reads the same bars
    plain = run_backtest(request(FROM, TO), make_env(d), TEST_CONFIG)
    assert integrity["bars_read"] == plain.metrics["integrity"]["bars_read"]
    assert out.metrics["evidence_grade"] != "X"


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
        "schema": 2,
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
    halt = reg.entries[0]
    assert halt.absent_from_ms == ms(2023, 3, 24, 12, 39)
    assert halt.approved_at == "2026-10-28"
    assert halt.override == ()
    # D-10A: 11:28-12:38 was published (fillers), so a minute missing there is missing data
    assert reg.split_missing(halt.market, ms(2023, 3, 24, 11, 30), ms(2023, 3, 24, 11, 40)) == (
        0,
        10,
        [],
    )
    # the simulator still freezes the whole interval, observed fillers included
    assert reg.simulator_intervals() == (
        (halt.market, ms(2023, 3, 24, 11, 28), ms(2023, 3, 24, 14, 0), "binance-spot-2023-03-24"),
    )
    assert reg.manifest()["schema"] == 2
    assert reg.manifest()["entries"][0]["absent_from_ms"] == halt.absent_from_ms


LONG = {
    "from": "2023-03-24T00:00:00Z",
    "to": "2023-03-26T00:00:00Z",
    "absent_from": "2023-03-24T00:00:00Z",
}
OVERRIDE = {
    "approved_by": "owner",
    "approved_at": "2026-10-29",
    "reason": "multi-day exchange outage",
    "reference": "docs/devlog/example.md",
}


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
        ({"approved_at": None}, "approved_at"),
        ({"approved_at": "28/10/2026"}, "approved_at"),
        ({"approved_at": "2026-13-01"}, "approved_at"),
        ({"approved_at": "2026-10-28T00:00:00Z"}, "approved_at"),
        ({"approved_at": 20261028}, "approved_at"),
        ({"absent_from": None}, "absent_from"),
        ({"absent_from": "2023-03-24T11:00:00Z"}, "absent_from"),  # before from
        ({"absent_from": "2023-03-24T14:00:00Z"}, "absent_from"),  # == to
        ({"absent_from": "2023-03-24T12:39:30Z"}, "minute-aligned"),
        ({"absent_form": "2023-03-24T12:39:00Z"}, "unknown field"),  # a typo is refused
        ({"exceptional_override": "yes"}, "mapping"),
        # review B-1: evidence and approval fields are typed, never just truthy
        ({"approved_by": "   "}, "approved_by"),
        ({"approved_by": True}, "approved_by"),
        ({"approved_by": 123}, "approved_by"),
        ({"approved_by": ["a"]}, "approved_by"),
        ({"id": "   "}, "non-blank id"),
        ({"id": " x"}, "non-blank id"),
        ({"external_sources": "x"}, "external_sources"),
        ({"external_sources": ["  "]}, "external_sources"),
        ({"external_sources": ["ok", 3]}, "external_sources"),
        ({"signature": True}, "signature"),
        ({"signature": ["BTCUSDT"]}, "signature"),
        # review N-2 and date formats: a time is required; only YYYY-MM-DD approval dates
        ({"from": "2023-03-24Z"}, "timestamp"),
        ({"approved_at": "2026-W44-3"}, "approved_at"),
        ({"approved_at": datetime(2026, 10, 28)}, "approved_at"),  # a datetime, not a date
        ({"approved_at": False}, "approved_at"),
        # review N-3: an override is only for intervals longer than 24 hours
        ({"exceptional_override": OVERRIDE}, "only for intervals longer"),
    ],
)
def test_an_invalid_registry_fails_closed(change: dict[str, Any], message: str) -> None:
    entry = _entry()
    entry.update(change)
    with pytest.raises(InterruptionRegistryError, match=message):
        parse_registry({"schema": 2, "interruptions": [entry]}, sha256=None, source=None)


def _entry(**over: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": "x",
        "market": "spot",
        "from": "2023-03-24T11:28:00Z",
        "to": "2023-03-24T14:00:00Z",
        "absent_from": "2023-03-24T12:39:00Z",
        "scope": "exchange-wide",
        "signature": {"a": 1},
        "external_sources": ["s"],
        "approved_by": "owner",
        "approved_at": "2026-10-28",
    }
    entry.update(over)
    return entry


def _parse(*entries: dict[str, Any]) -> Any:
    return parse_registry({"schema": 2, "interruptions": list(entries)}, sha256=None, source=None)


def test_the_24_hour_limit_is_inclusive_and_longer_intervals_fail_closed() -> None:
    day = _entry(**{"from": "2023-03-24T00:00:00Z", "to": "2023-03-25T00:00:00Z",
                    "absent_from": "2023-03-24T00:00:00Z"})  # fmt: skip
    assert _parse(day).entries[0].to_ms - _parse(day).entries[0].from_ms == 24 * 60 * MIN
    longer = {**day, "to": "2023-03-25T00:01:00Z"}
    with pytest.raises(InterruptionRegistryError, match="24 hours"):
        _parse(longer)
    accepted = _parse({**longer, "exceptional_override": OVERRIDE}).entries[0]
    assert dict(accepted.override)["reference"] == "docs/devlog/example.md"


@pytest.mark.parametrize("missing", ["approved_by", "approved_at", "reason", "reference"])
def test_an_incomplete_exceptional_override_fails_closed(missing: str) -> None:
    longer = _entry(**{"from": "2023-03-24T00:00:00Z", "to": "2023-03-26T00:00:00Z",
                       "absent_from": "2023-03-24T00:00:00Z"})  # fmt: skip
    for bad in (None, "", "   "):
        override = {**OVERRIDE, missing: bad}
        with pytest.raises(InterruptionRegistryError, match=missing):
            _parse({**longer, "exceptional_override": override})
    without = {k: v for k, v in OVERRIDE.items() if k != missing}
    with pytest.raises(InterruptionRegistryError, match=missing):
        _parse({**longer, "exceptional_override": without})


def test_unknown_override_or_registry_fields_fail_closed() -> None:
    longer = _entry(**{"from": "2023-03-24T00:00:00Z", "to": "2023-03-26T00:00:00Z",
                       "absent_from": "2023-03-24T00:00:00Z"})  # fmt: skip
    with pytest.raises(InterruptionRegistryError, match="unknown exceptional_override"):
        _parse({**longer, "exceptional_override": {**OVERRIDE, "note": "x"}})
    with pytest.raises(InterruptionRegistryError, match="unknown registry field"):
        parse_registry({"schema": 2, "interruptions": [], "extra": 1}, sha256=None, source=None)
    with pytest.raises(InterruptionRegistryError, match="schema"):
        parse_registry({"schema": 1, "interruptions": [_entry()]}, sha256=None, source=None)


def test_every_policy_field_is_reflected_in_the_manifest_and_the_file_hash(tmp_path: Path) -> None:
    base = _parse(_entry()).manifest()
    for change in ({"approved_at": "2026-10-27"}, {"absent_from": "2023-03-24T12:40:00Z"}):
        assert _parse(_entry(**change)).manifest() != base, change
    longer = _entry(**LONG)
    with_override = _parse({**longer, "exceptional_override": OVERRIDE}).manifest()
    assert with_override["entries"][0]["exceptional_override"] == OVERRIDE  # every field
    other = {**OVERRIDE, "reason": "another reason"}
    assert _parse({**longer, "exceptional_override": other}).manifest() != with_override
    import yaml

    shas = []
    for approved in ("2026-10-28", "2026-10-27"):
        f = tmp_path / f"r{approved}.yaml"
        f.write_text(
            yaml.safe_dump({"schema": 2, "interruptions": [_entry(approved_at=approved)]}),
            encoding="utf-8",
        )
        shas.append(load_registry(f).sha256)
    assert shas[0] != shas[1]


def test_an_invalid_override_approval_date_fails_closed() -> None:
    override = {**OVERRIDE, "approved_at": "2026-13-01"}
    with pytest.raises(InterruptionRegistryError, match=r"exceptional_override.approved_at"):
        _parse({**_entry(**LONG), "exceptional_override": override})


def test_a_duplicate_yaml_key_fails_closed(tmp_path: Path) -> None:
    """Review N-1: PyYAML would silently keep the last value; a reviewer reading the file would
    see the first. A second `absent_from` would hide the published-filler minutes."""
    text = Path("configs/exchange_interruptions.yaml").read_text(encoding="utf-8")
    marker = '    absent_from: "2023-03-24T12:39:00Z"'
    assert marker in text
    doubled = text.replace(marker, marker + '\n    absent_from: "2023-03-24T11:28:00Z"')
    f = tmp_path / "dup.yaml"
    f.write_text(doubled, encoding="utf-8")
    with pytest.raises(InterruptionRegistryError, match="duplicate key"):
        load_registry(f)


def test_a_non_integer_schema_fails_closed() -> None:
    for schema in (2.0, "2", True):
        with pytest.raises(InterruptionRegistryError, match="schema"):
            parse_registry({"schema": schema, "interruptions": []}, sha256=None, source=None)


def test_an_unquoted_yaml_date_is_accepted_as_the_approval_date(tmp_path: Path) -> None:
    f = tmp_path / "r.yaml"
    text = Path("configs/exchange_interruptions.yaml").read_text(encoding="utf-8")
    f.write_text(
        text.replace('approved_at: "2026-10-28"', "approved_at: 2026-10-28"), encoding="utf-8"
    )
    assert load_registry(f).entries[0].approved_at == "2026-10-28"


def test_duplicate_or_overlapping_entries_fail_closed() -> None:
    a = _entry(**{"id": "a", "from": "2023-03-24T11:00:00Z", "to": "2023-03-24T12:00:00Z",
                  "absent_from": "2023-03-24T11:00:00Z"})  # fmt: skip
    b = _entry(**{"id": "b", "from": "2023-03-24T11:30:00Z", "to": "2023-03-24T12:30:00Z",
                  "absent_from": "2023-03-24T11:30:00Z"})  # fmt: skip
    with pytest.raises(InterruptionRegistryError, match="overlap"):
        _parse(a, b)
    with pytest.raises(InterruptionRegistryError, match="duplicate"):
        _parse(a, {**a})
    usdm = {**b, "market": "usdm"}  # another market may overlap
    assert len(_parse(a, usdm).entries) == 2


def test_a_missing_registry_file_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(InterruptionRegistryError, match="cannot read"):
        load_registry(tmp_path / "nope.yaml")
    (tmp_path / "bad.yaml").write_text("schema: [", encoding="utf-8")
    with pytest.raises(InterruptionRegistryError, match="not valid YAML"):
        load_registry(tmp_path / "bad.yaml")
    assert json.dumps(EMPTY_REGISTRY.manifest())  # serializable
