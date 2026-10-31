"""M2-E2: zero-volume classes (ADR-0014 rule 2), the ADR-0015 open exception and QA report v2.

The five real AVAX/HBAR cases come from `tests/fixtures/archive/e2_open_exception.json`: verbatim
CSV lines cut from the verified archives by `scripts/fixtures/extract_e2_open_cases.py`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import importlib.util
import json
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from arc_helpers import DAY, HEADER, HOUR, MIN, FakeArchive, JsonLinesCodec, kline_values, ms

from tsclaude.cli import build_parser
from tsclaude.contracts.market import Bar, BarSource, Interval, Market
from tsclaude.contracts.timeutil import ManualClock, from_iso, to_iso
from tsclaude.marketdata import datacli
from tsclaude.marketdata.archive import csvparse, qa
from tsclaude.marketdata.archive.importer import (
    ArchiveImportError,
    Importer,
    ImportRequest,
    _bar_row,
)
from tsclaude.marketdata.archive.paths import ArchiveDataType, ArchiveFile, Period
from tsclaude.marketdata.types import RestKline
from tsclaude.quality.interruptions import (
    EMPTY_REGISTRY,
    ExchangeInterruption,
    InterruptionRegistry,
    load_registry,
    parse_registry,
)
from tsclaude.store.lake import Lake, LakeKind
from tsclaude.store.lake_writer import LakeWriter

FIXTURE = Path(__file__).parents[2] / "fixtures" / "archive" / "e2_open_exception.json"
NOW = ms(2026, 10, 20, 12, 30)
SYM = "BTCUSDT"
M15 = 15 * MIN

#: ADR-0015 table: case id -> (derived open, native open)
ADR_TABLE = {
    "AVAXUSDT-2025-12-07T03:45:00Z": (13.48, 13.49),
    "AVAXUSDT-2025-12-29T23:15:00Z": (12.42, 12.41),
    "AVAXUSDT-2025-12-30T18:15:00Z": (12.55, 12.56),
    "HBARUSDT-2024-01-25T18:30:00Z": (0.0712, 0.0711),
    "HBARUSDT-2025-12-14T03:45:00Z": (0.12399, 0.124),
}
#: ADR-0015 table: case id -> (1m line of the first minute of the bucket, 15m line)
ADR_LINES = {
    "AVAXUSDT-2025-12-07T03:45:00Z": (8866, 592),
    "AVAXUSDT-2025-12-29T23:15:00Z": (41716, 2782),
    "AVAXUSDT-2025-12-30T18:15:00Z": (42856, 2858),
    "HBARUSDT-2024-01-25T18:30:00Z": (35671, 2379),
    "HBARUSDT-2025-12-14T03:45:00Z": (18946, 1264),
}
#: ADR-0015 source table
ADR_SHA256 = {
    "AVAXUSDT-1m-2025-12.zip": "6464c58b341850ccc7212f23d82b5604c74944c916c5258936c4f42a1736c1df",
    "AVAXUSDT-15m-2025-12.zip": "1a777574924bf8f055f7318cb39fea6459d6f039b29d161435c7a6973e1b2b36",
    "HBARUSDT-1m-2024-01.zip": "f75724733830d1521895a655b5e682280a3ebead1fb0540c2a1269c099852b3d",
    "HBARUSDT-15m-2024-01.zip": "843f84b2e6faa5fcf5b1618cd34c1f1efcc37dd6dae54cb3050bf8a57a123735",
    "HBARUSDT-1m-2025-12.zip": "3d2ffff772cfb4c8544c59d5865880784398b21a8196a64163d588af3965813c",
    "HBARUSDT-15m-2025-12.zip": "639d2e01a719186e12b3fb4b8013004520a2c4adb419376fdf2fd34b548154dc",
}


def _cases() -> list[dict[str, Any]]:
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
    assert isinstance(cases, list)
    return cases


def _month_of(case: dict[str, Any]) -> tuple[int, int]:
    m = re.search(r"(\d{4})-(\d{2})\.zip$", case["derived_1m"]["file"])
    assert m is not None
    year, month = int(m[1]), int(m[2])
    start = ms(year, month)
    return start, ms(year + 1, 1) if month == 12 else ms(year, month + 1)


def _case_data(case: dict[str, Any]) -> tuple[list[Bar], list[RestKline]]:
    """The fixture's verbatim lines through the real parser and the real `Bar` validation."""
    period = _month_of(case)
    text = "\n".join(r["csv"] for r in case["derived_1m"]["rows"]) + "\n"
    rows = csvparse.parse_klines_csv(text, Market.SPOT, period, MIN).rows
    bars = [_bar_row(Market.SPOT, case["symbol"], r, BarSource.ARCHIVE, NOW)[1] for r in rows]
    native_text = case["native_15m"]["row"]["csv"] + "\n"
    native = csvparse.parse_klines_csv(native_text, Market.SPOT, period, M15).rows
    assert len(bars) == 16  # the candle before the bucket, then its 15 minutes
    assert len(native) == 1
    return bars, list(native)


def _case(index: int = 0) -> tuple[list[Bar], list[RestKline]]:
    return _case_data(_cases()[index])


def _only_mismatch(result: dict[str, Any]) -> dict[str, Any]:
    (entry,) = result["mismatch_samples"]
    assert isinstance(entry, dict)
    return entry


def _bar(t: int, close: float = 100.0, **over: Any) -> Bar:
    values: dict[str, Any] = {
        "market": Market.SPOT,
        "symbol": SYM,
        "interval": Interval.M1,
        "open_time_ms": t,
        "close_time_ms": t + MIN - 1,
        "open": close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": 2.0,
        "quote_volume": 200.0,
        "trades": 10,
        "taker_buy_volume": 1.0,
        "taker_buy_quote_volume": 100.0,
        "source": BarSource.ARCHIVE,
        "ingested_at_ms": NOW,
    }
    return Bar(**{**values, **over})


def _z1(t: int, price: float = 100.0) -> Bar:
    return _bar(
        t,
        price,
        high=price,
        low=price,
        volume=0.0,
        quote_volume=0.0,
        trades=0,
        taker_buy_volume=0.0,
        taker_buy_quote_volume=0.0,
    )


def _entry(
    lo: int, hi: int, absent: int, market: str = "spot", name: str = "halt"
) -> dict[str, Any]:
    def text(t: int) -> str:
        return datetime.fromtimestamp(t / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "id": name,
        "market": market,
        "from": text(lo),
        "to": text(hi),
        "absent_from": text(absent),
        "scope": "exchange-wide",
        "signature": {"symbols_checked": ["BTCUSDT"]},
        "external_sources": ["a report"],
        "approved_by": "owner",
        "approved_at": "2026-10-28",
    }


def _registry(*entries: dict[str, Any]) -> InterruptionRegistry:
    return parse_registry(
        {"schema": 2, "interruptions": list(entries)}, sha256="ab" * 32, source="test.yaml"
    )


T0 = ms(2024, 6, 3, 10, 0)
SPOT_HALT = _registry(_entry(T0 + 5 * MIN, T0 + 15 * MIN, T0 + 10 * MIN))
HALT: Sequence[ExchangeInterruption] = SPOT_HALT.for_market(Market.SPOT)


