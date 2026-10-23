from __future__ import annotations

from datetime import date

import pytest
from arc_helpers import (
    DAY,
    HEADER,
    MIN,
    klay_2024_10_lines,
    kline_lines,
    make_zip,
    ms,
    price_at,
)

from tsclaude.contracts.market import Market
from tsclaude.marketdata.archive import csvparse
from tsclaude.marketdata.archive.checksum import ChecksumError, parse_checksum, verify
from tsclaude.marketdata.archive.csvparse import ArchiveFormatError
from tsclaude.marketdata.archive.paths import (
    ArchiveDataType,
    ArchiveFile,
    Period,
    PlanKind,
    archive_file,
    plan,
)

DEC24 = (ms(2024, 12), ms(2025, 1))
JAN25 = (ms(2025, 1), ms(2025, 2))


def test_archive_urls_follow_documented_layout() -> None:
    spot = ArchiveFile(
        Market.SPOT, ArchiveDataType.KLINES, "BTCUSDT", "1m", Period.MONTHLY, "2024-12"
    )
    assert spot.url() == (
        "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2024-12.zip"
    )
    assert spot.checksum_url().endswith("BTCUSDT-1m-2024-12.zip.CHECKSUM")
    fut = ArchiveFile(
        Market.USDM, ArchiveDataType.MARK_PRICE_KLINES, "ETHUSDT", "1m", Period.DAILY, "2026-10-19"
    )
    assert fut.relpath == (
        "data/futures/um/daily/markPriceKlines/ETHUSDT/1m/ETHUSDT-1m-2026-10-19.zip"
    )
    assert (fut.start_ms, fut.end_ms) == (ms(2026, 10, 19), ms(2026, 10, 20))
    assert (spot.start_ms, spot.end_ms) == DEC24


def test_plan_monthly_daily_rest() -> None:
    today = date(2026, 10, 20)
    items = plan(ms(2026, 8, 15), ms(2026, 10, 20, 12), today)
    kinds = [(i.kind, i.label) for i in items]
    assert kinds[:2] == [(PlanKind.MONTHLY, "2026-08"), (PlanKind.MONTHLY, "2026-09")]
    dailies = [label for k, label in kinds if k is PlanKind.DAILY]
    assert dailies == [f"2026-10-{d:02d}" for d in range(1, 20)]  # up to yesterday
    assert kinds[-1] == (PlanKind.REST, "rest")
    assert (items[-1].start_ms, items[-1].end_ms) == (ms(2026, 10, 20), ms(2026, 10, 20, 12))
    assert items[0].start_ms == ms(2026, 8, 15)  # monthly file, filtered to the request
    with pytest.raises(ValueError, match="markPriceKlines"):
        archive_file(Market.SPOT, ArchiveDataType.MARK_PRICE_KLINES, "X", "1m", items[0])


def test_checksum_parsing_and_verification() -> None:
    data = b"zip bytes"
    import hashlib

    digest = hashlib.sha256(data).hexdigest()
    assert parse_checksum(f"{digest}  BTCUSDT-1m-2024-12.zip\n", "BTCUSDT-1m-2024-12.zip") == digest
    assert verify(data, digest.upper(), "x") == digest
    with pytest.raises(ChecksumError, match="mismatch"):
        verify(b"other", digest, "x")
    with pytest.raises(ChecksumError, match="expected"):
        parse_checksum(f"{digest}  OTHER.zip", "BTCUSDT-1m-2024-12.zip")
    with pytest.raises(ChecksumError, match="malformed"):
        parse_checksum("nonsense", "a.zip")


def test_ms_file_and_us_file_normalize_to_contiguous_ms() -> None:
    dec = csvparse.parse_klines_csv("\n".join(kline_lines(*DEC24)), Market.SPOT, DEC24, MIN)
    jan = csvparse.parse_klines_csv(
        "\n".join(kline_lines(*JAN25, unit="us")), Market.SPOT, JAN25, MIN
    )
    assert (dec.unit, jan.unit) == ("ms", "us")
    assert dec.issues.unit_note is None
    assert jan.issues.unit_note is None
    assert dec.rows[-1].open_time_ms == ms(2024, 12, 31, 23, 59)
    assert jan.rows[0].open_time_ms == ms(2025, 1, 1)
    assert jan.rows[0].open_time_ms - dec.rows[-1].open_time_ms == MIN
    assert jan.rows[0].close_time_ms == ms(2025, 1, 1) + MIN - 1
    assert len(dec.rows) == 31 * 1440
    assert len(jan.rows) == 31 * 1440


