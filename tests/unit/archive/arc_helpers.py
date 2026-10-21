"""Shared helpers for archive/lake unit tests: documented-format CSV/zip generation, fakes.

CSV layout (binance-public-data README): open time, open, high, low, close, volume, close time,
quote asset volume, number of trades, taker buy base volume, taker buy quote volume, ignore.
Values are synthetic.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tsclaude.contracts.market import Market
from tsclaude.marketdata.archive.checksum import ChecksumError, parse_checksum, sha256_bytes
from tsclaude.marketdata.archive.download import ArchiveNotFound, VerifiedFile
from tsclaude.marketdata.archive.paths import ArchiveFile
from tsclaude.marketdata.types import FundingRate, PriceKline, RestKline
from tsclaude.store.lake import ColumnType, Row

MIN = 60_000
HOUR = 3_600_000
DAY = 86_400_000

HEADER = (
    "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,"
    "taker_buy_quote_volume,ignore"
)


def ms(y: int, m: int, d: int = 1, h: int = 0, mi: int = 0) -> int:
    return int(datetime(y, m, d, h, mi, tzinfo=UTC).timestamp() * 1000)


def price_at(t: int) -> float:
    """Deterministic synthetic price path (exactly representable steps)."""
    return 100.0 + ((t // MIN) % 97) * 0.25


def kline_values(t: int, step: int, volume: float = 2.0) -> list[Any]:
    o = price_at(t)
    c = price_at(t + step - MIN) + 0.125
    h = max(o, c) + 0.5
    lo = min(o, c) - 0.5
    return [t, o, h, lo, c, volume, t + step - 1, volume * 100.0, 10, volume / 2, volume * 50.0, 0]


def kline_lines(
    start: int,
    end: int,
    step: int = MIN,
    *,
    unit: str = "ms",
    skip: Sequence[int] = (),
    override: dict[int, list[Any]] | None = None,
    zero_volume: Sequence[int] = (),
) -> list[str]:
    lines = []
    factor = 1000 if unit == "us" else 1
    t = start
    while t < end:
        if t not in skip:
            vals = (override or {}).get(t) or kline_values(
                t, step, 0.0 if t in zero_volume else 2.0
            )
            vals = list(vals)
            vals[0] = vals[0] * factor
            vals[6] = vals[6] * factor + (999 if unit == "us" else 0)
            lines.append(",".join(_fmt(v) for v in vals))
        t += step
    return lines


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.8f}"
    return str(v)


def make_zip(csv_name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        info = zipfile.ZipInfo(csv_name, date_time=(2026, 1, 1, 0, 0, 0))
        zf.writestr(info, text)
    return buf.getvalue()


def checksum_text(zip_name: str, data: bytes) -> str:
    return f"{hashlib.sha256(data).hexdigest()}  {zip_name}\n"


@dataclass
class FakeArchive:
    """In-memory data.binance.vision: relpath -> (zip bytes, checksum text)."""

    files: dict[str, tuple[bytes, str]] = field(default_factory=dict)
    fetched: list[str] = field(default_factory=list)

    def add(
        self,
        f: ArchiveFile,
        lines: Sequence[str],
        *,
        header: bool = False,
        corrupt_checksum: bool = False,
    ) -> None:
        csv_name = f.name.removesuffix(".zip") + ".csv"
        text = "\n".join(([HEADER] if header else []) + list(lines)) + "\n"
        data = make_zip(csv_name, text)
        cks = checksum_text(f.name, data)
        if corrupt_checksum:
            cks = ("0" * 64) + f"  {f.name}\n"
        self.files[f.relpath] = (data, cks)

    def add_klines(self, f: ArchiveFile, step: int = MIN, **kw: Any) -> None:
        header = kw.pop("header", False)
        corrupt = kw.pop("corrupt_checksum", False)
        self.add(
            f,
            kline_lines(f.start_ms, f.end_ms, step, **kw),
            header=header,
            corrupt_checksum=corrupt,
        )

    async def fetch(self, f: ArchiveFile) -> VerifiedFile:
        """Same verification contract as ArchiveDownloader.fetch."""
        self.fetched.append(f.relpath)
        if f.relpath not in self.files:
            raise ArchiveNotFound(f.relpath)
        data, cks = self.files[f.relpath]
        expected = parse_checksum(cks, f.name)
        if sha256_bytes(data) != expected:
            raise ChecksumError(f"checksum mismatch for {f.relpath}")
        return VerifiedFile(f, data, expected, f.url(), f.checksum_url(), f.relpath, False)


@dataclass
class FakeRest:
    """In-memory REST source with the BinanceRest method signatures used by the importer."""

    klines_by_interval: dict[str, Callable[[int, int], list[RestKline]]] = field(
        default_factory=dict
    )
    funding: list[FundingRate] = field(default_factory=list)
    marks: Callable[[int, int], list[PriceKline]] | None = None
    calls: list[tuple[str, int, int]] = field(default_factory=list)

    async def klines(
        self, symbol: str, start_ms: int, end_ms: int, interval: str = "1m"
    ) -> list[RestKline]:
        self.calls.append((f"klines:{interval}", start_ms, end_ms))
        fn = self.klines_by_interval.get(interval)
        return fn(start_ms, end_ms) if fn else []

    async def mark_price_klines(
        self, symbol: str, start_ms: int, end_ms: int, interval: str = "1m"
    ) -> list[PriceKline]:
        self.calls.append(("mark", start_ms, end_ms))
        return self.marks(start_ms, end_ms) if self.marks else []

    async def funding_rates(
        self, symbol: str, start_ms: int | None = None, end_ms: int | None = None, limit: int = 1000
    ) -> list[FundingRate]:
        self.calls.append(("funding", start_ms or 0, end_ms or 0))
        rows = [
            f
            for f in self.funding
            if (start_ms is None or f.funding_time_ms >= start_ms)
            and (end_ms is None or f.funding_time_ms <= end_ms)
        ]
        return rows[:limit]


def rest_klines(start: int, end: int, step: int = MIN) -> list[RestKline]:
    out = []
    t = start - start % step + (step if start % step else 0)
    while t < end:
        v = kline_values(t, step)
        out.append(RestKline(t, v[1], v[2], v[3], v[4], v[5], v[6], v[7], v[8], v[9], v[10]))
        t += step
    return out


class JsonLinesCodec:
    """Test-only lake codec (no pyarrow): one JSON object per line; floats round-trip exactly."""

    extension = "jsonl"

    def write(
        self, path: Path, schema: Sequence[tuple[str, ColumnType]], rows: Sequence[Row]
    ) -> None:
        with path.open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps({n: r[n] for n, _ in schema}) + "\n")

    def read(self, path: Path, schema: Sequence[tuple[str, ColumnType]]) -> list[Row]:
        names = [n for n, _ in schema]
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            out.append({n: d[n] for n in names})
        return out


MARKETS = (Market.SPOT, Market.USDM)
