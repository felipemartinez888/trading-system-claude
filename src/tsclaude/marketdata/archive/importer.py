"""Import one (market, kind, symbol, range) from the Binance archive (+ REST) into the lake.

Flow per request, one UTC month at a time (bounded memory):

1. plan sources (`paths.plan`): monthly zips for complete months, daily zips for the current month,
   REST for today; a missing monthly zip falls back to its daily zips; a missing recent daily zip
   (publication lag, `rest_fallback_days`) falls back to REST; older missing files are recorded
   as unavailable (pre-listing, delisted, or not published) and their minutes show up in QA;
2. fetch and **verify** each zip (`download`), parse it (`csvparse`, per-file ms/µs detection);
3. merge rows, count identical duplicates, record differing duplicates (first source wins);
4. validate every row (the `Bar` contract for 1m klines, equivalent checks for daily klines,
   mark prices and funding); rejected rows are reported, never repaired;
5. write the month's immutable lake part;
6. at the end: QA report (gaps, duplicates, violations, zero-volume runs, cross-check of derived
   15m/1h bars against native archive klines for sample months) and the dataset manifest.

Any `ChecksumError` (or a kline format error) aborts the whole request: parts written by this run
are removed again and no manifest is committed, so nothing from it becomes visible.
"""

from __future__ import annotations

import asyncio
import logging
from array import array
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any, Protocol

from tsclaude import __version__
from tsclaude.contracts.market import Bar, BarSource, Interval, Market
from tsclaude.contracts.timeutil import MS_PER_DAY, MS_PER_HOUR, MS_PER_MINUTE, WallClock, to_iso
from tsclaude.marketdata.archive import csvparse, paths
from tsclaude.marketdata.archive.checksum import ChecksumError
from tsclaude.marketdata.archive.csvparse import ArchiveFormatError
from tsclaude.marketdata.archive.download import ArchiveNotFound, VerifiedFile
from tsclaude.marketdata.archive.paths import ArchiveDataType, ArchiveFile, PlanItem, PlanKind
from tsclaude.marketdata.archive.qa import (
    QACollector,
    build_report,
    cross_check,
    merge_runs,
    zero_volume_runs,
)
from tsclaude.marketdata.types import FundingRate, PriceKline, RestKline
from tsclaude.store.lake_writer import LakeKind, LakeWriter, PartInfo, Row, Staging, month_is_sealed

log = logging.getLogger(__name__)

FUNDING_PAGE = 1000

_KIND_SPEC: dict[LakeKind, tuple[ArchiveDataType | None, str, int]] = {
    LakeKind.BARS_1M: (ArchiveDataType.KLINES, "1m", MS_PER_MINUTE),
    LakeKind.KLINES_1D: (ArchiveDataType.KLINES, "1d", MS_PER_DAY),
    LakeKind.MARK_PRICE_1M: (ArchiveDataType.MARK_PRICE_KLINES, "1m", MS_PER_MINUTE),
    LakeKind.FUNDING: (None, "", 0),
}

DEFAULT_KINDS: dict[Market, tuple[LakeKind, ...]] = {
    Market.SPOT: (LakeKind.BARS_1M,),
    Market.USDM: (LakeKind.BARS_1M, LakeKind.MARK_PRICE_1M, LakeKind.FUNDING),
}


class Fetcher(Protocol):
    async def fetch(self, f: ArchiveFile) -> VerifiedFile: ...


class RestSource(Protocol):
    async def klines(
        self, symbol: str, start_ms: int, end_ms: int, interval: str = "1m"
    ) -> list[RestKline]: ...

    async def mark_price_klines(
        self, symbol: str, start_ms: int, end_ms: int, interval: str = "1m"
    ) -> list[PriceKline]: ...

    async def funding_rates(
        self,
        symbol: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = FUNDING_PAGE,
    ) -> list[FundingRate]: ...


class ArchiveImportError(RuntimeError):
    """An import request failed; nothing from it was committed."""