# ------------------------------------------------------------------ the five real cases
def test_the_fixture_keeps_the_adr_provenance() -> None:
    cases = _cases()
    assert [c["id"] for c in cases] == list(ADR_TABLE)
    for c in cases:
        for src in (c["derived_1m"], c["native_15m"]):
            assert src["sha256"] == ADR_SHA256[src["file"]]
            assert src["csv_member"] == src["file"].removesuffix(".zip") + ".csv"
        lines = [r["line"] for r in c["derived_1m"]["rows"]]
        assert lines == list(range(lines[0], lines[0] + 16))
        first_minute, native_line = ADR_LINES[c["id"]]
        assert lines[1] == first_minute  # lines[0] is the candle before the bucket
        assert c["native_15m"]["row"]["line"] == native_line
    # nothing from the sealed 2026 holdout is in the repository
    text = FIXTURE.read_text(encoding="utf-8")
    assert "2026-" not in text
    assert all(_month_of(c)[1] <= ms(2026, 1) for c in cases)


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["id"])
def test_each_real_case_is_explained(case: dict[str, Any]) -> None:
    bars, native = _case_data(case)
    derived_open, native_open = ADR_TABLE[case["id"]]
    result = qa.cross_check(bars[1:], native, Interval.M15)  # the bucket alone has no previous
    assert result["mismatches"] == 1
    assert result["mismatches_unexplained"] == 1  # candle before the bucket unavailable
    assert not result["ok"]

    result = qa.cross_check(bars, native, Interval.M15)
    entry = _only_mismatch(result)
    assert set(entry["diffs"]) == {"open"}  # H, L, C, volumes and trades agree
    assert entry["diffs"]["open"] == [derived_open, native_open]
    assert entry["explanation"] == {
        "reason": "leading_zero_trade_open",
        "k": 1,
        "filler": derived_open,
        "first_traded_open": native_open,
        "previous_minute": qa.to_iso(from_iso(case["bucket_open"]) - MIN),
        "first_minute": qa.to_iso(from_iso(case["bucket_open"])),
        "first_traded_minute": qa.to_iso(from_iso(case["bucket_open"]) + MIN),
    }
    assert result["mismatches_explained"] == 1
    assert result["mismatches_unexplained"] == 0
    assert result["explained_by_reason"] == {"leading_zero_trade_open": 1}
    assert result["ok"]


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["id"])
def test_the_first_candle_of_each_real_case_is_class_z1(case: dict[str, Any]) -> None:
    bars, _ = _case_data(case)
    classes = qa.classify_zero_trade(bars)
    assert classes.anomaly_count == 0
    assert classes.z1_minutes >= 1
    assert classes.z1_ranges[0][0] == from_iso(case["bucket_open"])


# ------------------------------------------------------------------ the exception stays narrow
def _with(bars: list[Bar], index: int, **over: Any) -> list[Bar]:
    return [b.model_copy(update=over) if i == index else b for i, b in enumerate(bars)]


def _native_with(native: list[RestKline], **over: Any) -> list[RestKline]:
    return [dataclasses.replace(native[0], **over)]


def _assert_unexplained(bars: list[Bar], native: list[RestKline]) -> dict[str, Any]:
    result = qa.cross_check(bars, native, Interval.M15)
    assert result["mismatches"] >= 1
    assert result["mismatches_explained"] == 0
    assert result["mismatches_unexplained"] == result["mismatches"]
    assert result["explained_mismatches"] == []
    assert result["ok"] is False
    return result


@pytest.mark.parametrize("field_name", ["high", "low", "close", "volume", "quote_volume", "trades"])
def test_any_other_differing_field_keeps_the_mismatch_unexplained(field_name: str) -> None:
    bars, native = _case()
    value = getattr(native[0], field_name)
    changed = value + 1 if field_name == "trades" else value * 1.01
    result = _assert_unexplained(bars, _native_with(native, **{field_name: changed}))
    assert field_name in _only_mismatch(result)["diffs"]


@pytest.mark.parametrize("field_name", ["taker_buy_volume", "taker_buy_quote_volume"])
def test_a_taker_volume_beyond_tolerance_keeps_the_mismatch_unexplained(field_name: str) -> None:
    bars, native = _case()
    changed = getattr(native[0], field_name) * 1.01
    _assert_unexplained(bars, _native_with(native, **{field_name: changed}))


def test_a_volume_within_the_existing_tolerance_stays_explained() -> None:
    bars, native = _case()
    nudged = native[0].volume * (1 + 1e-12)
    result = qa.cross_check(bars, _native_with(native, volume=nudged), Interval.M15)
    assert result["mismatches_explained"] == 1


def test_a_native_open_other_than_the_first_traded_open_is_unexplained() -> None:
    bars, native = _case()
    _assert_unexplained(bars, _native_with(native, open=native[0].open + 0.01))


def test_the_tolerance_is_never_a_price_tolerance() -> None:
    """A one-tick open difference without a leading zero-trade candle stays a failure."""
    bars, native = _case()
    added = {
        "volume": 1.0,
        "quote_volume": 13.48,
        "taker_buy_volume": 0.5,
        "taker_buy_quote_volume": 6.74,
    }
    traded = _with(bars, 1, trades=5, **added)
    n = native[0]
    same_totals = {k: getattr(n, k) + v for k, v in added.items()}
    result = _assert_unexplained(traded, _native_with(native, trades=n.trades + 5, **same_totals))
    assert set(_only_mismatch(result)["diffs"]) == {"open"}  # so the rule itself refuses


def test_a_non_flat_leading_candle_is_unexplained() -> None:
    bars, native = _case()
    _assert_unexplained(_with(bars, 1, high=bars[1].high * 1.0001), native)


def test_a_leading_candle_with_volume_is_unexplained() -> None:
    bars, native = _case()
    same_total = _native_with(native, quote_volume=native[0].quote_volume + 0.5)
    result = _assert_unexplained(_with(bars, 1, quote_volume=0.5), same_total)
    assert set(_only_mismatch(result)["diffs"]) == {"open"}  # so the rule itself refuses


def test_a_filler_different_from_the_previous_close_is_unexplained() -> None:
    bars, native = _case()
    _assert_unexplained(_with(bars, 0, close=bars[0].close * 1.001), native)


def test_a_missing_previous_candle_fails_closed() -> None:
    bars, native = _case()
    _assert_unexplained(bars[1:], native)


