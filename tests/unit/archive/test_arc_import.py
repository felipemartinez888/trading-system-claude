"""Importer + lake + QA with an in-memory archive (checksums verified) and a JSON-lines codec."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from arc_helpers import (
    DAY,
    HOUR,
    MIN,
    FakeArchive,
    FakeRest,
    JsonLinesCodec,
    YieldingArchive,
    edge_lines,
    klay_2024_10_lines,
    kline_values,
    ms,
    rest_klines,
)
from legacy_importer import LegacyImporter

from tsclaude.contracts.market import Bar, Market
from tsclaude.contracts.timeutil import ManualClock
from tsclaude.marketdata.archive.checksum import ChecksumError
from tsclaude.marketdata.archive.importer import ArchiveImportError, Importer, ImportRequest
from tsclaude.marketdata.archive.paths import ArchiveDataType, ArchiveFile, Period
from tsclaude.marketdata.types import FundingRate, PriceKline
from tsclaude.store.lake import Lake, LakeKind
from tsclaude.store.lake_writer import LakeWriter, Staging

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
    writer = LakeWriter(lake.data_dir, lake.codec)
    return Importer(writer, archive, rests, ManualClock(NOW), **kw)  # type: ignore[arg-type]


def _legacy_importer(
    lake: Lake, archive: FakeArchive, rest: FakeRest | None = None, **kw: object
) -> Importer:
    rests = {Market.SPOT: rest, Market.USDM: rest} if rest else None
    writer = LakeWriter(lake.data_dir, lake.codec)
    return LegacyImporter(writer, archive, rests, ManualClock(NOW), **kw)  # type: ignore[arg-type]


def _run(importer: Importer, req: ImportRequest):  # type: ignore[no-untyped-def]
    res = asyncio.run(importer.run(req))
    return res


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
    lake.refresh()
    rows = lake.read_part(next(ip.part for ip in lake.index() if ip.part.month == 1))
    assert rows[0]["open_time_ms"] == ms(2025, 1)
    assert rows[0]["source"] == "archive"


def test_mixed_unit_archive_is_imported_with_provenance(lake: Lake, archive: FakeArchive) -> None:
    f = ArchiveFile(
        Market.SPOT, ArchiveDataType.KLINES, "KLAYUSDT", "1d", Period.MONTHLY, "2024-10"
    )
    archive.add(f, klay_2024_10_lines())
    zip_bytes, _ = archive.files[f.relpath]
    req = ImportRequest(Market.SPOT, LakeKind.KLINES_1D, "KLAYUSDT", f.start_ms, f.end_ms)
    res = _run(_importer(lake, archive, cross_check_months=0), req)
    assert res.dataset_id is not None
    assert res.rows == 28  # 27 ms days + the valid µs day; two malformed rows skipped
    assert res.qa["summary"]["malformed_rows"] == 2
    assert [m["line"] for m in res.qa["malformed_rows"]] == [28, 30]
    manifest = lake.manifest(res.dataset_id)
    (src,) = [s for s in manifest["sources"] if s["role"] == "data"]
    assert src["sha256"] == hashlib.sha256(zip_bytes).hexdigest()  # original archive untouched
    assert src["timestamp_unit"] == "ms"  # existing key keeps its meaning
    assert src["timestamp_units"] == {"ms": 28, "us": 2}
    assert src["timestamp_unit_transitions"] == [{"line": 29, "from": "ms", "to": "us"}]
    assert any("mixed timestamp units" in n for n in res.qa["unit_notes"])
    lake.refresh()
    rows = lake.read_part(next(iter(lake.index())).part)
    assert rows[-1]["open_time_ms"] == ms(2024, 10, 29)
    assert rows[-1]["close_time_ms"] == ms(2024, 10, 30) - 1


def test_homogeneous_archives_have_no_mixed_unit_metadata(lake: Lake, archive: FakeArchive) -> None:
    archive.add_klines(_file("2025-01"), unit="us")
    res = _run(_importer(lake, archive, cross_check_months=0), _req(ms(2025, 1), ms(2025, 2)))
    (src,) = [s for s in res.sources if s["role"] == "data"]
    assert "timestamp_units" not in src
    assert "timestamp_unit_transitions" not in src
    assert res.qa["unit_notes"] == []


def test_reimport_is_idempotent(lake: Lake, archive: FakeArchive, tmp_path: Path) -> None:
    archive.add_klines(_file("2024-12"))
    req = _req(ms(2024, 12), ms(2025, 1))
    first = _run(_importer(lake, archive, cross_check_months=0), req)
    files_before = sorted(p.name for p in lake.lake_dir.rglob("part-*"))
    manifest_before = (lake.imports_dir / str(first.dataset_id) / "manifest.json").read_text()
    clock_later = Importer(
        LakeWriter(lake.data_dir, lake.codec),
        archive,
        None,
        ManualClock(NOW + 5 * DAY),
        cross_check_months=0,
    )
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
    assert list(lake.data_dir.rglob("part-*")) == []
    assert not (lake.data_dir / ".staging").exists()  # the failed import's staging is gone


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
    lake.refresh()
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
    lake.refresh()
    open_parts = [ip.part for ip in lake.index() if not ip.part.sealed]
    sealed_parts = [ip.part for ip in lake.index() if ip.part.sealed]
    rows = [r for p in open_parts for r in lake.read_part(p)]
    assert len(rows) + sum(p.rows for p in sealed_parts) == 2500
    assert sealed_parts  # 2026 funding lands under the sealed root
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


def test_holdout_month_goes_to_sealed_root_and_qa_has_no_prices(
    lake: Lake, archive: FakeArchive
) -> None:
    from arc_helpers import kline_lines

    f = _file("2026-02")
    start = f.start_ms
    bad = kline_values(start + 7 * MIN, MIN)
    bad[2] = bad[4] - 1.0  # violation inside the holdout
    lines = kline_lines(f.start_ms, f.end_ms, unit="us", override={start + 7 * MIN: bad})
    conflict = lines[20].split(",")
    conflict[4] = f"{float(conflict[4]) + 0.25:.8f}"
    conflict[2] = f"{max(float(conflict[2]), float(conflict[4])):.8f}"
    lines.insert(21, ",".join(conflict))
    archive.add(f, lines)
    res = _run(_importer(lake, archive), _req(f.start_ms, f.end_ms))
    assert all(p.path.startswith("lake_sealed/") for p in res.parts)
    (rejected,) = res.qa["rejected_rows"]
    assert "row" not in rejected
    assert rejected["redacted"]
    assert rejected["open_time_ms"] == start + 7 * MIN
    (dup,) = res.qa["duplicate_conflicts"]
    assert "kept" not in dup
    assert "dropped" not in dup
    assert res.qa["cross_checks"] == []  # no holdout month is used for the price cross-check
    report = (lake.imports_dir / str(res.dataset_id) / "qa_report.json").read_text()
    assert str(bad[4]) not in report


def test_unknown_first_row_is_malformed_not_header(lake: Lake, archive: FakeArchive) -> None:
    from arc_helpers import kline_lines

    f = _file("2024-12")
    archive.add(f, ["time,o,h,l,c", *kline_lines(f.start_ms, f.end_ms)])
    res = _run(_importer(lake, archive, cross_check_months=0), _req(f.start_ms, f.end_ms))
    assert res.qa["summary"]["malformed_rows"] == 1
    data = [s for s in res.sources if s["role"] == "data"]
    assert data[0]["header_row"] is False


# ---------------------------------------------------------------------- AP-1: bounded importer
def _bounded_archive(scenario: str) -> YieldingArchive:
    """Sparse multi-month archives (edges of each month only) that exercise every QA path."""
    a = YieldingArchive()
    if scenario == "open":
        f = _file("2024-11")
        lines = edge_lines(f, 200, zero_tail=5)
        bad = kline_values(f.start_ms + 7 * MIN, MIN)
        bad[2] = bad[4] - 1.0  # high below close: rejected
        lines[7] = ",".join(f"{v:.8f}" if isinstance(v, float) else str(v) for v in bad)
        lines.insert(21, lines[20])  # identical duplicate
        conflict = lines[40].split(",")
        conflict[4] = f"{float(conflict[4]) + 0.25:.8f}"
        conflict[2] = f"{max(float(conflict[2]), float(conflict[4])):.8f}"
        lines.insert(41, ",".join(conflict))  # differing duplicate
        a.add(f, lines)
        f = _file("2024-12")  # microseconds + header; its zero head continues the previous run
        a.add(f, edge_lines(f, 200, unit="us", zero_head=5), header=True)
        a.add_klines(_file("2025-01-05"))  # no monthly file: only two daily files exist
        a.add_klines(_file("2025-01-06"))
        f = _file("2025-02")
        a.add(f, edge_lines(f, 200))
        for label in ("2024-11", "2025-02"):  # native references for the cross-check
            a.add_klines(_file(label, interval="15m"), step=15 * MIN)
        a.add_klines(_file("2024-11", interval="1h"), step=HOUR)  # 2025-02 1h is missing
    else:  # "holdout": an open month followed by two sealed months
        f = _file("2025-12")
        a.add(f, edge_lines(f, 200))
        a.add_klines(_file("2025-12", interval="15m"), step=15 * MIN)
        a.add_klines(_file("2025-12", interval="1h"), step=HOUR)
        f = _file("2026-01")
        a.add(f, edge_lines(f, 200, unit="us"))
        f = _file("2026-02")
        bad = kline_values(f.start_ms + 7 * MIN, MIN)
        bad[2] = bad[4] - 1.0  # violation inside the holdout
        lines = edge_lines(f, 200, unit="us")
        fields = [f"{v:.8f}" if isinstance(v, float) else str(v) for v in bad]
        fields[0] = str(int(fields[0]) * 1000)  # microseconds, like the rest of the file
        fields[6] = str(int(fields[6]) * 1000 + 999)
        lines[7] = ",".join(fields)
        conflict = lines[20].split(",")
        conflict[4] = f"{float(conflict[4]) + 0.25:.8f}"
        conflict[2] = f"{max(float(conflict[2]), float(conflict[4])):.8f}"
        lines.insert(21, ",".join(conflict))  # conflicting duplicate inside the holdout
        a.add(f, lines)
    return a


_SCENARIOS = {
    "open": (ms(2024, 11), ms(2025, 3)),
    "holdout": (ms(2025, 12), ms(2026, 3)),
}


def _lake_at(tmp_path: Path, name: str) -> Lake:
    return Lake(tmp_path / name, JsonLinesCodec())


@pytest.mark.parametrize("scenario", ["open", "holdout"])
@pytest.mark.parametrize("concurrency", [1, 3])
def test_bounded_importer_matches_the_legacy_pipeline(
    tmp_path: Path, scenario: str, concurrency: int
) -> None:
    """T2: identical dataset id, part content hashes, manifest, QA report and source records.

    The reference is the frozen pre-change pipeline run sequentially (month_concurrency=1): with
    concurrent months its QA lists (unit notes, unavailable files) came out in task-completion
    order, the bounded pipeline always emits them in month order, so its output no longer depends
    on `month_concurrency`.

    Covers ms/us months, a header row, identical and conflicting duplicates, a rejected row, a
    missing monthly file (daily fallback, unavailable days), a zero-volume run across a month
    boundary, a missing native cross-check file, and the sealed holdout months."""
    start, end = _SCENARIOS[scenario]
    archive = _bounded_archive(scenario)
    new_lake = _lake_at(tmp_path, "new")
    old_lake = _lake_at(tmp_path, "old")
    new = asyncio.run(
        _importer(new_lake, archive, month_concurrency=concurrency).run(_req(start, end))
    )
    old = asyncio.run(
        _legacy_importer(old_lake, archive, month_concurrency=1).run(_req(start, end))
    )
    assert new.dataset_id is not None
    assert new.dataset_id == old.dataset_id
    assert [(p.path, p.content_hash, p.rows) for p in new.parts] == [
        (p.path, p.content_hash, p.rows) for p in old.parts
    ]
    assert new.parts == old.parts  # including file hashes and time ranges
    assert new.rows == old.rows
    assert new.qa == old.qa
    assert new.sources == old.sources
    dsid = new.dataset_id
    for name in ("manifest.json", "qa_report.json"):
        assert (new_lake.imports_dir / dsid / name).read_text() == (
            old_lake.imports_dir / dsid / name
        ).read_text()
    if scenario == "open":
        s = new.qa["summary"]
        assert s["rejected_rows"] == 1
        assert s["duplicates_identical"] == 1
        assert s["duplicate_conflicts"] == 1
        assert s["unavailable_files"] == 29  # 31 January days minus the two daily files
        assert [(r["from"], r["count"]) for r in new.qa["zero_volume_runs"]] == [
            ("2024-11-30T23:55:00.000Z", 10)  # 5 + 5 minutes, joined across the boundary
        ]
        assert any(c["ok"] is None for c in new.qa["cross_checks"])  # the missing 1h reference


def test_holdout_months_stay_sealed_over_a_multi_month_request(tmp_path: Path) -> None:
    """T7: parts of 2026 months land under lake_sealed, nothing of 2026 under lake, and no sealed
    month is used for the price cross-check or leaks prices into the QA output."""
    start, end = _SCENARIOS["holdout"]
    lake = _lake_at(tmp_path, "d")
    res = asyncio.run(
        _importer(lake, _bounded_archive("holdout"), month_concurrency=3).run(_req(start, end))
    )
    by_month = {(p.year, p.month): p.path.split("/")[0] for p in res.parts}
    assert by_month == {(2025, 12): "lake", (2026, 1): "lake_sealed", (2026, 2): "lake_sealed"}
    assert not [p for p in lake.lake_dir.rglob("part-*") if "year=2026" in str(p)]
    assert {c["month"] for c in res.qa["cross_checks"]} == {"2025-12"}
    (rejected,) = res.qa["rejected_rows"]
    assert rejected["redacted"]
    assert "row" not in rejected
    (conflict,) = res.qa["duplicate_conflicts"]
    assert "kept" not in conflict
    assert "dropped" not in conflict


@pytest.mark.parametrize("legacy", [False, True])
def test_each_row_is_validated_as_one_bar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy: bool
) -> None:
    """T5: exactly one `Bar` construction per merged row (accepted or rejected). The frozen
    pre-change pipeline constructs a second one for every accepted row."""
    constructed = 0
    original = Bar.__init__

    def counting(self: Bar, **data: object) -> None:
        nonlocal constructed
        constructed += 1
        original(self, **data)

    monkeypatch.setattr(Bar, "__init__", counting)
    build = _legacy_importer if legacy else _importer
    start, end = _SCENARIOS["open"]
    res = asyncio.run(
        build(_lake_at(tmp_path, "t5"), _bounded_archive("open"), cross_check_months=0).run(
            _req(start, end)
        )
    )
    rejected = res.qa["summary"]["rejected_rows"]
    assert rejected == 1
    assert constructed == (2 * res.rows if legacy else res.rows) + rejected


@pytest.mark.parametrize("concurrency", [1, 3])
def test_failure_in_month_k_publishes_nothing_and_rerun_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, concurrency: int
) -> None:
    """T6: a checksum error in month 4 of 5 leaves no part, manifest or staging directory, no
    sibling month writes after the abort, and the corrected re-run is idempotent."""
    labels = ["2024-08", "2024-09", "2024-10", "2024-11", "2024-12"]
    archive = YieldingArchive()
    for label in labels:
        f = _file(label)
        archive.add(f, edge_lines(f, 100), corrupt_checksum=label == "2024-11")
    closed_at_write: list[bool] = []
    original = Staging.write_parts

    def spy(self: Staging, *a: Any, **k: Any) -> Any:
        closed_at_write.append(self._closed)
        return original(self, *a, **k)

    monkeypatch.setattr(Staging, "write_parts", spy)
    lake = _lake_at(tmp_path, "t6")
    req = _req(ms(2024, 8), ms(2025, 1))
    with pytest.raises(ChecksumError):
        asyncio.run(_importer(lake, archive, month_concurrency=concurrency).run(req))
    if concurrency == 1:
        assert len(closed_at_write) >= 3  # months 1-3 were staged before month 4 failed
    assert closed_at_write.count(True) == 0  # nothing wrote after the abort
    assert lake.dataset_ids() == []
    assert list(lake.data_dir.rglob("part-*")) == []
    assert not (lake.data_dir / ".staging").exists()

    f = _file("2024-11")
    archive.add(f, edge_lines(f, 100))  # corrected publication
    first = asyncio.run(_importer(lake, archive, month_concurrency=concurrency).run(req))
    assert first.created
    assert first.rows == 5 * 200
    files = sorted(p.name for p in lake.lake_dir.rglob("part-*"))
    manifest = (lake.imports_dir / str(first.dataset_id) / "manifest.json").read_text()
    again = asyncio.run(_importer(lake, archive, month_concurrency=concurrency).run(req))
    assert again.dataset_id == first.dataset_id
    assert not again.created
    assert sorted(p.name for p in lake.lake_dir.rglob("part-*")) == files
    assert (lake.imports_dir / str(first.dataset_id) / "manifest.json").read_text() == manifest
    fresh = asyncio.run(
        _importer(_lake_at(tmp_path, "t6-fresh"), archive, month_concurrency=concurrency).run(req)
    )
    assert fresh.dataset_id == first.dataset_id  # the failed attempt left no trace in the result


def test_failed_request_cancels_the_other_months(tmp_path: Path) -> None:
    """T6 (cancellation): when one month fails, the months still fetching are cancelled and
    drained before `run` raises - nothing keeps running against the closed staging area."""

    class SlowArchive(YieldingArchive):
        fetches_finished = 0

        async def fetch(self, f: ArchiveFile):  # type: ignore[no-untyped-def]
            if "2024-08" in f.relpath:  # the first month fails immediately
                return await FakeArchive.fetch(self, f)
            await asyncio.sleep(0.05)
            out = await FakeArchive.fetch(self, f)
            type(self).fetches_finished += 1
            return out

    archive = SlowArchive()
    for label in ("2024-08", "2024-09", "2024-10", "2024-11", "2024-12"):
        f = _file(label)
        archive.add(f, edge_lines(f, 100), corrupt_checksum=label == "2024-08")
    lake = _lake_at(tmp_path, "cancel")
    importer = _importer(lake, archive, month_concurrency=3)

    async def scenario() -> int:
        with pytest.raises(ChecksumError):
            await importer.run(_req(ms(2024, 8), ms(2025, 1)))
        pending = len(asyncio.all_tasks()) - 1  # everything except this coroutine
        await asyncio.sleep(0.2)  # long enough for un-cancelled siblings to finish fetching
        return pending

    pending = asyncio.run(scenario())
    assert pending == 0
    assert SlowArchive.fetches_finished == 0
    assert lake.dataset_ids() == []
    assert not (lake.data_dir / ".staging").exists()
