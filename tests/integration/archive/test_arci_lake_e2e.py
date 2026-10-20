"""End to end via `datacli` + MockTransport: import, Parquet lake, catalog, verify, universe."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from arci_helpers import (
    DAY,
    FakeArchive,
    JsonLinesCodec,
    exchange_info,
    make_handler,
    ms,
    read_json,
    require,
)

httpx = require("httpx")

from tsclaude.contracts.market import Market  # noqa: E402
from tsclaude.contracts.timeutil import ManualClock  # noqa: E402
from tsclaude.marketdata.archive.paths import (  # noqa: E402
    ArchiveDataType,
    ArchiveFile,
    Period,
)
from tsclaude.marketdata.datacli import open_env, run_import, run_universe  # noqa: E402
from tsclaude.marketdata.universe import UniverseError, UniverseRule  # noqa: E402
from tsclaude.store.holdout import HoldoutGuard, SealedUnlocks  # noqa: E402
from tsclaude.store.lake import Lake, LakeKind  # noqa: E402
from tsclaude.store.lake_reader import LakeReader  # noqa: E402

NOW = ms(2026, 10, 20, 12, 30)
SPOT_SYMBOLS = [
    {"symbol": s, "status": "TRADING", "baseAsset": s.removesuffix("USDT"), "quoteAsset": "USDT"}
    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT")
]
USDM_SYMBOLS = [
    {**s, "contractType": "PERPETUAL", "onboardDate": ms(2020, 1, 1), "pair": s["symbol"]}
    for s in SPOT_SYMBOLS
]


def _codec(name: str) -> Any:
    if name == "parquet":
        require("pyarrow")
        from tsclaude.store.parquet_codec import ParquetCodec

        return ParquetCodec()
    return JsonLinesCodec()


def _klines(
    archive: FakeArchive,
    market: Market,
    symbol: str,
    label: str,
    interval: str = "1m",
    step: int = 60_000,
) -> None:
    period = Period.MONTHLY if len(label) == 7 else Period.DAILY
    archive.add_klines(
        ArchiveFile(market, ArchiveDataType.KLINES, symbol, interval, period, label),
        step=step,
        unit="us" if market is Market.SPOT and label >= "2025" else "ms",
    )


@pytest.mark.parametrize("codec_name", ["jsonl", "parquet"])
def test_import_verify_catalog(tmp_path: Path, codec_name: str) -> None:
    codec = _codec(codec_name)
    archive = FakeArchive()
    for label in ("2024-12", "2025-01"):
        _klines(archive, Market.SPOT, "BTCUSDT", label)
        _klines(archive, Market.SPOT, "BTCUSDT", label, "15m", 15 * 60_000)
        _klines(archive, Market.SPOT, "BTCUSDT", label, "1h", 3_600_000)
    _klines(archive, Market.SPOT, "BTCUSDT", "2026-01")  # sealed holdout month
    handler = make_handler(
        archive, exchange_info("spot", SPOT_SYMBOLS), exchange_info("usdm", USDM_SYMBOLS), NOW
    )

    async def main() -> dict[str, Any]:
        env = await open_env(
            tmp_path / "data",
            clock=ManualClock(NOW),
            codec=codec,
            http_transport=httpx.MockTransport(handler),
        )
        try:
            first = await run_import(env, Market.SPOT, ["BTCUSDT"], ms(2024, 12), ms(2025, 2))
            sealed = await run_import(env, Market.SPOT, ["BTCUSDT"], ms(2026, 1), ms(2026, 2))
            assert sealed["ok"], sealed["failures"]
            return {**first, "catalog": sealed["catalog"]}
        finally:
            await env.close()

    result = asyncio.run(main())
    assert result["ok"], result["failures"]
    (imp,) = result["imports"]
    assert imp["rows"] == 62 * 1440
    assert imp["qa"]["missing_interior"] == 0
    lake = Lake(tmp_path / "data", codec)
    dsid = imp["dataset_id"]
    report = lake.verify(dsid, raw_cache_dir=tmp_path / "data" / "archive_cache")
    assert report["ok"], report["errors"]
    assert report["sources_checked"] == 6  # 2 data files + 4 QA reference files
    manifest = read_json(tmp_path / "data" / "imports" / dsid / "manifest.json")
    assert [s["timestamp_unit"] for s in manifest["sources"] if s["role"] == "data"] == ["ms", "us"]
    reader = LakeReader(lake, HoldoutGuard(SealedUnlocks()))
    bars = reader.read_bars(
        Market.SPOT, "BTCUSDT", ms(2024, 12, 31, 23, 58), ms(2025, 1, 1, 0, 2), purpose="research"
    ).items
    assert [b.open_time_ms for b in bars] == [
        ms(2024, 12, 31, 23, 58) + i * 60_000 for i in range(4)
    ]
    if codec_name == "parquet":
        duckdb = require("duckdb")
        assert result["catalog"] is not None
        con = duckdb.connect(result["catalog"], read_only=True)
        try:
            assert con.execute("SELECT count(*) FROM bars_1m").fetchone() == (62 * 1440,)
            assert con.execute("SELECT count(*) FROM datasets").fetchone() == (2,)
            # the sealed holdout month is never referenced by the catalog (L-07 b)
            assert con.execute("SELECT count(*) FROM parts WHERE year = 2026").fetchone() == (0,)
            assert con.execute(
                "SELECT count(*) FROM bars_1m WHERE filename LIKE '%lake_sealed%'"
            ).fetchone() == (0,)
            months = con.execute("SELECT DISTINCT month FROM bars_1m ORDER BY 1").fetchall()
            assert [m[0] for m in months] == [1, 12]
        finally:
            con.close()


def test_parquet_and_jsonl_give_the_same_dataset_id(tmp_path: Path) -> None:
    require("pyarrow")
    from tsclaude.store.parquet_codec import ParquetCodec

    rows = [
        {
            "funding_time_ms": ms(2025, 1) + i * 8 * 3_600_000,
            "funding_rate": 0.0001 * i,
            "mark_price": None if i == 1 else 100.5,
            "source": "rest_backfill",
            "ingested_at_ms": i,
        }
        for i in range(3)
    ]
    ids = []
    for name, codec in (("pq", ParquetCodec()), ("js", JsonLinesCodec())):
        lake = Lake(tmp_path / name, codec)
        parts = lake.write_parts(Market.USDM, LakeKind.FUNDING, "BTCUSDT", rows)
        ids.append(lake.commit_dataset(parts, {}, None)[0])
        back = lake.read_part(parts[0])
        assert back[1]["mark_price"] is None
        assert back[2]["funding_rate"] == rows[2]["funding_rate"]
        assert lake.verify(ids[-1])["ok"]
    assert ids[0] == ids[1]  # content addressing is independent of the file encoder


def test_research_universe_frozen_at_2025(tmp_path: Path) -> None:
    pytest.importorskip("yaml")
    archive = FakeArchive()
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        for market in (Market.SPOT, Market.USDM):
            for label in ("2024-10", "2024-11", "2024-12"):
                _klines(archive, market, symbol, label, "1d", DAY)
        if symbol != "SOLUSDT":  # SOL spot has no daily klines before the 24-month cutoff
            _klines(archive, Market.SPOT, symbol, "2022-12", "1d", DAY)
    # today's status of ETH is BREAK: research mode must not use it (L-08)
    spot = [dict(s, status="BREAK") if s["symbol"] == "ETHUSDT" else s for s in SPOT_SYMBOLS]
    log: list[str] = []
    handler = make_handler(
        archive, exchange_info("spot", spot), exchange_info("usdm", USDM_SYMBOLS), NOW, log
    )
    rule = UniverseRule(top_n=1, min_volume_days=85)

    async def main(snapshot: date | None) -> dict[str, Any]:
        env = await open_env(
            tmp_path / "data",
            clock=ManualClock(NOW),
            codec=JsonLinesCodec(),
            http_transport=httpx.MockTransport(handler),
        )
        try:
            return await run_universe(env, snapshot, rule, tmp_path / "configs")
        finally:
            await env.close()

    result = asyncio.run(main(None))
    assert result["ok"], result
    assert result["purpose"] == "research"
    assert result["candidates"] == 3
    assert result["eligible"] == 2  # BTC and ETH; SOL fails the spot listing check
    import yaml

    doc = yaml.safe_load(Path(result["snapshot"]).read_text(encoding="utf-8"))
    assert Path(result["snapshot"]).name == "universe_snapshot_2025-01-01.yaml"
    assert doc["purpose"] == "research"
    assert doc["windows"]["volume"] == ["2024-10-03T00:00:00.000Z", "2025-01-01T00:00:00.000Z"]
    assert doc["windows"]["listing_cutoff"] == "2023-01-01T00:00:00.000Z"
    assert {e["symbol"]: e["reason"] for e in doc["excluded"]}["SOLUSDT"].startswith("spot not")
    assert len(doc["inputs"]["dataset_ids"]) == 8  # 3 symbols x 2 markets + 2 listing probes
    assert all(len(v["sha256"]) == 64 for v in doc["inputs"]["exchange_info"].values())
    assert not any("/klines" in u and "api.binance.com" in u for u in log)  # archive only
    with pytest.raises(UniverseError, match="after the holdout start"):
        asyncio.run(main(date(2026, 10, 19)))


def test_live_universe_uses_rest_and_status(tmp_path: Path) -> None:
    pytest.importorskip("yaml")
    spot = [dict(s, status="BREAK") if s["symbol"] == "ETHUSDT" else s for s in SPOT_SYMBOLS]
    log: list[str] = []
    handler = make_handler(
        FakeArchive(), exchange_info("spot", spot), exchange_info("usdm", USDM_SYMBOLS), NOW, log
    )

    async def main() -> dict[str, Any]:
        env = await open_env(
            tmp_path / "data",
            clock=ManualClock(NOW),
            codec=JsonLinesCodec(),
            http_transport=httpx.MockTransport(handler),
        )
        try:
            return await run_universe(
                env, None, UniverseRule(top_n=2), tmp_path / "configs", mode="live"
            )
        finally:
            await env.close()

    result = asyncio.run(main())
    assert result["ok"], result
    assert result["purpose"] == "live_forward"
    assert Path(result["snapshot"]).name == "universe_live_2026-10-20.yaml"
    assert result["candidates"] == 2  # ETHUSDT excluded by today's status
    assert not any(u.startswith("data.binance.vision") for u in log)
    assert list((tmp_path / "data").glob("imports/*/manifest.json")) == []  # lake untouched