def test_a_derived_open_taken_from_another_minute_is_unexplained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An aggregation that takes the open from the wrong minute must not be explained away."""
    bars, native = _case()
    real = qa.aggregate

    def wrong(items: Any, interval: Interval) -> tuple[list[Bar], int]:
        derived, skipped = real(items, interval)
        return [d.model_copy(update={"open": bars[1].open + 0.5}) for d in derived], skipped

    monkeypatch.setattr(qa, "aggregate", wrong)
    assert native[0].open == bars[2].open  # everything else still fits the exception
    _assert_unexplained(bars, native)


def test_a_bucket_without_any_traded_candle_is_unexplained() -> None:
    t = T0
    bars = [_bar(t - MIN)] + [_z1(t + i * MIN) for i in range(15)]
    native = RestKline(
        open_time_ms=t,
        open=101.0,
        high=100.0,
        low=100.0,
        close=100.0,
        volume=0.0,
        close_time_ms=t + M15 - 1,
        quote_volume=0.0,
        trades=0,
        taker_buy_volume=0.0,
        taker_buy_quote_volume=0.0,
    )
    _assert_unexplained(bars, [native])


def test_an_incomplete_bucket_is_not_compared() -> None:
    bars, native = _case()
    result = qa.cross_check(bars[:-1], native, Interval.M15)
    assert result["compared"] == 0
    assert result["derived_incomplete_skipped"] == 2  # the candle before it is a bucket too
    assert result["ok"] is False


def test_several_leading_candles_are_counted_and_must_all_be_z1() -> None:
    t = T0
    bars = [_bar(t - MIN), _z1(t), _z1(t + MIN), _z1(t + 2 * MIN)]
    bars += [_bar(t + (i + 3) * MIN, 100.5) for i in range(12)]
    high, low = max(b.high for b in bars[1:]), min(b.low for b in bars[1:])
    native = RestKline(
        open_time_ms=t,
        open=100.5,
        high=high,
        low=low,
        close=100.5,
        volume=24.0,
        close_time_ms=t + M15 - 1,
        quote_volume=2400.0,
        trades=120,
        taker_buy_volume=12.0,
        taker_buy_quote_volume=1200.0,
    )
    result = qa.cross_check(bars, [native], Interval.M15)
    assert _only_mismatch(result)["explanation"]["k"] == 3
    assert result["ok"]
    # the second leading candle at another price is no longer Z1
    broken = _with(bars, 2, open=100.25, high=100.25, low=100.25, close=100.25)
    _assert_unexplained(broken, [native])


def test_the_exception_also_applies_to_one_hour_buckets() -> None:
    t = T0
    bars = [_bar(t - MIN), _z1(t)] + [_bar(t + (i + 1) * MIN, 100.5) for i in range(59)]
    native = RestKline(
        open_time_ms=t,
        open=100.5,
        high=101.5,
        low=99.5,
        close=100.5,
        volume=118.0,
        close_time_ms=t + HOUR - 1,
        quote_volume=11800.0,
        trades=590,
        taker_buy_volume=59.0,
        taker_buy_quote_volume=5900.0,
    )
    result = qa.cross_check(bars, [native], Interval.H1)
    assert _only_mismatch(result)["explanation"]["reason"] == "leading_zero_trade_open"
    assert result["ok"]


def test_a_leading_candle_inside_a_registered_interruption_is_not_z1() -> None:
    """A registered outage filler is Z2: the exception is for genuine no-trade minutes only."""
    bars, native = _case()
    start = bars[1].open_time_ms
    registry = _registry(_entry(start, start + 2 * MIN, start + MIN))
    result = qa.cross_check(bars, native, Interval.M15, registry.for_market(Market.SPOT))
    assert result["mismatches_explained"] == 0
    assert result["ok"] is False
    # an interruption of the other market does not touch this spot classification
    other = _registry(_entry(start, start + 2 * MIN, start + MIN, market="usdm"))
    result = qa.cross_check(bars, native, Interval.M15, other.for_market(Market.SPOT))
    assert result["mismatches_explained"] == 1


def test_consistent_buckets_are_not_listed_as_explained() -> None:
    bars = [_bar(T0 + i * MIN) for i in range(15)]
    native = RestKline(
        open_time_ms=T0,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=30.0,
        close_time_ms=T0 + M15 - 1,
        quote_volume=3000.0,
        trades=150,
        taker_buy_volume=15.0,
        taker_buy_quote_volume=1500.0,
    )
    result = qa.cross_check(bars, [native], Interval.M15)
    assert result["mismatches"] == result["mismatches_explained"] == 0
    assert result["explained_by_reason"] == {}
    assert result["ok"]


# ------------------------------------------------------------------ zero-volume classes
def test_z1_requires_every_condition() -> None:
    prev = (T0 - MIN, 100.0)
    assert qa.classify_candle(_z1(T0), prev, ()) == (qa.Z1, None)
    assert qa.classify_candle(_bar(T0), prev, ()) is None  # an ordinary candle has no class
    assert not qa.is_z1_shape(_z1(T0).model_copy(update={"trades": 1}))


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"trades": 3}, qa.A_ZERO_VOLUME_WITH_TRADES),
        ({"high": 101.0}, qa.A_NOT_FLAT),
        ({"low": 99.0}, qa.A_NOT_FLAT),
        ({"close": 100.5, "high": 100.5}, qa.A_NOT_FLAT),
        ({"volume": 1.0}, qa.A_VOLUME_WITH_ZERO_TRADES),
        ({"quote_volume": 1.0}, qa.A_VOLUME_WITH_ZERO_TRADES),
        ({"taker_buy_volume": 1.0}, qa.A_VOLUME_WITH_ZERO_TRADES),
        ({"taker_buy_quote_volume": 1.0}, qa.A_VOLUME_WITH_ZERO_TRADES),
    ],
)
def test_class_a_shapes(over: dict[str, Any], reason: str) -> None:
    b = _z1(T0).model_copy(update=over)
    assert qa.classify_candle(b, (T0 - MIN, 100.0), ()) == (qa.A, reason)


def test_a_flat_price_other_than_the_previous_close_is_class_a() -> None:
    got = qa.classify_candle(_z1(T0, 100.0), (T0 - MIN, 100.5), ())
    assert got == (qa.A, qa.A_FLAT_NOT_PREVIOUS_CLOSE)


def test_a_zero_trade_candle_after_a_missing_minute_is_class_a() -> None:
    bars = [_bar(T0 - 2 * MIN), _z1(T0)]
    classes = qa.classify_zero_trade(bars)
    assert classes.z1_minutes == 0
    assert classes.anomaly_count == 1
    assert classes.anomalies[0]["reason"] == qa.A_PREVIOUS_MISSING


def test_the_first_candle_of_the_data_cannot_be_z1() -> None:
    classes = qa.classify_zero_trade([_z1(T0)])
    assert (classes.z1_minutes, classes.anomaly_count) == (0, 1)
    assert classes.anomalies[0]["reason"] == qa.A_PREVIOUS_MISSING


def test_registry_boundaries_separate_z1_from_z2() -> None:
    """[from, to) of this market: the minutes just outside stay Z1."""
    lo, absent = T0 + 5 * MIN, T0 + 10 * MIN
    bars = [_bar(T0 + 3 * MIN), _z1(T0 + 4 * MIN), *[_z1(lo + i * MIN) for i in range(5)]]
    classes = qa.classify_zero_trade(bars, HALT)
    assert classes.z1_ranges == [(T0 + 4 * MIN, lo)]
    assert classes.z2_ranges == [(lo, absent)]
    assert (classes.z1_minutes, classes.z2_minutes) == (1, 5)
    assert classes.absence_observed == []
    # the first minute after `to` is a genuine no-trade minute again
    after = qa.classify_candle(_z1(T0 + 15 * MIN), (T0 + 14 * MIN, 100.0), HALT)
    assert after == (qa.Z1, None)
    assert qa.classify_candle(_z1(T0 + 14 * MIN), (T0 + 13 * MIN, 100.0), HALT) == (qa.Z2, None)


def test_a_registry_of_another_market_never_applies() -> None:
    usdm = _registry(_entry(T0, T0 + 10 * MIN, T0 + 5 * MIN, market="usdm"))
    bars = [_bar(T0 - MIN), _z1(T0), _z1(T0 + MIN)]
    classes = qa.classify_zero_trade(bars, usdm.for_market(Market.SPOT))
    assert (classes.z1_minutes, classes.z2_minutes) == (2, 0)


def test_candles_observed_inside_a_documented_absence_are_a_finding() -> None:
    bars = [_bar(T0 + 9 * MIN), _bar(T0 + 10 * MIN), _z1(T0 + 11 * MIN)]
    classes = qa.classify_zero_trade(bars, HALT)
    assert classes.absence_observed == [(T0 + 10 * MIN, T0 + 12 * MIN)]
    report = _report([b.open_time_ms for b in bars], classes, HALT)
    assert report["summary"]["classification_ok"] is False
    assert "documented absence" in report["classification_findings"][0]


def test_z1_runs_are_reported_and_long_runs_flagged() -> None:
    long_run = [_z1(T0 + (i + 1) * MIN) for i in range(30)]  # ADR-0014: at least 30 minutes
    gap = T0 + 40 * MIN
    short_run = [_bar(gap), _z1(gap + MIN), _z1(gap + 2 * MIN)]
    classes = qa.classify_zero_trade([_bar(T0), *long_run, *short_run])
    section = qa.zero_trade_section(classes)
    assert section["z1_minutes"] == 32
    assert section["z1_runs"] == 1  # the 2-minute run is below ZERO_VOLUME_MIN_RUN
    assert [r["count"] for r in section["long_z1_runs"]] == [30]


def test_a_run_one_minute_below_the_long_limit_is_not_flagged() -> None:
    run = [_z1(T0 + (i + 1) * MIN) for i in range(29)]
    section = qa.zero_trade_section(qa.classify_zero_trade([_bar(T0), *run]))
    assert section["z1_runs"] == 1
    assert section["long_z1_runs"] == []


def test_class_a_rows_are_listed_with_their_values_up_to_the_limit() -> None:
    bars = [_z1(T0 + 2 * i * MIN) for i in range(qa.LISTED_LIMIT + 3)]
    classes = qa.classify_zero_trade(bars)
    assert classes.anomaly_count == qa.LISTED_LIMIT + 3
    section = qa.zero_trade_section(classes)
    assert section["a_rows"] == qa.LISTED_LIMIT + 3
    assert len(section["a_rows_listed"]) == qa.LISTED_LIMIT
    assert section["a_rows_truncated"] is True
    assert section["a_rows_listed"][0]["row"]["trades"] == 0


def _pattern(n: int, seed: int) -> list[Bar]:
    """A deterministic mix of traded, Z1, anomalous and missing minutes."""
    out: list[Bar] = []
    price = 100.0
    x = seed
    for i in range(n):
        x = (x * 1103515245 + 12345) % 2**31
        t = T0 + i * MIN
        kind = x % 7
        if kind == 0:
            continue  # missing minute
        if kind in (1, 2, 3):
            out.append(_z1(t, price))
        elif kind == 4:
            out.append(_z1(t, price + 1))  # flat at another price: class A
        else:
            price += 0.25
            out.append(_bar(t, price))
    return out


def _as_tuple(c: qa.ZeroTradeClasses) -> tuple[Any, ...]:
    return (
        c.z1_minutes,
        c.z2_minutes,
        c.anomaly_count,
        c.z1_ranges,
        c.z2_ranges,
        c.absence_observed,
        c.anomalies,
        c.last,
    )


@pytest.mark.parametrize("seed", range(12))
def test_joining_stretches_equals_classifying_everything_at_once(seed: int) -> None:
    """The bounded importer classifies month by month; the boundary must make no difference."""
    bars = _pattern(120, seed)
    registry = _registry(_entry(T0 + 30 * MIN, T0 + 50 * MIN, T0 + 40 * MIN))
    halt = registry.for_market(Market.SPOT)
    whole = qa.classify_zero_trade(bars, halt)
    for cut in range(0, len(bars), 7):
        cuts = [0, cut, min(cut + 5, len(bars)), len(bars)]
        parts = [
            qa.classify_zero_trade(bars[a:b], halt, defer_first=True) for a, b in pairwise(cuts)
        ]
        assert _as_tuple(qa.join_zero_trade(parts, halt)) == _as_tuple(whole)


def test_a_z1_run_across_a_month_boundary_is_one_run() -> None:
    start = ms(2024, 7)
    bars = [
        _bar(start - 2 * MIN),
        _z1(start - MIN),
        _z1(start),
        _z1(start + MIN),
        _bar(start + 2 * MIN),
    ]
    parts = [
        qa.classify_zero_trade(bars[:2], defer_first=True),
        qa.classify_zero_trade(bars[2:], defer_first=True),
    ]
    joined = qa.join_zero_trade(parts)
    assert joined.z1_ranges == [(start - MIN, start + 2 * MIN)]
    assert joined.anomaly_count == 0


def test_a_month_boundary_with_a_gap_cannot_be_z1() -> None:
    start = ms(2024, 7)
    parts = [
        qa.classify_zero_trade([_bar(start - 2 * MIN)], defer_first=True),
        qa.classify_zero_trade([_z1(start)], defer_first=True),
    ]
    joined = qa.join_zero_trade(parts)
    assert joined.z1_minutes == 0
    assert joined.anomalies[0]["reason"] == qa.A_PREVIOUS_MISSING


def test_an_ordinary_first_bar_is_not_kept_for_the_join() -> None:
    classes = qa.classify_zero_trade([_bar(T0), _bar(T0 + MIN)], defer_first=True)
    assert classes.first is None
    assert classes.last == (T0 + MIN, 100.0)


# ------------------------------------------------------------------ missing minutes
def _report(
    times: Sequence[int],
    classes: qa.ZeroTradeClasses,
    intervals: Sequence[ExchangeInterruption] = (),
    *,
    cross_checks: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    return qa.build_report(
        bars_times=times,
        start_ms=times[0],
        end_ms=times[-1] + MIN,
        step_ms=MIN,
        collector=qa.QACollector(),
        cross_checks=cross_checks,
        zero_trade=classes,
        interruptions=intervals,
        registry=SPOT_HALT.manifest(),
    )


def _times_without(lo: int, hi: int) -> list[int]:
    """Minutes of [T0, T0 + 20) except the missing range [lo, hi)."""
    return [t for t in range(T0, T0 + 20 * MIN, MIN) if not lo <= t < hi]


def test_missing_minutes_in_the_documented_absence_are_registered_downtime() -> None:
    times = _times_without(T0 + 10 * MIN, T0 + 15 * MIN)
    s = _report(times, qa.ZeroTradeClasses(), HALT)["summary"]
    assert s["missing_interior"] == 5
    assert s["missing_registered_downtime"] == 5
    assert s["missing_unexpected"] == 0
    assert s["classification_ok"] is True


def _uncovered_report(
    times: Sequence[int], start: int, end: int, intervals: Sequence[ExchangeInterruption] = ()
) -> dict[str, Any]:
    return qa.build_report(
        bars_times=times,
        start_ms=start,
        end_ms=end,
        step_ms=MIN,
        collector=qa.QACollector(),
        zero_trade=qa.ZeroTradeClasses(),
        interruptions=intervals,
    )


def test_an_uncovered_tail_is_unexpected_and_fails() -> None:
    """ADR-0013 section 6: every other missing minute fails, also outside the interior."""
    times = list(range(T0, T0 + 10 * MIN, MIN))
    report = _uncovered_report(times, T0, T0 + 20 * MIN)
    s = report["summary"]
    assert (s["missing_interior"], s["uncovered_tail"]) == (0, 10)
    assert s["missing_unexpected"] == 10
    assert s["classification_ok"] is False
    assert report["classification_findings"] == ["unexpected missing minutes: 10"]


def test_an_uncovered_head_is_unexpected_and_fails() -> None:
    times = list(range(T0 + 3 * MIN, T0 + 10 * MIN, MIN))
    s = _uncovered_report(times, T0, T0 + 10 * MIN)["summary"]
    assert (s["uncovered_head"], s["missing_unexpected"]) == (3, 3)
    assert s["classification_ok"] is False


def test_an_import_without_any_row_fails_the_classification() -> None:
    s = _uncovered_report([], T0, T0 + 10 * MIN)["summary"]
    assert s["missing_unexpected"] == 10
    assert s["classification_ok"] is False


def test_an_empty_request_range_fails_the_classification() -> None:
    """A request past the available data has no missing minutes at all, and no rows."""
    report = _uncovered_report([], T0, T0)
    assert report["summary"]["missing_unexpected"] == 0
    assert report["summary"]["classification_ok"] is False
    assert report["classification_findings"] == ["no rows imported"]


def test_a_request_wholly_inside_the_documented_absence_fails_the_classification() -> None:
    report = _uncovered_report([], T0 + 10 * MIN, T0 + 15 * MIN, HALT)
    s = report["summary"]
    assert (s["missing_registered_downtime"], s["missing_unexpected"]) == (5, 0)
    assert s["classification_ok"] is False
    assert report["classification_findings"] == ["no rows imported"]


def test_a_tail_inside_the_documented_absence_is_registered_downtime() -> None:
    times = list(range(T0, T0 + 10 * MIN, MIN))  # the absence of HALT starts at T0 + 10
    s = _uncovered_report(times, T0, T0 + 15 * MIN, HALT)["summary"]
    assert (s["missing_registered_downtime"], s["missing_unexpected"]) == (5, 0)
    assert s["classification_ok"] is True


def test_the_summary_counts_explained_mismatches_per_reason() -> None:
    bars, native = _case()
    check = qa.cross_check(bars, native, Interval.M15)
    s = _report([T0], qa.ZeroTradeClasses(), cross_checks=[check, check])["summary"]
    assert s["cross_check_explained_by_reason"] == {"leading_zero_trade_open": 2}


def test_a_missing_minute_before_absent_from_is_unexpected_and_fails() -> None:
    times = _times_without(T0 + 7 * MIN, T0 + 8 * MIN)
    report = _report(times, qa.ZeroTradeClasses(), HALT)
    assert report["summary"]["missing_registered_downtime"] == 0
    assert report["summary"]["missing_unexpected"] == 1
    assert report["summary"]["classification_ok"] is False
    assert report["classification_findings"] == ["unexpected missing minutes: 1"]


def test_a_gap_across_the_boundaries_only_counts_its_documented_part() -> None:
    times = _times_without(T0 + 8 * MIN, T0 + 17 * MIN)  # 2 before, 5 inside, 2 after
    report = _report(times, qa.ZeroTradeClasses(), HALT)
    assert report["summary"]["missing_registered_downtime"] == 5
    assert report["summary"]["missing_unexpected"] == 4
    assert report["summary"]["classification_ok"] is False
    (part,) = report["missing_classification"]["registered_downtime"]
    assert (part["from_ms"], part["to_ms"]) == (T0 + 10 * MIN, T0 + 15 * MIN)
    assert part["registry_id"] == "halt"


def test_an_unregistered_gap_stays_unexpected_and_an_empty_registry_exempts_nothing() -> None:
    times = _times_without(T0 + 10 * MIN, T0 + 15 * MIN)
    s = _report(times, qa.ZeroTradeClasses())["summary"]
    assert s["missing_registered_downtime"] == 0
    assert s["missing_unexpected"] == 5
    assert s["classification_ok"] is False


def test_a_registry_of_another_market_exempts_nothing() -> None:
    usdm = _registry(_entry(T0 + 5 * MIN, T0 + 15 * MIN, T0 + 10 * MIN, market="usdm"))
    times = _times_without(T0 + 10 * MIN, T0 + 15 * MIN)
    s = _report(times, qa.ZeroTradeClasses(), usdm.for_market(Market.SPOT))["summary"]
    assert s["missing_unexpected"] == 5


def test_the_repository_registry_splits_the_2023_halt_as_approved() -> None:
    registry = load_registry(Path(__file__).parents[3] / "configs" / "exchange_interruptions.yaml")
    spot = registry.for_market(Market.SPOT)
    absent, end = ms(2023, 3, 24, 12, 39), ms(2023, 3, 24, 14, 0)
    fillers = ms(2023, 3, 24, 11, 28)
    section = qa.missing_section([(fillers, end)], spot)
    assert section["registered_downtime_minutes"] == (end - absent) // MIN == 81
    assert section["unexpected_minutes"] == (absent - fillers) // MIN == 71
    assert registry.for_market(Market.USDM) == ()


def test_class_a_rows_fail_the_classification() -> None:
    classes = qa.classify_zero_trade([_z1(T0)])
    report = _report([T0], classes)
    assert report["summary"]["zero_trade_a_rows"] == 1
    assert report["summary"]["classification_ok"] is False
    assert report["classification_findings"] == ["class A zero-volume candles: 1"]


# ------------------------------------------------------------------ report format and redaction
def test_the_report_states_its_version_and_counts() -> None:
    bars, native = _case()
    check = qa.cross_check(bars, native, Interval.M15)
    report = _report(
        [b.open_time_ms for b in bars], qa.classify_zero_trade(bars), cross_checks=[check]
    )
    assert report["qa_report_version"] == report["summary"]["qa_report_version"] == 2
    s = report["summary"]
    assert s["cross_check_mismatches_explained"] == 1
    assert s["cross_check_mismatches_unexplained"] == 0
    assert s["cross_checks_ok"] is True
    assert report["exchange_interruptions"]["sha256"] == SPOT_HALT.sha256
    assert report["cross_checks"][0]["explained_mismatches"][0]["explanation"]["k"] == 1


def test_a_report_without_bars_has_no_classification() -> None:
    report = qa.build_report(
        bars_times=[T0],
        start_ms=T0,
        end_ms=T0 + MIN,
        step_ms=MIN,
        collector=qa.QACollector(),
    )
    assert report["qa_report_version"] == 2
    assert report["zero_trade_classification"] is None
    assert report["missing_classification"] is None
    assert report["summary"]["classification_ok"] is None
    assert report["summary"]["missing_unexpected"] is None


def _holdout_mismatch(t: int) -> dict[str, Any]:
    return {
        "open_time_ms": t,
        "time": "x",
        "diffs": {"open": [1.5, 1.75]},
        "explanation": {
            "reason": "leading_zero_trade_open",
            "k": 1,
            "filler": 1.5,
            "first_traded_open": 1.75,
            "first_minute": "x",
        },
    }


def test_holdout_explanations_and_anomalies_keep_no_prices() -> None:
    t = ms(2026, 2, 1)
    (r,) = qa.redact_holdout([_holdout_mismatch(t)])
    assert r["diffs"] == ["open"]
    assert r["redacted"]
    assert r["explanation"] == {"reason": "leading_zero_trade_open", "k": 1, "first_minute": "x"}
    classes = qa.classify_zero_trade([_z1(t, 1.5)])
    text = json.dumps(qa.zero_trade_section(classes))
    assert "open_time_ms" in text
    assert "1.5" not in text


def test_the_report_redacts_every_list_of_holdout_mismatches() -> None:
    t = ms(2026, 2, 1)
    check = {
        "interval": "15m",
        "ok": True,
        "mismatches_explained": 1,
        "mismatches_unexplained": 0,
        "mismatch_samples": [_holdout_mismatch(t)],
        "explained_mismatches": [_holdout_mismatch(t)],
    }
    report = _report([t, t + MIN], qa.classify_zero_trade([]), cross_checks=[check])
    (out,) = report["cross_checks"]
    for (entry,) in (out["mismatch_samples"], out["explained_mismatches"]):
        assert entry["redacted"]
        assert entry["diffs"] == ["open"]
        assert "filler" not in entry["explanation"]
        assert "first_traded_open" not in entry["explanation"]


def test_joining_keeps_the_anomaly_list_at_its_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qa, "LISTED_LIMIT", 2)
    a_rows = [_z1(T0 + i * MIN).model_copy(update={"trades": 3}) for i in range(3)]
    joined = qa.join_zero_trade(
        [qa.classify_zero_trade(a_rows[:2]), qa.classify_zero_trade(a_rows[2:])]
    )
    assert joined.anomaly_count == 3
    assert [r["open_time_ms"] for r in joined.anomalies] == [T0, T0 + MIN]
    assert qa.zero_trade_section(joined)["a_rows_truncated"] is True


# ------------------------------------------------------------------ through the importer
def _file(label: str, interval: str = "1m") -> ArchiveFile:
    return ArchiveFile(Market.SPOT, ArchiveDataType.KLINES, SYM, interval, Period.MONTHLY, label)


def _fmt(values: list[Any]) -> str:
    return ",".join(f"{v:.8f}" if isinstance(v, float) else str(v) for v in values)


def _filler(t: int, price: float) -> list[Any]:
    return [t, price, price, price, price, 0.0, t + MIN - 1, 0.0, 0, 0.0, 0.0, 0]


def _native_row(minutes: list[list[Any]], start: int, step: int) -> list[Any]:
    """A native candle as Binance builds it, from trades: fillers contribute nothing."""
    traded = [m for m in minutes if m[8] > 0]
    if not traded:  # no trade in the whole bucket: flat at the filler, as published
        price = minutes[0][4]
        return [start, price, price, price, price, 0.0, start + step - 1, 0.0, 0, 0.0, 0.0, 0]
    return [
        start,
        traded[0][1],
        max(m[2] for m in traded),
        min(m[3] for m in traded),
        traded[-1][4],
        sum(m[5] for m in traded),
        start + step - 1,
        sum(m[7] for m in traded),
        sum(m[8] for m in traded),
        sum(m[9] for m in traded),
        sum(m[10] for m in traded),
        0,
    ]


def _month_archive(
    archive: FakeArchive,
    label: str,
    start: int,
    end: int,
    fillers: Sequence[int] = (),
    edit: Callable[[list[list[Any]]], None] | None = None,
) -> list[list[Any]]:
    """A complete consistent month: 1m file, and native 15m and 1h files built from its trades."""
    rows: list[list[Any]] = []
    for t in range(start, end, MIN):
        rows.append(kline_values(t, MIN))
        if t in fillers:
            rows[-1] = _filler(t, rows[-2][4])
    if edit:
        edit(rows)
    archive.add(_file(label), [_fmt(r) for r in rows])
    for interval, step in (("15m", M15), ("1h", HOUR)):
        native = [
            _native_row(rows[i : i + step // MIN], start + i * MIN, step)
            for i in range(0, len(rows), step // MIN)
        ]
        archive.add(_file(label, interval), [_fmt(r) for r in native])
    return rows


def _importer(lake: Lake, archive: FakeArchive, **kw: Any) -> Importer:
    writer = LakeWriter(lake.data_dir, lake.codec)
    return Importer(writer, archive, None, ManualClock(NOW), **kw)


def _import(importer: Importer, start: int, end: int) -> Any:
    return asyncio.run(importer.run(ImportRequest(Market.SPOT, LakeKind.BARS_1M, SYM, start, end)))


def test_the_importer_records_verifiable_evidence_for_an_explained_mismatch(
    lake: Lake, archive: FakeArchive
) -> None:
    start, end = ms(2024, 11), ms(2024, 12)
    bucket = ms(2024, 11, 5, 10, 15)
    _month_archive(archive, "2024-11", start, end, fillers=[bucket])
    res = _import(_importer(lake, archive), start, end)
    s = res.qa["summary"]
    assert res.qa["qa_report_version"] == s["qa_report_version"] == 2
    assert s["cross_check_mismatches_explained"] == 1
    assert s["cross_check_mismatches_unexplained"] == 0
    assert s["cross_checks_ok"] is True
    assert s["zero_trade_z1_minutes"] == 1
    assert s["classification_ok"] is True
    check = next(c for c in res.qa["cross_checks"] if c["interval"] == "15m")
    (entry,) = check["explained_mismatches"]
    evidence = entry["explanation"]["evidence"]
    line = (bucket - start) // MIN + 1
    sha_1m = hashlib.sha256(archive.files[_file("2024-11").relpath][0]).hexdigest()
    sha_15m = hashlib.sha256(archive.files[_file("2024-11", "15m").relpath][0]).hexdigest()
    assert evidence["derived_1m"] == {
        "previous": {"file": _file("2024-11").name, "sha256": sha_1m, "line": line - 1},
        "first": {"file": _file("2024-11").name, "sha256": sha_1m, "line": line},
        "first_traded": {"file": _file("2024-11").name, "sha256": sha_1m, "line": line + 1},
    }
    assert evidence["native"] == {
        "file": _file("2024-11", "15m").name,
        "sha256": sha_15m,
        "line": (bucket - start) // M15 + 1,
    }
    stored = json.loads((lake.imports_dir / res.dataset_id / "qa_report.json").read_text())
    assert stored["qa_report_version"] == 2
    assert stored["cross_checks"] == json.loads(json.dumps(res.qa["cross_checks"]))


def test_the_importer_classifies_a_filler_on_the_first_minute_of_a_month(
    lake: Lake, archive: FakeArchive
) -> None:
    """The month boundary: its previous candle belongs to the month processed separately."""
    start, mid, end = ms(2024, 11), ms(2024, 12), ms(2025, 1)
    november = _month_archive(archive, "2024-11", start, mid)
    last_close = november[-1][4]

    def filler_first(rows: list[list[Any]]) -> None:
        rows[0] = _filler(mid, last_close)

    _month_archive(archive, "2024-12", mid, end, edit=filler_first)
    res = _import(_importer(lake, archive, month_concurrency=2), start, end)
    s = res.qa["summary"]
    assert s["zero_trade_z1_minutes"] == 1
    assert s["zero_trade_a_rows"] == 0
    # the bucket is at the month boundary, so the candle before it is outside the sample month:
    # ADR-0015 fails closed there, and the mismatch stays unexplained
    assert s["cross_checks_ok"] is False
    assert s["cross_check_mismatches_explained"] == 0
    assert s["cross_check_mismatches_unexplained"] == 2  # 15m and 1h


def test_evidence_that_cannot_be_located_fails_closed(
    lake: Lake, archive: FakeArchive, monkeypatch: pytest.MonkeyPatch
) -> None:
    start, end = ms(2024, 11), ms(2024, 12)
    bucket = ms(2024, 11, 5, 10, 15)
    _month_archive(archive, "2024-11", start, end, fillers=[bucket])

    async def nowhere(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(Importer, "_locate_1m", nowhere)
    res = _import(_importer(lake, archive), start, end)
    s = res.qa["summary"]
    assert s["cross_check_mismatches_explained"] == 0
    assert s["cross_check_mismatches_unexplained"] == 1
    assert s["cross_checks_ok"] is False
    check = next(c for c in res.qa["cross_checks"] if c["interval"] == "15m")
    assert check["explained_mismatches"] == []
    assert check["explained_by_reason"] == {}
    (entry,) = check["mismatch_samples"]
    assert entry["explanation"] is None
    assert "evidence" in entry["explanation_rejected"]


def test_a_changed_source_file_fails_the_evidence_closed(lake: Lake, archive: FakeArchive) -> None:
    """The 1m file must still be the one the bars came from."""
    start, end = ms(2024, 11), ms(2024, 12)
    bucket = ms(2024, 11, 5, 10, 15)
    rows = _month_archive(archive, "2024-11", start, end, fillers=[bucket])

    class Swapping(FakeArchive):
        fetches: int = 0

        async def fetch(self, f: ArchiveFile) -> Any:
            if f.interval == "1m":
                self.fetches += 1
                if self.fetches == 2:  # the read-back for the evidence
                    self.add(f, [_fmt(r) for r in rows[:-1]])
            return await super().fetch(f)

    swapping = Swapping(files=dict(archive.files))
    res = _import(_importer(lake, swapping), start, end)
    assert res.qa["summary"]["cross_check_mismatches_explained"] == 0
    assert res.qa["summary"]["cross_checks_ok"] is False


def test_the_registry_changes_qa_only_never_the_stored_data(
    tmp_path: Path, archive: FakeArchive
) -> None:
    """Same bars, same dataset, same cross-check totals with and without the registry."""
    start, end = ms(2024, 11), ms(2024, 12)
    halt_lo, halt_hi = ms(2024, 11, 5, 10), ms(2024, 11, 5, 12)
    fillers = list(range(halt_lo, halt_lo + 30 * MIN, MIN))
    rows = _month_archive(archive, "2024-11", start, end, fillers=fillers)
    registry = _registry(_entry(halt_lo, halt_hi, halt_lo + 30 * MIN))
    kept = [r for r in rows if not halt_lo + 30 * MIN <= r[0] < halt_hi]
    archive.add(_file("2024-11"), [_fmt(r) for r in kept])
    plain = _import(_importer(Lake(tmp_path / "a", JsonLinesCodec()), archive), start, end)
    with_registry = _import(
        _importer(Lake(tmp_path / "b", JsonLinesCodec()), archive, interruptions=registry),
        start,
        end,
    )
    assert plain.dataset_id == with_registry.dataset_id
    assert [p.content_hash for p in plain.parts] == [p.content_hash for p in with_registry.parts]
    assert plain.qa["gaps"] == with_registry.qa["gaps"]
    a, b = plain.qa["summary"], with_registry.qa["summary"]
    assert a["missing_interior"] == b["missing_interior"] == 90
    assert (a["missing_unexpected"], b["missing_unexpected"]) == (90, 0)
    assert b["missing_registered_downtime"] == 90
    assert (a["zero_trade_z2_minutes"], b["zero_trade_z2_minutes"]) == (0, 30)
    assert a["zero_trade_z1_minutes"] == 30
    assert b["zero_trade_z1_minutes"] == 0
    assert a["classification_ok"] is False
    assert b["classification_ok"] is True
    for key in ("cross_checks_ok", "cross_check_mismatches_explained"):
        assert a[key] == b[key]
    assert a["cross_check_mismatches_unexplained"] == b["cross_check_mismatches_unexplained"]
    assert with_registry.qa["exchange_interruptions"]["sha256"] == registry.sha256
    assert plain.qa["exchange_interruptions"]["sha256"] is None


def test_the_importer_default_is_the_empty_registry() -> None:
    assert Importer.__init__.__kwdefaults__["interruptions"] is EMPTY_REGISTRY


def test_daily_klines_have_no_classification(lake: Lake, archive: FakeArchive) -> None:
    f = ArchiveFile(Market.SPOT, ArchiveDataType.KLINES, SYM, "1d", Period.MONTHLY, "2024-11")
    archive.add_klines(f, step=DAY)
    req = ImportRequest(Market.SPOT, LakeKind.KLINES_1D, SYM, f.start_ms, f.end_ms)
    res = asyncio.run(_importer(lake, archive).run(req))
    assert res.qa["qa_report_version"] == 2
    assert res.qa["zero_trade_classification"] is None
    assert res.qa["exchange_interruptions"] is None


# ------------------------------------------------------------------ compatibility with E-3
def test_a_row_lost_with_fillers_present_still_fails_the_import(
    lake: Lake, archive: FakeArchive, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The E-3 row accounting does not treat a classified candle as a droppable row."""
    start, end = ms(2024, 11), ms(2024, 12)
    filler = ms(2024, 11, 5, 10)
    _month_archive(archive, "2024-11", start, end, fillers=[filler])
    original = csvparse.parse_klines_csv

    def lossy(text: str, *a: Any, **kw: Any) -> Any:
        parsed = original(text, *a, **kw)
        rows = [r for r in parsed.rows if r.open_time_ms != filler]
        return type(parsed)(rows, parsed.unit, parsed.issues)

    monkeypatch.setattr(csvparse, "parse_klines_csv", lossy)
    with pytest.raises(ArchiveImportError, match="source row reconciliation failed"):
        _import(_importer(lake, archive), start, end)
    lake.refresh()
    assert lake.dataset_ids() == []