def test_header_row_is_tolerated() -> None:
    text = HEADER + "\n" + "\n".join(kline_lines(DEC24[0], DEC24[0] + 3 * MIN))
    parsed = csvparse.parse_klines_csv(text, Market.USDM, DEC24, MIN)
    assert parsed.issues.header_skipped
    assert len(parsed.rows) == 3


def test_unit_against_date_expectation_is_noted_not_guessed() -> None:
    # a spot 2025 file in ms: magnitude decides, the mismatch is reported
    parsed = csvparse.parse_klines_csv(
        "\n".join(kline_lines(JAN25[0], JAN25[0] + 2 * MIN)), Market.SPOT, JAN25, MIN
    )
    assert parsed.unit == "ms"
    assert parsed.issues.unit_note is not None
    assert "expected us" in parsed.issues.unit_note


def test_reverse_unit_change_and_wrong_period_are_hard_errors() -> None:
    ms_lines = kline_lines(JAN25[0], JAN25[0] + 2 * MIN)
    us_lines = kline_lines(JAN25[0] + 2 * MIN, JAN25[0] + 4 * MIN, unit="us")
    with pytest.raises(ArchiveFormatError, match="unit"):
        csvparse.parse_klines_csv("\n".join(us_lines + ms_lines), Market.SPOT, JAN25, MIN)
    with pytest.raises(ArchiveFormatError, match="outside the file period"):
        csvparse.parse_klines_csv("\n".join(ms_lines), Market.SPOT, DEC24, MIN)
    with pytest.raises(ArchiveFormatError, match="neither"):
        csvparse.parse_klines_csv("12345,1,1,1,1,1,12346,1,1,1,1,0", Market.SPOT, JAN25, MIN)


OCT24 = (ms(2024, 10), ms(2024, 11))


def test_klayusdt_2024_10_mixed_unit_file_is_normalized_per_row() -> None:
    parsed = csvparse.parse_klines_csv("\n".join(klay_2024_10_lines()), Market.SPOT, OCT24, DAY)
    days = [ms(2024, 10, d) for d in range(1, 28)] + [ms(2024, 10, 29)]
    assert [r.open_time_ms for r in parsed.rows] == days
    assert parsed.unit == "ms"  # first row; consumers of `unit` see the same value as before
    assert parsed.issues.unit_counts == {"ms": 28, "us": 2}
    assert parsed.issues.unit_transitions == [(29, "ms", "us")]
    assert parsed.issues.mixed_units
    assert parsed.issues.unit_note is None
    us_row = parsed.rows[-1]
    assert (us_row.open_time_ms, us_row.close_time_ms) == (1730160000000, 1730246399999)
    # the truncated ms bar and the µs row with a stale close time are rejected, not repaired
    assert [n for n, _ in parsed.issues.malformed_rows] == [28, 30]
    assert all("close time != open time" in reason for _, reason in parsed.issues.malformed_rows)


def test_homogeneous_files_report_a_single_unit() -> None:
    for unit in ("ms", "us"):
        lines = kline_lines(*JAN25, unit=unit)[:10]
        parsed = csvparse.parse_klines_csv("\n".join(lines), Market.SPOT, JAN25, MIN)
        assert parsed.issues.unit_counts == {unit: 10}
        assert parsed.issues.unit_transitions == []
        assert not parsed.issues.mixed_units


def _mixed(first: str, second: str, third: str | None = None) -> str:
    """Three-day blocks in the given units, consecutive in time, within Jan 2025."""
    units = [u for u in (first, second, third) if u]
    lines: list[str] = []
    for i, unit in enumerate(units):
        lo = JAN25[0] + 3 * i * DAY
        lines += kline_lines(lo, lo + 3 * DAY, DAY, unit=unit)
    return "\n".join(lines)


