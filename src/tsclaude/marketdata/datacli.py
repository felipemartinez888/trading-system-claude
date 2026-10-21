"""`tsclaude data ...` commands: historical import, universe snapshot, dataset verification.

    tsclaude data import   --market spot|usdm (--symbols A,B | --symbols-from FILE)
                           --from YYYY-MM-DD --to YYYY-MM-DD [--kinds bars_1m,...]
    tsclaude data universe [--snapshot 2025-01-01] [--live] [--rule configs/universe_rule.yaml]
    tsclaude data verify   <dataset_id> | --all
    tsclaude data catalog  (rebuild catalog.duckdb)

`--to` is inclusive (whole UTC day); ranges are clipped to closed bars. Every command prints a JSON
summary on stdout (and to `--summary FILE` if given). Exit code 0 = all ok, 1 = some request failed,
2 = verification failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tsclaude.contracts.market import Market
from tsclaude.contracts.timeutil import MS_PER_DAY, SystemClock, WallClock
from tsclaude.marketdata.archive.importer import (
    DEFAULT_KINDS,
    Importer,
    ImportRequest,
    ImportResult,
)
from tsclaude.marketdata.archive.paths import day_ms, ms_to_date
from tsclaude.marketdata.config import EndpointsConfig
from tsclaude.marketdata.universe import (
    RESEARCH_SNAPSHOT,
    Mode,
    SymbolData,
    UniverseRule,
    check_research_snapshot,
    lake_facts,
    live_facts,
    load_rule,
    raw_sha256,
    select_universe,
    snapshot_date_ok,
    snapshot_document,
    static_candidates,
    windows,
    write_snapshot,
)
from tsclaude.store import lake_admin
from tsclaude.store.holdout import HoldoutGuard, SealedUnlocks
from tsclaude.store.lake_reader import LakeReader
from tsclaude.store.lake_writer import LakeKind, LakeWriter, PartCodec

if TYPE_CHECKING:
    import httpx

    from tsclaude.marketdata.archive.download import ArchiveDownloader
    from tsclaude.marketdata.binance.rest import BinanceRest

log = logging.getLogger(__name__)


@dataclass
class DataEnv:
    """Everything an import needs; built by `open_env`, closed by `close`."""

    data_dir: Path
    lake: LakeWriter
    downloader: ArchiveDownloader
    codec: PartCodec | None
    rest: dict[Market, BinanceRest]
    clock: WallClock

    async def close(self) -> None:
        await self.downloader.aclose()
        for r in self.rest.values():
            await r.aclose()


async def open_env(
    data_dir: Path,
    *,
    clock: WallClock | None = None,
    codec: PartCodec | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
    archive_base_url: str | None = None,
    endpoints: EndpointsConfig | None = None,
    concurrency: int = 4,
) -> DataEnv:
    from tsclaude.marketdata.archive.download import ArchiveDownloader
    from tsclaude.marketdata.archive.paths import ARCHIVE_BASE_URL
    from tsclaude.marketdata.binance.rest import BinanceRest

    clock = clock or SystemClock()
    ep = endpoints or EndpointsConfig()
    lake = LakeWriter(data_dir, codec)
    downloader = ArchiveDownloader(
        data_dir / "archive_cache",
        base_url=archive_base_url or ARCHIVE_BASE_URL,
        transport=http_transport,
        concurrency=concurrency,
    )
    rest = {
        Market.SPOT: BinanceRest(Market.SPOT, ep.spot_rest, clock, transport=http_transport),
        Market.USDM: BinanceRest(Market.USDM, ep.usdm_rest, clock, transport=http_transport),
    }
    return DataEnv(data_dir, lake, downloader, codec, rest, clock)


def _importer(env: DataEnv, **kw: Any) -> Importer:
    return Importer(env.lake, env.downloader, dict(env.rest), env.clock, **kw)


async def run_import(
    env: DataEnv,
    market: Market,
    symbols: Sequence[str],
    start_ms: int,
    end_ms: int,
    kinds: Sequence[LakeKind] | None = None,
    *,
    cross_check_months: int = 2,
) -> dict[str, Any]:
    """Import every (symbol, kind). Failures are reported per request, never hidden."""
    kinds = list(kinds or DEFAULT_KINDS[market])
    await env.rest[market].load_exchange_info(tuple(symbols))  # seeds the REST weight limiter
    importer = _importer(env, cross_check_months=cross_check_months)
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for symbol in symbols:
        for kind in kinds:
            req = ImportRequest(market, kind, symbol, start_ms, end_ms)
            try:
                res = await importer.run(req)
            except Exception as exc:
                log.exception("import failed", extra={"symbol": symbol, "kind": str(kind)})
                failures.append(
                    {
                        "market": str(market),
                        "kind": str(kind),
                        "symbol": symbol,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            results.append(res.summary())
    catalog = lake_admin.build_catalog(env.data_dir, env.codec)
    if catalog is None:
        log.warning("duckdb not installed; catalog.duckdb not built")
    return {"ok": not failures, "imports": results, "failures": failures, "catalog": catalog}


async def run_universe(
    env: DataEnv,
    snapshot: date | None,
    rule: UniverseRule,
    configs_dir: Path,
    *,
    mode: Mode = "research",
    exchange_info: dict[Market, Any] | None = None,
    symbol_concurrency: int = 4,
    override_reason: str | None = None,
) -> dict[str, Any]:
    """Compute and write the universe snapshot.

    research (default, L-08): S defaults to 2025-01-01 and may not be after the holdout start;
    facts come from the lake through the holdout guard; today's status is not used.
    live: S defaults to today; status TRADING required; facts come from REST; the file is
    `universe_live_<date>.yaml` and labelled live_forward.
    """
    from tsclaude.marketdata.binance.parse import parse_exchange_info

    today = ms_to_date(env.clock.now_ms())
    if snapshot is None:
        snapshot = RESEARCH_SNAPSHOT if mode == "research" else today
    snapshot_date_ok(snapshot, today)
    if mode == "research":
        check_research_snapshot(snapshot, override_reason)
    elif override_reason:
        raise ValueError("--override-reason applies to research snapshots only")
    raw: dict[Market, Any] = {}
    for market in (Market.SPOT, Market.USDM):
        if exchange_info and market in exchange_info:
            raw[market] = exchange_info[market]
            info = parse_exchange_info(market, raw[market])
            env.rest[market].weights.seed(info.rate_limits)
        else:
            raw[market], _ = await env.rest[market].load_exchange_info_raw(None)
    infos = {m: parse_exchange_info(m, raw[m]) for m in raw}
    evidence_dir = env.data_dir / "imports" / "exchange_info" / f"{mode}-{snapshot.isoformat()}"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    inputs: dict[str, Any] = {"exchange_info": {}, "dataset_ids": [], "rest_daily_klines": None}
    for m, payload in raw.items():
        digest = raw_sha256(payload)
        path = evidence_dir / f"{m}-{digest[:12]}.json"
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        inputs["exchange_info"][str(m)] = {"sha256": digest, "path": path.as_posix()}

    w = windows(snapshot, rule)
    candidates, excluded = static_candidates(
        infos[Market.SPOT], infos[Market.USDM], rule, w, status_filter=(mode == "live")
    )
    sem = asyncio.Semaphore(symbol_concurrency)
    failures: list[dict[str, str]] = []
    data: dict[str, SymbolData] = {}

    if mode == "live":

        async def fetch_live(symbol: str) -> None:
            async with sem:
                try:
                    data[symbol] = await live_facts(env.rest, symbol, w)
                except Exception as exc:
                    failures.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})

        await asyncio.gather(*(fetch_live(s) for s in candidates))
        inputs["rest_daily_klines"] = {
            "endpoints": ["/api/v3/klines", "/fapi/v1/klines"],
            "interval": "1d",
            "symbols": len(candidates),
        }
    else:
        importer = _importer(env, cross_check_months=0)
        dataset_ids: set[str] = set()

        async def ensure(symbol: str) -> None:
            async with sem:
                reqs = [
                    ImportRequest(Market.SPOT, LakeKind.KLINES_1D, symbol, *w.listing_probe),
                    ImportRequest(Market.SPOT, LakeKind.KLINES_1D, symbol, *w.volume),
                    ImportRequest(Market.USDM, LakeKind.KLINES_1D, symbol, *w.volume),
                ]
                for req in reqs:
                    try:
                        res: ImportResult = await importer.run(req)
                    except Exception as exc:
                        failures.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})
                        return
                    if res.dataset_id:
                        dataset_ids.add(res.dataset_id)

        await asyncio.gather(*(ensure(s) for s in candidates))
        if not failures:
            reader = LakeReader.open(env.data_dir, HoldoutGuard(SealedUnlocks()), env.codec)
            data = {s: lake_facts(reader, s, w) for s in candidates}
        inputs["dataset_ids"] = sorted(dataset_ids)
    if failures:
        # Excluding a candidate because its data could not be fetched would bias the universe.
        return {"ok": False, "mode": mode, "failures": failures, "candidates": len(candidates)}
    ranked, excluded_late = select_universe(candidates, data, rule, w)
    doc = snapshot_document(
        snapshot,
        rule,
        w,
        ranked,
        excluded + excluded_late,
        inputs,
        env.clock.now_ms(),
        mode,
        override_reason if mode == "research" else None,
    )
    path = write_snapshot(doc, configs_dir)
    return {
        "ok": True,
        "mode": mode,
        "purpose": doc["purpose"],
        "snapshot": path.as_posix(),
        "symbols": doc["symbols"],
        "candidates": len(candidates),
        "eligible": len(ranked),
    }


# ---------------------------------------------------------------------------- argparse
def _date(text: str) -> date:
    return date.fromisoformat(text)


def _symbols(args: argparse.Namespace, market: Market) -> list[str]:
    if args.symbols:
        return [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if args.symbols_from:
        import yaml

        doc = yaml.safe_load(Path(args.symbols_from).read_text(encoding="utf-8"))
        values = doc.get(str(market)) or doc.get("symbols")
        if not values:
            raise SystemExit(f"{args.symbols_from} has no '{market}' or 'symbols' list")
        return [str(s).upper() for s in values]
    raise SystemExit("--symbols or --symbols-from is required")


def _emit(result: dict[str, Any], summary_path: str | None) -> None:
    text = json.dumps(result, indent=2, sort_keys=True, default=str)
    print(text)
    if summary_path:
        path = Path(summary_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")


def _data_dir(args: argparse.Namespace) -> Path:
    if args.data_dir:
        return Path(args.data_dir)
    from tsclaude.runtime.settings import Settings

    return Settings.from_env().data_dir


def _cmd_import(args: argparse.Namespace) -> int:
    market = Market(args.market)
    symbols = _symbols(args, market)
    start, end = day_ms(args.date_from), day_ms(args.date_to) + MS_PER_DAY
    kinds = [LakeKind(k) for k in args.kinds.split(",")] if args.kinds else None

    async def main() -> dict[str, Any]:
        env = await open_env(_data_dir(args), concurrency=args.concurrency)
        try:
            return await run_import(
                env, market, symbols, start, end, kinds, cross_check_months=args.cross_check_months
            )
        finally:
            await env.close()

    result = asyncio.run(main())
    _emit(result, args.summary)
    return 0 if result["ok"] else 1


def _cmd_universe(args: argparse.Namespace) -> int:
    rule = load_rule(args.rule)
    exchange_info: dict[Market, Any] = {}
    if args.spot_exchange_info:
        exchange_info[Market.SPOT] = json.loads(Path(args.spot_exchange_info).read_text("utf-8"))
    if args.usdm_exchange_info:
        exchange_info[Market.USDM] = json.loads(Path(args.usdm_exchange_info).read_text("utf-8"))

    async def main() -> dict[str, Any]:
        env = await open_env(_data_dir(args), concurrency=args.concurrency)
        try:
            return await run_universe(
                env,
                args.snapshot,
                rule,
                Path(args.configs_dir),
                mode="live" if args.live else "research",
                exchange_info=exchange_info or None,
                override_reason=args.override_reason,
            )
        finally:
            await env.close()

    result = asyncio.run(main())
    _emit(result, args.summary)
    return 0 if result["ok"] else 1


def _cmd_verify(args: argparse.Namespace) -> int:
    if args.all == bool(args.dataset_id):
        raise SystemExit("give a dataset_id or --all")
    result = lake_admin.verify(_data_dir(args), None if args.all else [args.dataset_id])
    _emit(result, args.summary)
    return 0 if result["ok"] else 2


def _cmd_catalog(args: argparse.Namespace) -> int:
    data_dir = _data_dir(args)
    catalog = lake_admin.build_catalog(data_dir)
    _emit({"catalog": catalog, "datasets": len(lake_admin.dataset_ids(data_dir))}, args.summary)
    return 0 if catalog else 1


def register(sub: Any) -> None:
    data = sub.add_parser("data", help="historical data: import, universe, verify")
    data_sub = data.add_subparsers(dest="data_command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--data-dir", default=None, help="default: TSC_DATA_DIR or ./data")
        p.add_argument("--summary", default=None, help="also write the JSON summary here")

    imp = data_sub.add_parser("import", help="import archive (+REST tail) data into the lake")
    common(imp)
    imp.add_argument("--market", required=True, choices=[str(m) for m in Market])
    imp.add_argument("--symbols", default=None, help="comma-separated, e.g. BTCUSDT,ETHUSDT")
    imp.add_argument("--symbols-from", default=None, help="YAML with a market or 'symbols' list")
    imp.add_argument("--from", dest="date_from", type=_date, required=True)
    imp.add_argument("--to", dest="date_to", type=_date, required=True, help="inclusive")
    imp.add_argument("--kinds", default=None, help="default: market default kinds")
    imp.add_argument("--concurrency", type=int, default=4)
    imp.add_argument("--cross-check-months", type=int, default=2)
    imp.set_defaults(func=_cmd_import)

    uni = data_sub.add_parser("universe", help="compute the rule-based universe snapshot")
    common(uni)
    uni.add_argument(
        "--snapshot",
        type=_date,
        default=None,
        help="research default 2025-01-01 (L-08; must not be after the holdout start); "
        "live default: today",
    )
    uni.add_argument(
        "--override-reason",
        default=None,
        help="research only: required (and recorded) to use a snapshot other than 2025-01-01",
    )
    uni.add_argument(
        "--live",
        action="store_true",
        help="forward/live universe from today's status and REST volumes (never for research)",
    )
    uni.add_argument("--rule", default="configs/universe_rule.yaml")
    uni.add_argument("--configs-dir", default="configs")
    uni.add_argument("--spot-exchange-info", default=None, help="use a saved payload")
    uni.add_argument("--usdm-exchange-info", default=None, help="use a saved payload")
    uni.add_argument("--concurrency", type=int, default=4)
    uni.set_defaults(func=_cmd_universe)

    ver = data_sub.add_parser("verify", help="verify a dataset's files and hashes")
    common(ver)
    ver.add_argument("dataset_id", nargs="?", default=None)
    ver.add_argument("--all", action="store_true", help="verify every dataset in the lake")
    ver.set_defaults(func=_cmd_verify)

    cat = data_sub.add_parser("catalog", help="rebuild catalog.duckdb from the manifests")
    common(cat)
    cat.set_defaults(func=_cmd_catalog)


if __name__ == "__main__":  # pragma: no cover
    sys.exit("use `tsclaude data ...`")