@dataclass(frozen=True, slots=True)
class ImportRequest:
    market: Market
    kind: LakeKind
    symbol: str
    start_ms: int  # inclusive
    end_ms: int  # exclusive

    def __post_init__(self) -> None:
        if (
            self.kind in (LakeKind.MARK_PRICE_1M, LakeKind.FUNDING)
            and self.market is not Market.USDM
        ):
            raise ValueError(f"{self.kind} exists only for USD-M")
        if self.end_ms <= self.start_ms:
            raise ValueError("empty import range")


@dataclass
class ImportResult:
    request: ImportRequest
    dataset_id: str | None
    created: bool
    rows: int
    qa: dict[str, Any]
    sources: list[dict[str, Any]]
    parts: list[PartInfo] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        r = self.request
        return {
            "market": str(r.market),
            "kind": str(r.kind),
            "symbol": r.symbol,
            "from": to_iso(r.start_ms),
            "to": to_iso(r.end_ms),
            "dataset_id": self.dataset_id,
            "created": self.created,
            "rows": self.rows,
            "sources": len(self.sources),
            "qa": self.qa.get("summary", {}),
        }


@dataclass
class _Chunk:
    """Rows obtained for one plan item."""

    rows: list[Any]
    source: BarSource
    record: dict[str, Any]


