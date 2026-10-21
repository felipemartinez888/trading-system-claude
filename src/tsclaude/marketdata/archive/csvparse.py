"""One kline CSV parser for spot and USD-M archive files.

Columns (binance-public-data README, identical for spot and USD-M): open time, open, high, low,
close, volume, close time, quote asset volume, number of trades, taker buy base volume, taker buy
quote volume, ignore. Some files start with a header row; it is skipped only if it is exactly
the known header text (`KNOWN_HEADERS`); any other non-numeric row counts as malformed.

Timestamp unit (M0 spike): spot files are in **microseconds from 2025-01-01**, milliseconds before.
The unit is detected *per file* from the magnitude of the first row and then enforced for every
row; the normalized times must fall inside the file's own period (month or day). Any violation is a
hard `ArchiveFormatError`, so a mis-detected unit can never silently shift bars by 1000x.

A malformed data row (wrong column count, non-numeric value) is recorded and skipped -- its minute
becomes a gap in the QA report, nothing is interpolated -- unless malformed rows exceed a small
threshold, which means the format changed: then the whole file is rejected.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Literal

from tsclaude.contracts.market import Market
from tsclaude.marketdata.types import PriceKline, RestKline

type TimeUnit = Literal["ms", "us"]

_INT = re.compile(r"-?[0-9]{1,19}")
_MS_RANGE = (10**11, 10**14)  # 1973 .. 5138 in ms
_US_RANGE = (10**14, 10**17)
SPOT_MICROS_FROM_MS = 1_735_689_600_000  # 2025-01-01T00:00:00Z
# Header row used by Binance's kline CSVs (USD-M files, newer spot files). Only this exact text
# is skipped as a header; any other non-numeric row is a malformed data row.
KNOWN_HEADERS = frozenset(
    {
        "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,"
        "taker_buy_quote_volume,ignore",
    }
)
MAX_MALFORMED_FRACTION = 0.01
MAX_MALFORMED_ABSOLUTE = 5


class ArchiveFormatError(ValueError):
    """The file does not match the documented format. Hard error: nothing from it is used."""


@dataclass
class ParseIssues:
    malformed_rows: list[tuple[int, str]] = field(default_factory=list)  # (line number, reason)
    header_skipped: bool = False
    unit_note: str | None = None  # unit differs from the date-based expectation (kept, reported)


@dataclass
class ParsedKlines:
    rows: list[RestKline]
    unit: TimeUnit
    issues: ParseIssues


@dataclass
class ParsedPriceKlines:
    rows: list[PriceKline]
    unit: TimeUnit
    issues: ParseIssues


def read_single_csv(zip_bytes: bytes, expected_csv_name: str | None = None) -> str:
    """The text of the one CSV inside an archive zip."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            if len(names) != 1 or not names[0].lower().endswith(".csv"):
                raise ArchiveFormatError(f"zip must contain exactly one CSV, found {names}")
            if expected_csv_name is not None and names[0].rsplit("/", 1)[-1] != expected_csv_name:
                raise ArchiveFormatError(f"zip contains {names[0]!r}, expected {expected_csv_name}")
            return zf.read(names[0]).decode("utf-8")
    except zipfile.BadZipFile as exc:
        raise ArchiveFormatError("not a valid zip file") from exc
    except UnicodeDecodeError as exc:
        raise ArchiveFormatError("CSV is not UTF-8") from exc


def _unit_of(value: int) -> TimeUnit | None:
    if _MS_RANGE[0] <= value < _MS_RANGE[1]:
        return "ms"
    if _US_RANGE[0] <= value < _US_RANGE[1]:
        return "us"
    return None


def _ts(raw: str, unit: TimeUnit, what: str) -> int:
    if _INT.fullmatch(raw) is None:
        raise ValueError(f"{what} is not an integer")
    value = int(raw)
    if _unit_of(value) != unit:
        raise ArchiveFormatError(f"{what} {value} does not match the file's unit ({unit})")
    return value // 1000 if unit == "us" else value


def _num(raw: str, what: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{what} is not numeric") from exc
    if raw.strip() != raw or not raw or value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"{what} is not a finite number")
    return value


def _int(raw: str, what: str) -> int:
    if _INT.fullmatch(raw) is None:
        raise ValueError(f"{what} is not an integer")
    return int(raw)


def _data_rows(text: str, issues: ParseIssues) -> list[tuple[int, list[str]]]:
    rows: list[tuple[int, list[str]]] = []
    for line_no, row in enumerate(csv.reader(io.StringIO(text)), start=1):
        if not row or all(not c.strip() for c in row):
            continue
        if (
            not rows
            and not issues.header_skipped
            and ",".join(c.strip().lower() for c in row) in KNOWN_HEADERS
        ):
            issues.header_skipped = True
            continue
        rows.append((line_no, [c.strip() for c in row]))
    return rows