def test_fillers_are_committed_with_exact_accounting_and_code_provenance(
    lake: Lake, archive: FakeArchive
) -> None:
    start, end = ms(2024, 11), ms(2024, 12)
    filler = ms(2024, 11, 5, 10)
    rows = _month_archive(archive, "2024-11", start, end, fillers=[filler])
    res = _import(_importer(lake, archive), start, end)
    assert res.rows == len(rows) == (end - start) // MIN
    assert lake.manifest(res.dataset_id)["code_provenance"]["tsclaude_version"]
    assert res.qa["summary"]["zero_trade_z1_minutes"] == 1
    assert res.qa["summary"]["classification_ok"] is True


# ------------------------------------------------------------------ the data import command
ARGS = [
    "data", "import", "--market", "spot", "--symbols", SYM,
    "--from", "2024-01-01", "--to", "2024-01-02",
]  # fmt: skip


class _Env:
    async def close(self) -> None:
        return None


def _cli(
    monkeypatch: pytest.MonkeyPatch, *extra: str, data_dir: Path
) -> tuple[int, dict[str, Any]]:
    """Run the command with the network parts replaced; records what reached the import."""
    seen: dict[str, Any] = {"opened": 0}

    async def open_env(*a: Any, **k: Any) -> _Env:
        seen["opened"] += 1
        return _Env()

    async def run_import(*a: Any, **k: Any) -> dict[str, Any]:
        seen["interruptions"] = k["interruptions"]
        return {"ok": True}

    monkeypatch.setattr(datacli, "open_env", open_env)
    monkeypatch.setattr(datacli, "run_import", run_import)
    args = build_parser().parse_args([*ARGS, "--data-dir", str(data_dir), *extra])
    return args.func(args), seen