class Importer:
    def __init__(
        self,
        lake: LakeWriter,
        fetcher: Fetcher,
        rest: dict[Market, RestSource] | None,
        clock: WallClock,
        *,
        rest_fallback_days: int = 3,
        prefer_rest_current_month: bool = False,
        cross_check_months: int = 2,
        month_concurrency: int = 3,
    ) -> None:
        self._lake = lake
        self._fetcher = fetcher
        self._rest = rest or {}
        self._clock = clock
        self._rest_fallback_days = rest_fallback_days
        self._prefer_rest_current_month = prefer_rest_current_month
        self._cross_check_months = cross_check_months
        self._month_sem = asyncio.Semaphore(month_concurrency)

    # ------------------------------------------------------------------ entry point
    async def run(self, req: ImportRequest) -> ImportResult:
        """Import one request. Parts are staged; nothing is visible unless the whole request
        succeeds (the staging directory of a failed request is removed)."""
        staging = self._lake.begin()
        try:
            if req.kind is LakeKind.FUNDING:
                return await self._run_funding(req, staging)
            return await self._run_klines(req, staging)
        finally:
            staging.abort()  # no-op after a successful commit

    # ------------------------------------------------------------------ kline kinds
    def _plan(self, req: ImportRequest) -> list[PlanItem]:
        today = paths.ms_to_date(self._clock.now_ms())
        items = paths.plan(req.start_ms, req.end_ms, today)
        if req.kind is LakeKind.KLINES_1D or self._prefer_rest_current_month:
            # Daily-kline imports (or an explicit preference) use one REST range for the current
            # month instead of one zip per day.
            month_lo = paths.day_ms(paths.month_start(today))
            daily = [i for i in items if i.kind is PlanKind.DAILY and i.start_ms >= month_lo]
            if daily:
                rest_items = [i for i in items if i.kind is PlanKind.REST]
                lo = min(i.start_ms for i in daily + rest_items)
                hi = max(i.end_ms for i in daily + rest_items)
                items = [i for i in items if i not in daily and i not in rest_items]
                items.append(PlanItem(PlanKind.REST, lo, hi, "rest"))
        return items

    async def _run_klines(self, req: ImportRequest, staging: Staging) -> ImportResult:
        data_type, interval, step = _KIND_SPEC[req.kind]
        if data_type is None:  # pragma: no cover - funding handled separately
            raise ValueError(req.kind)
        collector = QACollector()
        sources: list[dict[str, Any]] = []
        months: dict[str, list[PlanItem]] = {}
        for item in self._plan(req):
            months.setdefault(paths.ms_to_date(item.start_ms).strftime("%Y-%m"), []).append(item)

        async def one_month(label: str) -> tuple[str, list[Any], list[dict[str, Any]]]:
            async with self._month_sem:
                chunks: list[_Chunk] = []
                for item in months[label]:
                    chunks.extend(
                        await self._resolve(req, data_type, interval, step, item, collector)
                    )
                rows = self._merge(chunks, req, collector)
                return label, rows, [c.record for c in chunks]

        results = await asyncio.gather(*(one_month(m) for m in sorted(months)))
        parts: list[PartInfo] = []
        times = array("q")
        zero_runs: list[tuple[int, int]] = []
        bars_by_month: dict[str, list[Bar]] = {}
        sample = self._sample_months(req) if req.kind is LakeKind.BARS_1M else set()
        ingested = self._clock.now_ms()
        for label, raw_rows, records in sorted(results, key=lambda r: r[0]):
            sources.extend(records)
            valid = self._validate(req, raw_rows, step, ingested, collector)
            if not valid:
                continue
            times.extend(int(r["open_time_ms"]) for r in valid)
            if req.kind is LakeKind.BARS_1M:
                bars = [_bar_from_row(req.market, req.symbol, r) for r in valid]
                zero_runs.extend(zero_volume_runs(bars, min_run=1))
                if label in sample:
                    bars_by_month[label] = bars
            parts.extend(staging.write_parts(req.market, req.kind, req.symbol, valid))
        checks: list[dict[str, Any]] = []
        for label in sorted(bars_by_month):
            checks.extend(await self._cross_checks(req, label, bars_by_month[label], sources))
        runs = [r for r in merge_runs(zero_runs) if (r[1] - r[0]) // MS_PER_MINUTE >= 3]
        qa = build_report(
            bars_times=list(times),
            start_ms=req.start_ms,
            end_ms=self._effective_end(req, step),
            step_ms=step,
            collector=collector,
            zero_runs=runs if req.kind is LakeKind.BARS_1M else (),
            cross_checks=checks,
        )
        return self._commit(req, interval, staging, parts, sources, qa)

    def _effective_end(self, req: ImportRequest, step: int) -> int:
        """Requests are clipped to closed periods: nothing after the last closed bar is expected."""
        now = self._clock.now_ms()
        return min(req.end_ms, now - now % step)

    async def _resolve(
        self,
        req: ImportRequest,
        data_type: ArchiveDataType,
        interval: str,
        step: int,
        item: PlanItem,
        collector: QACollector,
    ) -> list[_Chunk]:
        if item.kind is PlanKind.REST:
            return [await self._rest_chunk(req, interval, step, item)]
        f = paths.archive_file(req.market, data_type, req.symbol, interval, item)
        try:
            vf = await self._fetcher.fetch(f)
        except ArchiveNotFound:
            if item.kind is PlanKind.MONTHLY:
                log.info("monthly archive missing; trying daily files", extra={"file": f.relpath})
                out: list[_Chunk] = []
                for d in paths.daily_items(item.start_ms, item.end_ms):
                    out.extend(await self._resolve(req, data_type, interval, step, d, collector))
                return out
            today_ms = paths.day_ms(paths.ms_to_date(self._clock.now_ms()))
            if item.start_ms >= today_ms - self._rest_fallback_days * MS_PER_DAY:
                return [await self._rest_chunk(req, interval, step, item)]
            collector.unavailable.append(
                {"file": f.relpath, "from": to_iso(item.start_ms), "to": to_iso(item.end_ms)}
            )
            return []
        try:
            return [self._parse_file(req, vf, item, step, collector)]
        except ArchiveFormatError as exc:
            if req.kind is LakeKind.MARK_PRICE_1M:
                # markPriceKlines archive format is not documented; fall back to REST.
                collector.unit_notes.append(
                    f"{f.relpath}: archive format rejected ({exc}); REST used"
                )
                return [await self._rest_chunk(req, interval, step, item)]
            raise ArchiveImportError(f"{f.relpath}: {exc}") from exc

    def _parse_file(
        self,
        req: ImportRequest,
        vf: VerifiedFile,
        item: PlanItem,
        step: int,
        collector: QACollector,
    ) -> _Chunk:
        text = csvparse.read_single_csv(vf.data, vf.file.name.removesuffix(".zip") + ".csv")
        period = (vf.file.start_ms, vf.file.end_ms)
        rows: list[Any]
        if req.kind is LakeKind.MARK_PRICE_1M:
            pk = csvparse.parse_price_klines_csv(text, req.market, period, step)
            rows, unit, issues = list(pk.rows), pk.unit, pk.issues
        else:
            kl = csvparse.parse_klines_csv(text, req.market, period, step)
            rows, unit, issues = list(kl.rows), kl.unit, kl.issues
        for line_no, reason in issues.malformed_rows:
            collector.malformed.append({"file": vf.file.relpath, "line": line_no, "reason": reason})
        if issues.unit_note:
            collector.unit_notes.append(f"{vf.file.relpath}: {issues.unit_note}")
        record = {
            "role": "data",
            "type": f"archive_{vf.file.period}",
            "url": vf.url,
            "checksum_url": vf.checksum_url,
            "sha256": vf.sha256,
            "cache_path": vf.cache_path,
            "verified": True,
            "timestamp_unit": unit,
            "header_row": issues.header_skipped,
            "rows": len(rows),
            "from": to_iso(item.start_ms),
            "to": to_iso(item.end_ms),
        }
        return _Chunk(rows, BarSource.ARCHIVE, record)

    async def _rest_chunk(
        self, req: ImportRequest, interval: str, step: int, item: PlanItem
    ) -> _Chunk:
        rest = self._rest.get(req.market)
        now = self._clock.now_ms()
        hi = min(item.end_ms, now - now % step)
        record: dict[str, Any] = {
            "role": "data",
            "type": "rest",
            "endpoint": _endpoint(req.market, req.kind),
            "interval": interval,
            "from": to_iso(item.start_ms),
            "to": to_iso(max(item.start_ms, hi)),
            "rows": 0,
        }
        if hi <= item.start_ms:
            return _Chunk([], BarSource.REST_BACKFILL, record)
        if rest is None:
            raise ArchiveImportError(
                f"REST needed for {record['from']}..{record['to']} but not configured"
            )
        rows: list[Any]
        if req.kind is LakeKind.MARK_PRICE_1M:
            rows = list(await rest.mark_price_klines(req.symbol, item.start_ms, hi, interval))
        else:
            rows = list(await rest.klines(req.symbol, item.start_ms, hi, interval))
        rows = [r for r in rows if r.close_time_ms < now]
        record["rows"] = len(rows)
        return _Chunk(rows, BarSource.REST_BACKFILL, record)

    def _merge(
        self, chunks: Sequence[_Chunk], req: ImportRequest, collector: QACollector
    ) -> list[tuple[Any, BarSource]]:
        merged: dict[int, tuple[Any, BarSource]] = {}
        for chunk in chunks:
            for row in chunk.rows:
                t = row.open_time_ms
                if not req.start_ms <= t < req.end_ms:
                    continue
                current = merged.get(t)
                if current is None:
                    merged[t] = (row, chunk.source)
                elif current[0] == row:
                    collector.duplicates_identical += 1
                else:
                    collector.duplicate_conflicts.append(
                        {
                            "open_time_ms": t,
                            "time": to_iso(t),
                            "kept": _asdict(current[0]),
                            "dropped": _asdict(row),
                        }
                    )
        return [merged[t] for t in sorted(merged)]

    def _validate(
        self,
        req: ImportRequest,
        rows: Sequence[tuple[Any, BarSource]],
        step: int,
        ingested: int,
        collector: QACollector,
    ) -> list[Row]:
        out: list[Row] = []
        for row, source in rows:
            try:
                if req.kind is LakeKind.BARS_1M:
                    out.append(_bar_row(req.market, req.symbol, row, source, ingested))
                elif req.kind is LakeKind.KLINES_1D:
                    out.append(_daily_row(row, source, ingested))
                else:
                    out.append(_mark_row(row, source, ingested, step))
            except ValueError as exc:
                collector.rejected.append(
                    {
                        "open_time_ms": row.open_time_ms,
                        "time": to_iso(row.open_time_ms),
                        "reason": _reason(exc),
                        "row": _asdict(row),
                    }
                )
        return out

    def _sample_months(self, req: ImportRequest) -> set[str]:
        """First and last complete months fully inside the request (for the cross-check)."""
        if self._cross_check_months <= 0:
            return set()
        today = paths.ms_to_date(self._clock.now_ms())
        current = paths.day_ms(paths.month_start(today))
        labels = []
        m = paths.month_start(paths.ms_to_date(req.start_ms))
        while paths.day_ms(m) < req.end_ms:
            lo, hi = paths.day_ms(m), paths.day_ms(paths.next_month(m))
            sealed = month_is_sealed(m.year, m.month)
            if lo >= req.start_ms and hi <= req.end_ms and hi <= current and not sealed:
                labels.append(m.strftime("%Y-%m"))
            m = paths.next_month(m)
        if len(labels) <= self._cross_check_months:
            return set(labels)
        half = self._cross_check_months // 2
        return set(labels[: self._cross_check_months - half] + labels[len(labels) - half :])

    async def _cross_checks(
        self, req: ImportRequest, label: str, bars: list[Bar], sources: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        out = []
        for interval, step in ((Interval.M15, 15 * MS_PER_MINUTE), (Interval.H1, MS_PER_HOUR)):
            item = PlanItem(PlanKind.MONTHLY, 0, 0, label)
            f = paths.archive_file(
                req.market, ArchiveDataType.KLINES, req.symbol, str(interval), item
            )
            try:
                vf = await self._fetcher.fetch(f)
            except ArchiveNotFound:
                out.append(
                    {
                        "interval": str(interval),
                        "month": label,
                        "ok": None,
                        "note": f"native file not available: {f.relpath}",
                    }
                )
                continue
            text = csvparse.read_single_csv(vf.data, vf.file.name.removesuffix(".zip") + ".csv")
            native = csvparse.parse_klines_csv(text, req.market, (f.start_ms, f.end_ms), step)
            sources.append(
                {
                    "role": "qa_reference",
                    "type": "archive_monthly",
                    "url": vf.url,
                    "checksum_url": vf.checksum_url,
                    "sha256": vf.sha256,
                    "cache_path": vf.cache_path,
                    "verified": True,
                    "timestamp_unit": native.unit,
                    "rows": len(native.rows),
                }
            )
            result = cross_check(bars, native.rows, interval)
            result["month"] = label
            out.append(result)
        return out

    # ------------------------------------------------------------------ funding
    async def _run_funding(self, req: ImportRequest, staging: Staging) -> ImportResult:
        rest = self._rest.get(Market.USDM)
        if rest is None:
            raise ArchiveImportError("funding history needs the USD-M REST client")
        collector = QACollector()
        now = self._clock.now_ms()
        end = min(req.end_ms, now)
        cursor = req.start_ms
        fetched: dict[int, FundingRate] = {}
        pages = 0
        while cursor < end:
            page = await rest.funding_rates(req.symbol, cursor, end - 1, FUNDING_PAGE)
            pages += 1
            page = [r for r in page if req.start_ms <= r.funding_time_ms < end]
            if not page:
                break
            for r in page:
                current = fetched.get(r.funding_time_ms)
                if current is None:
                    fetched[r.funding_time_ms] = r
                elif current == r:
                    collector.duplicates_identical += 1
                else:
                    collector.duplicate_conflicts.append({"funding_time_ms": r.funding_time_ms})
            nxt = max(r.funding_time_ms for r in page) + 1
            if nxt <= cursor or len(page) < FUNDING_PAGE:
                break
            cursor = nxt
        ingested = self._clock.now_ms()
        rows: list[Row] = []
        for t in sorted(fetched):
            r = fetched[t]
            if r.symbol != req.symbol:
                collector.rejected.append({"funding_time_ms": t, "reason": "symbol mismatch"})
                continue
            rows.append(
                {
                    "funding_time_ms": t,
                    "funding_rate": r.funding_rate,
                    "mark_price": r.mark_price,
                    "source": str(BarSource.REST_BACKFILL),
                    "ingested_at_ms": ingested,
                }
            )
        parts = staging.write_parts(req.market, req.kind, req.symbol, rows) if rows else []
        times = [int(r["funding_time_ms"]) for r in rows]
        intervals = sorted({(b - a) // MS_PER_HOUR for a, b in pairwise(times)})
        sources = [
            {
                "role": "data",
                "type": "rest",
                "endpoint": "/fapi/v1/fundingRate",
                "pages": pages,
                "rows": len(rows),
                "from": to_iso(req.start_ms),
                "to": to_iso(end),
            }
        ]
        qa = build_report(
            bars_times=[],
            start_ms=0,
            end_ms=0,
            step_ms=1,
            collector=collector,
            extra={
                "funding": {
                    "events": len(rows),
                    "interval_hours_seen": intervals,
                    "note": "funding intervals vary by symbol and over time; "
                    "no minute-level gap analysis",
                }
            },
        )
        qa["summary"]["rows"] = len(rows)
        return self._commit(req, "", staging, parts, sources, qa)

    # ------------------------------------------------------------------ commit
    def _commit(
        self,
        req: ImportRequest,
        interval: str,
        staging: Staging,
        parts: list[PartInfo],
        sources: list[dict[str, Any]],
        qa: dict[str, Any],
    ) -> ImportResult:
        rows = sum(p.rows for p in parts)
        if not parts:
            log.warning(
                "import produced no rows; no dataset committed",
                extra={"market": str(req.market), "kind": str(req.kind), "symbol": req.symbol},
            )
            return ImportResult(req, None, False, 0, qa, sources)
        now = self._clock.now_ms()
        meta = {
            "market": str(req.market),
            "kind": str(req.kind),
            "symbol": req.symbol,
            "interval": interval or None,
            "request": {
                "from_ms": req.start_ms,
                "to_ms": req.end_ms,
                "from": to_iso(req.start_ms),
                "to": to_iso(req.end_ms),
            },
            "created_at_ms": now,
            "created_at": to_iso(now),
            "tool": f"tsclaude {__version__}",
            "sources": sources,
            "gaps": qa.get("gaps", []),
        }
        committed = staging.commit(meta, qa)
        if committed is None:  # pragma: no cover - parts were staged above
            return ImportResult(req, None, False, 0, qa, sources)
        dsid, created = committed
        return ImportResult(req, dsid, created, rows, qa, sources, parts)


# ---------------------------------------------------------------------- row conversion
def _endpoint(market: Market, kind: LakeKind) -> str:
    if kind is LakeKind.MARK_PRICE_1M:
        return "/fapi/v1/markPriceKlines"
    return "/api/v3/klines" if market is Market.SPOT else "/fapi/v1/klines"


def _asdict(row: Any) -> dict[str, Any]:
    return {f: getattr(row, f) for f in row.__slots__}


def _reason(exc: ValueError) -> str:
    """Short reason; pydantic validation errors report their first message."""
    from pydantic import ValidationError

    if isinstance(exc, ValidationError):
        errors = exc.errors()
        if errors:
            return str(errors[0].get("msg", exc))[:200]
    return str(exc).strip().splitlines()[0][:200] if str(exc).strip() else repr(exc)


def _bar_row(market: Market, symbol: str, k: RestKline, source: BarSource, ingested: int) -> Row:
    bar = Bar(
        market=market,
        symbol=symbol,
        interval=Interval.M1,
        open_time_ms=k.open_time_ms,
        close_time_ms=k.close_time_ms,
        open=k.open,
        high=k.high,
        low=k.low,
        close=k.close,
        volume=k.volume,
        quote_volume=k.quote_volume,
        trades=k.trades,
        taker_buy_volume=k.taker_buy_volume,
        taker_buy_quote_volume=k.taker_buy_quote_volume,
        source=source,
        ingested_at_ms=ingested,
    )
    return {
        "open_time_ms": bar.open_time_ms,
        "close_time_ms": bar.close_time_ms,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "quote_volume": bar.quote_volume,
        "trades": bar.trades,
        "taker_buy_volume": bar.taker_buy_volume,
        "taker_buy_quote_volume": bar.taker_buy_quote_volume,
        "source": str(bar.source),
        "ingested_at_ms": bar.ingested_at_ms,
    }


def _bar_from_row(market: Market, symbol: str, r: Row) -> Bar:
    return Bar(
        market=market,
        symbol=symbol,
        interval=Interval.M1,
        open_time_ms=r["open_time_ms"],
        close_time_ms=r["close_time_ms"],
        open=r["open"],
        high=r["high"],
        low=r["low"],
        close=r["close"],
        volume=r["volume"],
        quote_volume=r["quote_volume"],
        trades=r["trades"],
        taker_buy_volume=r["taker_buy_volume"],
        taker_buy_quote_volume=r["taker_buy_quote_volume"],
        source=BarSource(r["source"]),
        ingested_at_ms=r["ingested_at_ms"],
    )


def _check_prices(o: float, h: float, lo: float, c: float) -> None:
    if any(not p > 0 for p in (o, h, lo, c)):
        raise ValueError("prices must be positive")
    if h < max(o, c, lo):
        raise ValueError("high below open/close/low")
    if lo > min(o, c, h):
        raise ValueError("low above open/close/high")


def _daily_row(k: RestKline, source: BarSource, ingested: int) -> Row:
    """The `Bar` contract has no 1d interval; the same rules are applied here."""
    if k.open_time_ms % MS_PER_DAY:
        raise ValueError("daily open time not aligned to a UTC day")
    if k.close_time_ms != k.open_time_ms + MS_PER_DAY - 1:
        raise ValueError("daily close time != open + 1 day - 1 ms")
    _check_prices(k.open, k.high, k.low, k.close)
    if min(k.volume, k.quote_volume, k.taker_buy_volume, k.taker_buy_quote_volume, k.trades) < 0:
        raise ValueError("negative volume or trades")
    if k.taker_buy_volume > k.volume * (1 + 1e-9):
        raise ValueError("taker buy volume exceeds volume")
    return {
        "open_time_ms": k.open_time_ms,
        "close_time_ms": k.close_time_ms,
        "open": k.open,
        "high": k.high,
        "low": k.low,
        "close": k.close,
        "volume": k.volume,
        "quote_volume": k.quote_volume,
        "trades": k.trades,
        "taker_buy_volume": k.taker_buy_volume,
        "taker_buy_quote_volume": k.taker_buy_quote_volume,
        "source": str(source),
        "ingested_at_ms": ingested,
    }


def _mark_row(k: PriceKline, source: BarSource, ingested: int, step: int) -> Row:
    if k.open_time_ms % step:
        raise ValueError("open time not aligned")
    if k.close_time_ms != k.open_time_ms + step - 1:
        raise ValueError("close time != open + interval - 1")
    _check_prices(k.open, k.high, k.low, k.close)
    return {
        "open_time_ms": k.open_time_ms,
        "close_time_ms": k.close_time_ms,
        "open": k.open,
        "high": k.high,
        "low": k.low,
        "close": k.close,
        "source": str(source),
        "ingested_at_ms": ingested,
    }


__all__ = [
    "DEFAULT_KINDS",
    "ArchiveImportError",
    "ChecksumError",
    "ImportRequest",
    "ImportResult",
    "Importer",
]