def test_mixed_unit_rules_are_strict() -> None:
    ok = csvparse.parse_klines_csv(_mixed("ms", "us"), Market.SPOT, JAN25, DAY)
    assert len(ok.rows) == 6
    assert ok.issues.unit_transitions == [(4, "ms", "us")]
    for text in (_mixed("us", "ms"), _mixed("ms", "us", "ms")):
        with pytest.raises(ArchiveFormatError, match="unit changes"):
            csvparse.parse_klines_csv(text, Market.SPOT, JAN25, DAY)
    # the change must move forward in time
    back = kline_lines(JAN25[0] + 5 * DAY, JAN25[0] + 6 * DAY, DAY)
    fwd = kline_lines(JAN25[0] + 1 * DAY, JAN25[0] + 2 * DAY, DAY, unit="us")
    with pytest.raises(ArchiveFormatError, match="not after the previous row"):
        csvparse.parse_klines_csv("\n".join(back + fwd), Market.SPOT, JAN25, DAY)


def test_ambiguous_or_out_of_range_timestamps_are_hard_errors() -> None:
    base = kline_lines(JAN25[0], JAN25[0] + 2 * DAY, DAY)
    cells = kline_lines(JAN25[0] + 2 * DAY, JAN25[0] + 3 * DAY, DAY, unit="us")[0].split(",")
    mixed_row = ",".join([*cells[:6], str(int(cells[6]) // 1000), *cells[7:]])
    with pytest.raises(ArchiveFormatError, match="differs from close time unit"):
        csvparse.parse_klines_csv("\n".join([*base, mixed_row]), Market.SPOT, JAN25, DAY)
    junk = ",".join(["12345", *cells[1:6], "12346", *cells[7:]])
    with pytest.raises(ArchiveFormatError, match="neither ms nor microseconds"):
        csvparse.parse_klines_csv("\n".join([*base, junk]), Market.SPOT, JAN25, DAY)
    # a µs row whose normalized time is outside the file period
    late = kline_lines(JAN25[1], JAN25[1] + DAY, DAY, unit="us")
    with pytest.raises(ArchiveFormatError, match="outside the file period"):
        csvparse.parse_klines_csv("\n".join([*base, *late]), Market.SPOT, JAN25, DAY)


def test_malformed_rows_counted_until_threshold() -> None:
    lines = kline_lines(*DEC24)
    lines[10] = lines[10].replace(",", ";", 3)
    cells = lines[20].split(",")
    cells[2] = "abc"
    lines[20] = ",".join(cells)
    parsed = csvparse.parse_klines_csv("\n".join(lines), Market.SPOT, DEC24, MIN)
    assert [n for n, _ in parsed.issues.malformed_rows] == [11, 21]
    assert len(parsed.rows) == 31 * 1440 - 2
    few = kline_lines(DEC24[0], DEC24[0] + 20 * MIN)
    broken = [ln.replace(",", ";") for ln in few[:10]] + few[10:]
    with pytest.raises(ArchiveFormatError, match="malformed"):
        csvparse.parse_klines_csv("\n".join(broken), Market.SPOT, DEC24, MIN)


def test_zip_must_hold_one_csv() -> None:
    text = csvparse.read_single_csv(make_zip("A-1m-2024-12.csv", "x"), "A-1m-2024-12.csv")
    assert text == "x"
    with pytest.raises(ArchiveFormatError, match="expected"):
        csvparse.read_single_csv(make_zip("B.csv", "x"), "A-1m-2024-12.csv")
    with pytest.raises(ArchiveFormatError, match="zip"):
        csvparse.read_single_csv(b"not a zip")


def test_daily_kline_files() -> None:
    lines = kline_lines(DEC24[0], DEC24[1], DAY)
    parsed = csvparse.parse_klines_csv("\n".join(lines), Market.SPOT, DEC24, DAY)
    assert len(parsed.rows) == 31
    assert parsed.rows[0].close_time_ms == DEC24[0] + DAY - 1


def test_price_klines_follow_the_same_unit_rules() -> None:
    parsed = csvparse.parse_price_klines_csv(_mixed("ms", "us"), Market.USDM, JAN25, DAY)
    assert len(parsed.rows) == 6
    assert parsed.issues.unit_counts == {"ms": 3, "us": 3}
    with pytest.raises(ArchiveFormatError, match="unit changes"):
        csvparse.parse_price_klines_csv(_mixed("us", "ms"), Market.USDM, JAN25, DAY)


def test_price_klines() -> None:
    lines = kline_lines(*JAN25)[:5]
    parsed = csvparse.parse_price_klines_csv("\n".join(lines), Market.USDM, JAN25, MIN)
    assert len(parsed.rows) == 5
    assert parsed.rows[0].close == price_at(JAN25[0]) + 0.125
