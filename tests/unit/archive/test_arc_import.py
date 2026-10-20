"""Importer + lake + QA with an in-memory archive (checksums verified) and a JSON-lines codec."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from arc_helpers import (
    DAY,
    HOUR,
    MIN,
    FakeArchive,
    FakeRest,
    kline_values,
    ms,
    rest_klines,
)

from tsclaude.contracts.market import Market
from tsclaude.contracts.timeutil import ManualClock
from tsclaude.marketdata.archive.checksum import ChecksumError
from tsclaude.marketdata.archive.importer import ArchiveImportError, ImportRequest, Importer
from tsclaude.marketdata.archive.paths import ArchiveDataType, ArchiveFile, Period
from tsclaude.marketdata.types import FundingRate, PriceKline
from tsclaude.store.lake import Lake, LakeKind

NOW = ms(2026, 10, 20, 12, 30)
SYM = "BTCUSDT"


def _file(
    label: str,
    *,
    market: Market = Market.SPOT,
    interval: str = "1m",
    data_type: ArchiveDataType = ArchiveDataType.KLINES,
) -> ArchiveFile:
    period = Period.MONTHLY if len(label) == 7 else Period.DAILY
    return ArchiveFile(market, data_type, SYM, interval, period, label)


def _importer(
    lake: Lake, archive: FakeArchive, rest: FakeRest | None = None, **kw: object
) -> Importer:
    rests = {Market.SPOT: rest, Market.USDM: rest} if rest else None
    return Importer(lake, archive, rests, ManualClock(NOW), **kw)  # type: ignore[arg-type]


def _run(importer: Importer, req: ImportRequest):  # type: ignore[no-untyped-def]
    return asyncio.run(importer.run(req))


def _req(
    start: int, end: int, kind: LakeKind = LakeKind.BARS_1M, market: Market = Market.SPOT
) -> ImportRequest:
    return ImportRequest(market, kind, SYM, start, end)


def test_ms_and_us_months_give_contiguous_bars_and_manifest(
    lake: Lake, archive: FakeArchive
) -> None:
    archive.add_klines(_file("2024-12"))  # milliseconds
    archive.add_klines(_file("2025-01"), unit="us", header=True)  # microseconds from 2025
    res = _run(_importer(lake, archive, cross_check_months=0), _req(ms(2024, 12), ms(2025, 2)))
    assert res.dataset_id is not None
    assert res.created
    assert res.rows == 62 * 1440
    assert res.qa["summary"]["missing_interior"] == 0
    assert res.qa["gaps"] == []
    manifest = lake.manifest(res.dataset_id)
    assert manifest["row_count"] == 62 * 1440
    assert manifest["time_range"] == {"from_ms": ms(2024, 12), "to_ms": ms(2025, 1, 31, 23, 59)}
    assert [p["month"] for p in manifest["parts"]] == [12, 1]
    srcs = [s for s in manifest["sources"] if s["role"] == "data"]
    assert [(s["type"], s["timestamp_unit"], s["header_row"]) for s in srcs] == [
        ("archive_monthly", "ms", False),
        ("archive_monthly", "us", True),
    ]
    assert all(len(s["sha256"]) == 64 and s["verified"] for s in srcs)
    assert srcs[0]["url"].endswith("/data/spot/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2024-12.zip")
    assert manifest["request"]["from"] == "2024-12-01T00:00:00.000Z"
    assert (lake.imports_dir / res.dataset_id / "qa_report.json").exists()
    rows = lake.read_part(next(ip.part for ip in lake.index() if ip.part.month == 1))
    assert rows[0]["open_time_ms"] == ms(2025, 1)
    assert rows[0]["source"] == "archive"


def test_reimport_is_idempotent(lake: Lake, archive: FakeArchive, tmp_path: Path) -> None:
    archive.add_klines(_file("2024-12"))
    req = _req(ms(2024, 12), ms(2025, 1))
    first = _run(_importer(lake, archive, cross_check_months=0), req)
    files_before = sorted(p.name for p in lake.lake_dir.rglob("part-*"))
    manifest_before = (lake.imports_dir / str(first.dataset_id) / "manifest.json").read_text()
    clock_later = Importer(lake, archive, None, ManualClock(NOW + 5 * DAY), cross_check_months=0)
    second = asyncio.run(clock_later.run(req))
    assert second.dataset_id == first.dataset_id
    assert not second.created
    assert sorted(p.name for p in lake.lake_dir.rglob("part-*")) == files_before
    assert (lake.imports_dir / str(first.dataset_id) / "manifest.json").read_text() == (
        manifest_before
    )
    # a fresh lake from the same archive files yields the same dataset id (content addressing)
    other = Lake(tmp_path / "other", lake.codec)
    third = _run(_importer(other, archive, cross_check_months=0), req)
    assert third.dataset_id == first.dataset_id


def test_checksum_mismatch_is_hard_error_and_nothing_is_written(
    lake: Lake, archive: FakeArchive
) -> None:
    archive.add_klines(_file("2024-11"))
    archive.add_klines(_file("2024-12"), corrupt_checksum=True)
    with pytest.raises(ChecksumError):
        _run(_importer(lake, archive, cross_check_months=0), _req(ms(2024, 11), ms(2025, 1)))
    assert lake.dataset_ids() == []
    assert list(lake.lake_dir.rglob("part-*")) == []


def test_kline_format_error_aborts(lake: Lake, archive: FakeArchive) -> None:
    f = _file("2024-12")
    archive.add(f, ["not,a,kline"] * 30)
    with pytest.raises((ArchiveImportError, Exception)):
        _run(_importer(lake, archive, cross_check_months=0), _req(ms(2024, 12), ms(2025, 1)))
    assert lake.dataset_ids() == []


def test_qa_finds_injected_gaps_duplicates_violations_zero_volume(
    lake: Lake, archive: FakeArchive
) -> None:
    start = ms(2024, 12)
    bad = kline_values(start + 50 * MIN, MIN)
    bad[2] = bad[4] - 1.0  # high below close: violates the Bar contract
    f = _file("2024-12")
    from arc_helpers import kline_lines

    lines = kline_lines(
        f.start_ms,
        f.end_ms,
        skip=[start + 10 * MIN, start + 11 * MIN, start + 500 * MIN],
        override={start + 50 * MIN: bad},
        zero_volume=[start + m * MIN for m in range(100, 105)],
    )
    lines.insert(30, lines[29])  # identical duplicate
    conflict = lines[40].split(",")
    conflict[4] = f"{float(conflict[4]) + 0.25:.8f}"
    conflict[2] = f"{max(float(conflict[2]), float(conflict[4])):.8f}"
    lines.insert(41, ",".join(conflict))  # differing duplicate
    archive.add(f, lines)
    res = _run(_importer(lake, archive, cross_check_months=0), _req(f.start_ms, f.end_ms))
    s = res.qa["summary"]
    # 3 skipped + 1 rejected minute are missing; nothing was interpolated
    assert s["missing_interior"] == 4
    assert [(g["from_ms"], g["count"]) for g in res.qa["gaps"]] == [
        (start + 10 * MIN, 2),
        (start + 50 * MIN, 1),
        (start + 500 * MIN, 1),
    ]
    assert s["duplicates_identical"] == 1
    assert s["duplicate_conflicts"] == 1
    assert s["rejected_rows"] == 1
    assert "high below" in res.qa["rejected_rows"][0]["reason"]
    assert s["zero_volume_runs"] == 1
    assert res.qa["zero_volume_runs"][0]["count"] == 5
    assert res.rows == 31 * 1440 - 4
    assert lake.manifest(str(res.dataset_id))["gaps"] == res.qa["gaps"]


def test_cross_check_against_native_15m_and_1h(lake: Lake, archive: FakeArchive) -> None:
    for label in ("2024-11", "2024-12"):
        archive.add_klines(_file(label))
        archive.add_klines(_file(label, interval="15m"), step=15 * MIN)
        archive.add_klines(_file(label, interval="1h"), step=HOUR)
    res = _run(_importer(lake, archive), _req(ms(2024, 11), ms(2025, 1)))
    checks = res.qa["cross_checks"]
    assert len(checks) == 4  # 2 sample months x (15m, 1h)
    # the synthetic native klines are generated independently from the 1m rows, so they differ;
    # what matters is that every bucket is compared and mismatches are reported
    assert all(c["compared"] > 0 for c in checks)
    assert {c["interval"] for c in checks} == {"15m", "1h"}
    refs = [s for s in lake.manifest(str(res.dataset_id))["sources"] if s["role"] == "qa_reference"]
    assert len(refs) == 4


def test_cross_check_detects_mismatch_and_passes_when_consistent() -> None:
    from tsclaude.contracts.market import BarSource, Interval
    from tsclaude.marketdata.archive.qa import cross_check
    from tsclaude.marketdata.convert import bar_from_rest
    from tsclaude.marketdata.types import RestKline

    start = ms(2024, 12)
    one_minute = rest_klines(start, start + 2 * HOUR)
    bars = [bar_from_rest(Market.SPOT, SYM, k, BarSource.REST_BACKFILL, 0) for k in one_minute]
    native = []
    for h in range(2):
        group = one_minute[h * 60 : (h + 1) * 60]
        native.append(
            RestKline(
                group[0].open_time_ms,
                group[0].open,
                max(k.high for k in group),
                min(k.low for k in group),
                group[-1].close,
                sum(k.volume for k in group),
                group[-1].close_time_ms,
                sum(k.quote_volume for k in group),
                sum(k.trades for k in group),
                sum(k.taker_buy_volume for k in group),
                sum(k.taker_buy_quote_volume for k in group),
            )
        )
    ok = cross_check(bars, native, Interval.H1)
    assert ok["ok"]
    assert ok["compared"] == 2
    tampered = [
        native[0],
        native[1].__class__(
            **{**{f: getattr(native[1], f) for f in native[1].__slots__}, "close": 1.0}
        ),
    ]
    bad = cross_check(bars, tampered, Interval.H1)
    assert not bad["ok"]
    assert bad["mismatches"] == 1
    assert "close" in bad["mismatch_samples"][0]["diffs"]
    incomplete = cross_check(bars[:-1], native, Interval.H1)
    assert incomplete["derived_incomplete_skipped"] == 1
    assert incomplete["compared"] == 1


def test_missing_monthly_falls_back_to_daily_then_unavailable(
    lake: Lake, archive: FakeArchive
) -> None:
    # no monthly file for 2024-12; only two daily files exist
    archive.add_klines(_file("2024-12-05"))
    archive.add_klines(_file("2024-12-06"))
    res = _run(_importer(lake, archive, cross_check_months=0), _req(ms(2024, 12), ms(2025, 1)))
    assert res.rows == 2 * 1440
    assert res.qa["summary"]["unavailable_files"] == 29
    assert res.qa["uncovered_head"]["count"] == 4 * 1440  # before the first available bar
    assert res.qa["summary"]["missing_interior"] == 0


def test_current_month_daily_files_and_rest_tail(
    lake: Lake, archive: FakeArchive, rest: FakeRest
) -> None:
    rest.klines_by_interval["1m"] = rest_klines
    for d in range(1, 19):  # 2026-10-01 .. 18 published; the 19th is not yet
        archive.add_klines(_file(f"2026-10-{d:02d}"))
    res = _run(
        _importer(lake, archive, rest, cross_check_months=0),
        _req(ms(2026, 10, 1), ms(2026, 10, 21)),
    )
    types = [s["type"] for s in res.sources]
    assert types.count("archive_daily") == 18
    assert types.count("rest") == 2  # 2026-10-19 (publication lag) and today
    # REST only up to the last closed minute (now = 12:30)
    assert res.rows == 19 * 1440 + 12 * 60 + 30
    assert res.qa["summary"]["missing_interior"] == 0
    assert res.qa["summary"]["uncovered_tail"] == 0
    stored = [r for ip in lake.index() for r in lake.read_part(ip.part)]
    assert {r["source"] for r in stored} == {"archive", "rest_backfill"}


def test_daily_klines_use_rest_for_current_month(
    lake: Lake, archive: FakeArchive, rest: FakeRest
) -> None:
    rest.klines_by_interval["1d"] = lambda a, b: rest_klines(a, b, DAY)
    archive.add_klines(_file("2026-09", interval="1d"), step=DAY)
    res = _run(
        _importer(lake, archive, rest), _req(ms(2026, 9, 1), ms(2026, 10, 20), LakeKind.KLINES_1D)
    )
    assert res.rows == 30 + 19
    assert [s["type"] for s in res.sources] == ["archive_monthly", "rest"]


def test_mark_price_archive_with_rest_fallback(
    lake: Lake, archive: FakeArchive, rest: FakeRest
) -> None:
    rest.marks = lambda a, b: [
        PriceKline(k.open_time_ms, k.open, k.high, k.low, k.close, k.close_time_ms)
        for k in rest_klines(a, b)
    ]
    archive.add_klines(
        _file("2024-11", market=Market.USDM, data_type=ArchiveDataType.MARK_PRICE_KLINES)
    )
    res = _run(
        _importer(lake, archive, rest),
        _req(ms(2024, 11), ms(2025, 1), LakeKind.MARK_PRICE_1M, Market.USDM),
    )
    # 2024-12 has no archive file at all (monthly and dailies missing) and is too old for REST
    assert res.rows == 30 * 1440
    assert res.qa["summary"]["unavailable_files"] == 31
    archive.add(
        _file("2024-12", market=Market.USDM, data_type=ArchiveDataType.MARK_PRICE_KLINES),
        ["bad"] * 50,
    )
    res2 = _run(
        _importer(lake, archive, rest),
        _req(ms(2024, 12), ms(2025, 1), LakeKind.MARK_PRICE_1M, Market.USDM),
    )
    assert res2.rows == 31 * 1440  # undocumented archive format rejected -> REST used
    assert any("REST used" in n for n in res2.qa["unit_notes"])


def test_funding_history_paginates_over_rest(
    lake: Lake, archive: FakeArchive, rest: FakeRest
) -> None:
    t0 = ms(2024, 1)
    rest.funding = [
        FundingRate(SYM, 0.0001 * (i % 3), t0 + i * 8 * HOUR, 100.0 + i) for i in range(2500)
    ]
    res = _run(
        _importer(lake, archive, rest),
        _req(t0, t0 + 2500 * 8 * HOUR, LakeKind.FUNDING, Market.USDM),
    )
    assert res.rows == 2500
    assert len([c for c in rest.calls if c[0] == "funding"]) == 3
    assert res.qa["funding"]["interval_hours_seen"] == [8]
    rows = [r for ip in lake.index() for r in lake.read_part(ip.part)]
    assert len(rows) == 2500
    assert rows[0]["mark_price"] == 100.0


def test_empty_import_commits_nothing(lake: Lake, archive: FakeArchive) -> None:
    res = _run(_importer(lake, archive, cross_check_months=0), _req(ms(2023, 1), ms(2023, 3)))
    assert res.dataset_id is None
    assert lake.dataset_ids() == []
    assert res.qa["uncovered_head"]["count"] == (31 + 28) * 1440


def test_verify_detects_tampering(lake: Lake, archive: FakeArchive) -> None:
    archive.add_klines(_file("2024-12"))
    res = _run(_importer(lake, archive, cross_check_months=0), _req(ms(2024, 12), ms(2025, 1)))
    dsid = str(res.dataset_id)
    assert lake.verify(dsid)["ok"]
    part = lake.data_dir / lake.manifest(dsid)["parts"][0]["path"]
    lines = part.read_text().splitlines()
    row = json.loads(lines[5])
    row["close"] += 1
    lines[5] = json.dumps(row)
    part.write_text("\n".join(lines) + "\n")
    report = lake.verify(dsid)
    assert not report["ok"]
    assert any("file hash mismatch" in e for e in report["errors"])
    assert any("content hash mismatch" in e for e in report["errors"])