def test_a_broken_registry_refuses_before_anything_is_fetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "broken.yaml"
    bad.write_text("schema: 2\ninterruptions: not-a-list\n", encoding="utf-8")
    code, seen = _cli(monkeypatch, "--interruptions", str(bad), data_dir=tmp_path)
    assert code == 5
    assert seen["opened"] == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["refused"] == "exchange_interruptions"


def test_a_named_registry_that_does_not_exist_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, seen = _cli(
        monkeypatch, "--interruptions", str(tmp_path / "absent.yaml"), data_dir=tmp_path
    )
    assert code == 5
    assert seen["opened"] == 0


def test_a_broken_default_registry_also_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "exchange_interruptions.yaml").write_text("{", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    code, seen = _cli(monkeypatch, data_dir=tmp_path)
    assert code == 5
    assert seen["opened"] == 0


def test_no_default_registry_means_no_exemption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    code, seen = _cli(monkeypatch, data_dir=tmp_path)
    assert code == 0
    assert seen["interruptions"] is EMPTY_REGISTRY


def test_a_valid_registry_reaches_the_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_file = Path(__file__).parents[3] / "configs" / "exchange_interruptions.yaml"
    code, seen = _cli(monkeypatch, "--interruptions", str(repository_file), data_dir=tmp_path)
    assert code == 0
    assert seen["interruptions"].sha256 == load_registry(repository_file).sha256
    assert seen["interruptions"].entries


# ------------------------------------------------------------------ evidence line numbers
def test_open_time_lines_counts_file_lines_with_and_without_a_header() -> None:
    t = ms(2024, 11)
    rows = "\n".join(_fmt(kline_values(t + i * MIN, MIN)) for i in range(3))
    assert csvparse.open_time_lines(rows) == {t: 1, t + MIN: 2, t + 2 * MIN: 3}
    assert csvparse.open_time_lines(HEADER + "\n" + rows) == {
        t: 2,
        t + MIN: 3,
        t + 2 * MIN: 4,
    }
    micro = _fmt([t * 1000, *kline_values(t, MIN)[1:]])
    assert csvparse.open_time_lines(micro) == {t: 1}


def test_a_repeated_open_time_keeps_its_first_line() -> None:
    t = ms(2024, 11)
    row = _fmt(kline_values(t, MIN))
    assert csvparse.open_time_lines(f"{row}\n{row}") == {t: 1}


# ------------------------------------------------------------------ the safeguards one by one
def _plain_bucket(price: float = 100.0) -> list[Bar]:
    """The candle before the bucket and 15 traded candles, every open equal to `price`."""
    return [_bar(T0 - MIN, price)] + [_bar(T0 + i * MIN, price) for i in range(15)]


def _kline(t: int, open_price: float) -> RestKline:
    return RestKline(
        open_time_ms=t,
        open=open_price,
        high=open_price,
        low=open_price,
        close=open_price,
        volume=1.0,
        close_time_ms=t + M15 - 1,
        quote_volume=1.0,
        trades=1,
        taker_buy_volume=1.0,
        taker_buy_quote_volume=1.0,
    )


def _explain(bars: list[Bar], native_open: float, step: int = M15) -> dict[str, Any] | None:
    (derived,), _ = qa.aggregate(bars[1:], Interval.M15)
    by_open = {b.open_time_ms: b for b in bars}
    return qa.explain_open_difference(derived, _kline(T0, native_open), by_open, step)


def test_a_bucket_without_leading_filler_is_refused_by_the_rule_itself() -> None:
    """k = 0 cannot reach the rule through `cross_check` (the opens would agree); the rule still
    refuses it on its own."""
    assert _explain(_plain_bucket(), 100.0) is None


def test_the_rule_itself_requires_every_minute_of_the_bucket() -> None:
    bars = [_bar(T0 - MIN), _z1(T0)] + [_bar(T0 + i * MIN, 100.5) for i in range(1, 15)]
    assert _explain(bars, 100.5) is not None
    (derived,), _ = qa.aggregate(bars[1:], Interval.M15)
    by_open = {b.open_time_ms: b for b in bars}
    del by_open[T0 + 14 * MIN]  # the last minute: everything before it would still fit
    assert qa.explain_open_difference(derived, _kline(T0, 100.5), by_open, M15) is None


def test_more_than_fifteen_leading_candles_in_a_one_hour_bucket_are_counted() -> None:
    """The rule walks the bucket of the interval compared, not a fixed 15 minutes."""
    t = T0
    bars = [_bar(t - MIN)] + [_z1(t + i * MIN) for i in range(20)]
    bars += [_bar(t + (i + 20) * MIN, 100.5) for i in range(40)]
    native = RestKline(
        open_time_ms=t,
        open=100.5,
        high=101.5,
        low=99.5,
        close=100.5,
        volume=80.0,
        close_time_ms=t + HOUR - 1,
        quote_volume=8000.0,
        trades=400,
        taker_buy_volume=40.0,
        taker_buy_quote_volume=4000.0,
    )
    result = qa.cross_check(bars, [native], Interval.H1)
    assert _only_mismatch(result)["explanation"]["k"] == 20
    assert result["ok"]


def test_run_import_hands_the_registry_to_the_importer(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class _Rest:
        async def load_exchange_info(self, symbols: tuple[str, ...]) -> None:
            return None

    def importer(env: Any, **kw: Any) -> Any:
        seen.update(kw)
        raise RuntimeError("stop here")

    env = dataclasses.make_dataclass("E", ["rest", "lake", "codec"])(
        {Market.SPOT: _Rest()}, None, None
    )
    monkeypatch.setattr(datacli, "_importer", importer)
    registry = _registry(_entry(T0, T0 + HOUR, T0))
    with pytest.raises(RuntimeError, match="stop here"):
        asyncio.run(
            datacli.run_import(env, Market.SPOT, [SYM], T0, T0 + DAY, interruptions=registry)
        )
    assert seen["interruptions"] is registry


# ------------------------------------------------------------------ the fixture extractor
def _extractor() -> Any:
    path = Path(__file__).parents[3] / "scripts" / "fixtures" / "extract_e2_open_cases.py"
    spec = importlib.util.spec_from_file_location("extract_e2_open_cases", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_extractor_refuses_a_2026_archive_without_reading_it(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="sealed holdout"):
        _extractor()._lines(tmp_path / "absent.zip", "AVAXUSDT-1m-2026-01.zip")


def test_the_extractor_refuses_a_changed_archive(tmp_path: Path) -> None:
    other = tmp_path / "AVAXUSDT-1m-2025-12.zip"
    other.write_bytes(b"not the published archive")
    with pytest.raises(SystemExit, match="differs from ADR-0015"):
        _extractor()._lines(other, other.name)


def test_the_extractor_refuses_a_line_inside_the_holdout() -> None:
    module = _extractor()
    start = qa.HOLDOUT_START_MS
    assert start == module.HOLDOUT_START_MS
    with pytest.raises(SystemExit, match="sealed holdout"):
        module._take({start: (1, "row")}, start, "file")
    assert module._take({start - MIN: (1, "row")}, start - MIN, "file") == {
        "line": 1,
        "csv": "row",
    }


# ---------------------------------------------------- the registry through the importer
def _halted_month(archive: FakeArchive) -> tuple[int, int, int, int]:
    """A month with one leading filler at 10:15 and the minutes 10:30-12:00 absent. Returns
    (start, end, halt from, halt to); the filler lies inside the halt, before its absence."""
    start, end = ms(2024, 11), ms(2024, 12)
    halt_lo, halt_hi = ms(2024, 11, 5, 10), ms(2024, 11, 5, 12)
    rows = _month_archive(archive, "2024-11", start, end, fillers=[halt_lo + 15 * MIN])
    kept = [r for r in rows if not halt_lo + 30 * MIN <= r[0] < halt_hi]
    archive.add(_file("2024-11"), [_fmt(r) for r in kept])
    return start, end, halt_lo, halt_hi


def test_the_importer_ignores_a_registry_entry_of_another_market(
    lake: Lake, archive: FakeArchive
) -> None:
    start, end, lo, hi = _halted_month(archive)
    registry = _registry(_entry(lo, hi, lo + 30 * MIN, market="usdm"))
    res = _import(_importer(lake, archive, interruptions=registry), start, end)
    s = res.qa["summary"]
    assert (s["zero_trade_z1_minutes"], s["zero_trade_z2_minutes"]) == (1, 0)
    assert (s["missing_registered_downtime"], s["missing_unexpected"]) == (0, 90)
    assert s["cross_check_mismatches_explained"] == 1
    assert s["cross_check_mismatches_unexplained"] == 0


def test_the_importer_gives_the_registry_to_the_cross_check(
    lake: Lake, archive: FakeArchive
) -> None:
    """A filler in a registered interruption is Z2, so the open exception does not apply."""
    start, end, lo, hi = _halted_month(archive)
    registry = _registry(_entry(lo, hi, lo + 30 * MIN))
    res = _import(_importer(lake, archive, interruptions=registry), start, end)
    s = res.qa["summary"]
    assert (s["zero_trade_z1_minutes"], s["zero_trade_z2_minutes"]) == (0, 1)
    assert (s["missing_registered_downtime"], s["missing_unexpected"]) == (90, 0)
    assert s["cross_check_mismatches_explained"] == 0
    assert s["cross_check_mismatches_unexplained"] == 1
    assert s["cross_checks_ok"] is False


def test_a_native_line_that_cannot_be_located_fails_closed(
    lake: Lake, archive: FakeArchive, monkeypatch: pytest.MonkeyPatch
) -> None:
    start, end = ms(2024, 11), ms(2024, 12)
    bucket = ms(2024, 11, 5, 10, 15)
    _month_archive(archive, "2024-11", start, end, fillers=[bucket])
    real = csvparse.open_time_lines
    texts: list[str] = []

    def without_the_bucket(text: str) -> dict[int, int]:
        texts.append(text)
        lines = real(text)
        if len(texts) == 1:  # the native file is read first
            del lines[bucket]
        return lines

    monkeypatch.setattr(csvparse, "open_time_lines", without_the_bucket)
    s = _import(_importer(lake, archive), start, end).qa["summary"]
    assert texts
    assert s["cross_check_mismatches_explained"] == 0
    assert s["cross_check_mismatches_unexplained"] == 1
    assert s["cross_checks_ok"] is False


def test_a_repeated_import_never_rewrites_an_existing_report(
    lake: Lake, archive: FakeArchive
) -> None:
    """A version 1 report stays exactly as written, whatever a later importer would say."""
    start, end, lo, hi = _halted_month(archive)
    first = _import(_importer(lake, archive), start, end)
    report = lake.imports_dir / first.dataset_id / "qa_report.json"
    manifest = report.with_name("manifest.json")
    old = json.loads(report.read_text(encoding="utf-8"))
    del old["qa_report_version"]
    report.write_text(json.dumps(old), encoding="utf-8")
    before = report.read_bytes(), manifest.read_bytes()
    registry = _registry(_entry(lo, hi, lo + 30 * MIN))
    second = _import(_importer(lake, archive, interruptions=registry), start, end)
    assert second.dataset_id == first.dataset_id
    assert (report.read_bytes(), manifest.read_bytes()) == before


def test_a_1m_line_that_cannot_be_located_fails_closed(
    lake: Lake, archive: FakeArchive, monkeypatch: pytest.MonkeyPatch
) -> None:
    start, end = ms(2024, 11), ms(2024, 12)
    bucket = ms(2024, 11, 5, 10, 15)
    _month_archive(archive, "2024-11", start, end, fillers=[bucket])
    real = csvparse.open_time_lines
    texts: list[str] = []

    def without_the_previous_minute(text: str) -> dict[int, int]:
        texts.append(text)
        lines = real(text)
        if len(texts) == 2:  # the 1m file, read after the native one
            del lines[bucket - MIN]
        return lines

    monkeypatch.setattr(csvparse, "open_time_lines", without_the_previous_minute)
    s = _import(_importer(lake, archive), start, end).qa["summary"]
    assert len(texts) >= 2
    assert s["cross_check_mismatches_explained"] == 0
    assert s["cross_check_mismatches_unexplained"] == 1
    assert s["cross_checks_ok"] is False


def test_evidence_comes_from_the_archive_of_the_month_of_the_candle(
    lake: Lake, archive: FakeArchive
) -> None:
    start, split, end = ms(2024, 10), ms(2024, 11), ms(2024, 12)
    bucket = ms(2024, 11, 5, 10, 15)
    _month_archive(archive, "2024-10", start, split)
    _month_archive(archive, "2024-11", split, end, fillers=[bucket])
    res = _import(_importer(lake, archive), start, end)
    assert res.qa["summary"]["cross_check_mismatches_explained"] == 1
    check = next(
        c for c in res.qa["cross_checks"] if c["interval"] == "15m" and c["explained_mismatches"]
    )
    (entry,) = check["explained_mismatches"]
    assert entry["explanation"]["evidence"]["derived_1m"]["first"]["file"] == _file("2024-11").name


def test_only_a_data_record_can_hold_evidence(lake: Lake, archive: FakeArchive) -> None:
    """A cross-check record never stands in for the 1m archive, even with a matching hash."""
    start, end = ms(2024, 11), ms(2024, 12)
    _month_archive(archive, "2024-11", start, end)
    importer = _importer(lake, archive)
    req = ImportRequest(Market.SPOT, LakeKind.BARS_1M, SYM, start, end)
    record = {
        "type": "archive_monthly",
        "from": to_iso(start),
        "to": to_iso(end),
        "sha256": hashlib.sha256(archive.files[_file("2024-11").relpath][0]).hexdigest(),
    }

    def locate(role: str) -> dict[str, Any] | None:
        sources = [{**record, "role": role}]
        return asyncio.run(importer._locate_1m(req, start + MIN, sources, {}))

    located = locate("data")
    assert located is not None
    assert located["line"] == 2
    assert locate("cross_check") is None
