"""Archive file naming and download planning for `data.binance.vision`.

Layout (binance/binance-public-data README):

    data/spot/monthly/klines/<SYMBOL>/<interval>/<SYMBOL>-<interval>-<YYYY-MM>.zip
    data/spot/daily/klines/<SYMBOL>/<interval>/<SYMBOL>-<interval>-<YYYY-MM-DD>.zip
    data/futures/um/{monthly,daily}/{klines,markPriceKlines}/<SYMBOL>/<interval>/...

Each zip has a sibling `<name>.zip.CHECKSUM` (SHA-256). Monthly files appear after the month ends
(first Monday of the next month); daily files appear the next day.

Planning rule (M2-T1 brief): monthly zips for complete months, daily zips for the current month,
REST for the tail (today, and recent days whose daily file is not published yet). The fallbacks
(monthly missing -> its days; recent day missing -> REST) are applied by the importer, which knows
which files exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum

from tsclaude.contracts.market import Market
from tsclaude.contracts.timeutil import MS_PER_DAY

ARCHIVE_BASE_URL = "https://data.binance.vision"

_MARKET_PATH = {Market.SPOT: "spot", Market.USDM: "futures/um"}


class ArchiveDataType(StrEnum):
    KLINES = "klines"
    MARK_PRICE_KLINES = "markPriceKlines"  # USD-M only


class Period(StrEnum):
    MONTHLY = "monthly"
    DAILY = "daily"


class PlanKind(StrEnum):
    MONTHLY = "monthly"
    DAILY = "daily"
    REST = "rest"


@dataclass(frozen=True, slots=True)
class ArchiveFile:
    market: Market
    data_type: ArchiveDataType
    symbol: str
    interval: str
    period: Period
    label: str  # "YYYY-MM" or "YYYY-MM-DD"

    @property
    def name(self) -> str:
        return f"{self.symbol}-{self.interval}-{self.label}.zip"

    @property
    def relpath(self) -> str:
        """Path below the archive base URL (also used as cache path)."""
        return (
            f"data/{_MARKET_PATH[self.market]}/{self.period}/{self.data_type}/"
            f"{self.symbol}/{self.interval}/{self.name}"
        )

    def url(self, base: str = ARCHIVE_BASE_URL) -> str:
        return f"{base.rstrip('/')}/{self.relpath}"

    def checksum_url(self, base: str = ARCHIVE_BASE_URL) -> str:
        return self.url(base) + ".CHECKSUM"

    @property
    def start_ms(self) -> int:
        return period_bounds(self.period, self.label)[0]

    @property
    def end_ms(self) -> int:
        return period_bounds(self.period, self.label)[1]


@dataclass(frozen=True, slots=True)
class PlanItem:
    kind: PlanKind
    start_ms: int  # inclusive; the part of the request this item covers
    end_ms: int  # exclusive
    label: str  # "YYYY-MM", "YYYY-MM-DD" or "rest"


def day_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)


def ms_to_date(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).date()


def month_start(d: date) -> date:
    return d.replace(day=1)


def next_month(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1)


def period_bounds(period: Period, label: str) -> tuple[int, int]:
    """[start, end) in epoch ms of a monthly or daily archive file."""
    if period is Period.MONTHLY:
        start = date.fromisoformat(label + "-01")
        return day_ms(start), day_ms(next_month(start))
    d = date.fromisoformat(label)
    return day_ms(d), day_ms(d) + MS_PER_DAY


def plan(start_ms: int, end_ms: int, today: date) -> list[PlanItem]:
    """Plan the sources for [start_ms, end_ms) given the current UTC date."""
    if end_ms <= start_ms:
        return []
    items: list[PlanItem] = []
    today_ms = day_ms(today)
    current_month_ms = day_ms(month_start(today))
    m = month_start(ms_to_date(start_ms))
    while day_ms(m) < end_ms:
        m_lo, m_hi = day_ms(m), day_ms(next_month(m))
        lo, hi = max(start_ms, m_lo), min(end_ms, m_hi)
        if m_hi <= current_month_ms:
            items.append(PlanItem(PlanKind.MONTHLY, lo, hi, m.strftime("%Y-%m")))
        else:
            items.extend(daily_items(lo, min(hi, today_ms)))
            if hi > today_ms:
                items.append(PlanItem(PlanKind.REST, max(lo, today_ms), hi, "rest"))
        m = next_month(m)
    return [i for i in items if i.end_ms > i.start_ms]


def daily_items(start_ms: int, end_ms: int) -> list[PlanItem]:
    items: list[PlanItem] = []
    d = ms_to_date(start_ms)
    while day_ms(d) < end_ms:
        lo, hi = max(start_ms, day_ms(d)), min(end_ms, day_ms(d) + MS_PER_DAY)
        if hi > lo:
            items.append(PlanItem(PlanKind.DAILY, lo, hi, d.isoformat()))
        d += timedelta(days=1)
    return items


def archive_file(
    market: Market, data_type: ArchiveDataType, symbol: str, interval: str, item: PlanItem
) -> ArchiveFile:
    if item.kind is PlanKind.REST:
        raise ValueError("REST plan items have no archive file")
    if data_type is ArchiveDataType.MARK_PRICE_KLINES and market is not Market.USDM:
        raise ValueError("markPriceKlines exist only for USD-M")
    period = Period.MONTHLY if item.kind is PlanKind.MONTHLY else Period.DAILY
    return ArchiveFile(market, data_type, symbol, interval, period, item.label)