def _detect_unit(rows: list[tuple[int, list[str]]]) -> TimeUnit:
    for _, cells in rows:
        if cells and _INT.fullmatch(cells[0]):
            unit = _unit_of(int(cells[0]))
            if unit is None:
                raise ArchiveFormatError(f"open time {cells[0]} is neither ms nor microseconds")
            return unit
    raise ArchiveFormatError("no data row with an integer open time")


def _check_threshold(issues: ParseIssues, total: int) -> None:
    bad = len(issues.malformed_rows)
    if bad > max(MAX_MALFORMED_ABSOLUTE, int(total * MAX_MALFORMED_FRACTION)):
        sample = issues.malformed_rows[:3]
        raise ArchiveFormatError(f"{bad} of {total} rows malformed (format change?): {sample}")


def _expectation_note(market: Market, unit: TimeUnit, period_start_ms: int) -> str | None:
    if market is not Market.SPOT:
        return None if unit == "ms" else "USD-M file in microseconds (not documented)"
    expected: TimeUnit = "us" if period_start_ms >= SPOT_MICROS_FROM_MS else "ms"
    if unit != expected:
        return f"spot file in {unit}, expected {expected} for its date (detected unit used)"
    return None


def _check_period(open_ms: int, period: tuple[int, int], line_no: int) -> None:
    if not period[0] <= open_ms < period[1]:
        raise ArchiveFormatError(
            f"line {line_no}: open time {open_ms} outside the file period {period}"
        )


def parse_klines_csv(
    text: str, market: Market, period: tuple[int, int], interval_ms: int
) -> ParsedKlines:
    """Parse a klines CSV for a file covering `period` = [start_ms, end_ms)."""
    issues = ParseIssues()
    rows = _data_rows(text, issues)
    if not rows:
        raise ArchiveFormatError("file has no data rows")
    unit = _detect_unit(rows)
    issues.unit_note = _expectation_note(market, unit, period[0])
    out: list[RestKline] = []
    for line_no, c in rows:
        if len(c) < 11:
            issues.malformed_rows.append((line_no, f"{len(c)} columns"))
            continue
        try:
            open_ms = _ts(c[0], unit, "open time")
            close_ms = _ts(c[6], unit, "close time")
            k = RestKline(
                open_time_ms=open_ms,
                open=_num(c[1], "open"),
                high=_num(c[2], "high"),
                low=_num(c[3], "low"),
                close=_num(c[4], "close"),
                volume=_num(c[5], "volume"),
                close_time_ms=close_ms,
                quote_volume=_num(c[7], "quote volume"),
                trades=_int(c[8], "trades"),
                taker_buy_volume=_num(c[9], "taker buy base volume"),
                taker_buy_quote_volume=_num(c[10], "taker buy quote volume"),
            )
        except ValueError as exc:
            if isinstance(exc, ArchiveFormatError):
                raise
            issues.malformed_rows.append((line_no, str(exc)))
            continue
        _check_period(open_ms, period, line_no)
        if close_ms != open_ms + interval_ms - 1:
            issues.malformed_rows.append((line_no, "close time != open time + interval - 1"))
            continue
        out.append(k)
    _check_threshold(issues, len(rows))
    return ParsedKlines(out, unit, issues)


def parse_price_klines_csv(
    text: str, market: Market, period: tuple[int, int], interval_ms: int
) -> ParsedPriceKlines:
    """markPriceKlines: same layout; only times and prices are read (other columns 'ignore')."""
    issues = ParseIssues()
    rows = _data_rows(text, issues)
    if not rows:
        raise ArchiveFormatError("file has no data rows")
    unit = _detect_unit(rows)
    issues.unit_note = _expectation_note(market, unit, period[0])
    out: list[PriceKline] = []
    for line_no, c in rows:
        if len(c) < 7:
            issues.malformed_rows.append((line_no, f"{len(c)} columns"))
            continue
        try:
            open_ms = _ts(c[0], unit, "open time")
            close_ms = _ts(c[6], unit, "close time")
            k = PriceKline(
                open_time_ms=open_ms,
                open=_num(c[1], "open"),
                high=_num(c[2], "high"),
                low=_num(c[3], "low"),
                close=_num(c[4], "close"),
                close_time_ms=close_ms,
            )
        except ValueError as exc:
            if isinstance(exc, ArchiveFormatError):
                raise
            issues.malformed_rows.append((line_no, str(exc)))
            continue
        _check_period(open_ms, period, line_no)
        if close_ms != open_ms + interval_ms - 1:
            issues.malformed_rows.append((line_no, "close time != open time + interval - 1"))
            continue
        out.append(k)
    _check_threshold(issues, len(rows))
    return ParsedPriceKlines(out, unit, issues)
