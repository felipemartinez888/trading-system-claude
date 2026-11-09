from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tsclaude.contracts.market import Market
from tsclaude.marketdata.binance.ws import combined_url, plan_groups, stream_name
from tsclaude.marketdata.config import (
    MarketDataConfig,
    WsConfig,
    apply_dev_profile,
    load_config,
    load_dev_profile,
)
from tsclaude.marketdata.types import StreamKey, StreamKind

REPO = Path(__file__).resolve().parents[3]
UNIVERSE = {
    "version": 0,
    "snapshot_date": "2026-10-19",
    "rule": "test",
    "spot": ["BTCUSDT", "ETHUSDT"],
    "usdm": ["BTCUSDT"],
}


def test_defaults_and_pairs() -> None:
    cfg = MarketDataConfig.from_mapping(UNIVERSE)
    assert cfg.universe.pairs() == [
        (Market.SPOT, "BTCUSDT"),
        (Market.SPOT, "ETHUSDT"),
        (Market.USDM, "BTCUSDT"),
    ]
    q = cfg.quality
    assert q.watermark.grace_s == 5
    assert q.ws.recycle_after_s == 23 * 3600
    assert q.backfill.lookback_days == 3
    assert q.endpoints.usdm_ws_market == ("wss://fstream.binance.com/market",)
    assert q.endpoints.usdm_ws_public == ("wss://fstream.binance.com/public",)


@pytest.mark.parametrize(
    "quality",
    [
        {"freshness": {"kline_1m_s": 0}},
        {"freshness": {"typo_s": 3}},
        {"ws": {"recycle_after_s": 86400}},
        {"ws": {"control_msgs_per_s_spot": 5}},
        {"ws": {"control_msgs_per_s_futures": 10}},
        {"ws": {"backoff_base_s": 10, "backoff_cap_s": 5}},
        {"rest": {"weight_budget_fraction": 1.5}},
        {"backfill": {"lookback_days": 10, "max_backfill_days": 5}},
    ],
)
def test_invalid_quality_rejected(quality: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        MarketDataConfig.from_mapping(UNIVERSE, quality)


@pytest.mark.parametrize("symbols", [["btcusdt"], ["BTC-USDT"], ["BTCUSDT", "BTCUSDT"]])
def test_invalid_symbols_rejected(symbols: list[str]) -> None:
    with pytest.raises(ValidationError):
        MarketDataConfig.from_mapping({**UNIVERSE, "spot": symbols})


def test_repo_yaml_files_load() -> None:
    pytest.importorskip("yaml")
    cfg = load_config(REPO / "configs" / "universe.yaml", REPO / "configs" / "data_quality.yaml")
    assert cfg.universe.spot == ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    assert cfg.universe.usdm == ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    assert cfg.quality.freshness.mark_price_s == 5
    assert cfg.quality.ws.connect_budget <= 300


def test_group_plan_and_urls() -> None:
    cfg = MarketDataConfig.from_mapping(UNIVERSE)
    groups = plan_groups(cfg.universe, cfg.quality.endpoints, cfg.quality.ws)
    assert [g.name for g in groups] == ["spot", "usdm-market", "usdm-public"]
    spot, fmarket, fpublic = groups
    assert spot.control_msgs_per_s == 4
    assert fmarket.control_msgs_per_s == 8
    assert {k.kind for k in fmarket.streams} == {StreamKind.KLINE_1M, StreamKind.MARK_PRICE}
    assert {k.kind for k in fpublic.streams} == {StreamKind.BOOK_TICKER}
    assert combined_url(fmarket.urls[0], fmarket.streams) == (
        "wss://fstream.binance.com/market/stream?streams=btcusdt@kline_1m/btcusdt@markPrice@1s"
    )
    assert combined_url(spot.urls[0], spot.streams[:2]) == (
        "wss://stream.binance.com:9443/stream?streams=btcusdt@kline_1m/btcusdt@bookTicker"
    )
    assert stream_name(StreamKey(Market.SPOT, "SOLUSDT", StreamKind.BOOK_TICKER)) == (
        "solusdt@bookTicker"
    )


def test_groups_split_by_max_streams() -> None:
    cfg = MarketDataConfig.from_mapping(UNIVERSE, {"ws": {"max_streams_per_connection": 3}})
    groups = plan_groups(
        cfg.universe, cfg.quality.endpoints, WsConfig(max_streams_per_connection=3)
    )
    assert [g.name for g in groups] == ["spot#1", "spot#2", "usdm-market", "usdm-public"]
    assert [len(g.streams) for g in groups] == [3, 1, 2, 1]


# ---------------------------------------------------------------------- ingestion limits
def test_backfill_defaults_keep_the_legacy_window_and_bounded_runs() -> None:
    cfg = MarketDataConfig.from_mapping(UNIVERSE, {})
    b = cfg.quality.backfill
    assert b.startup.catchup_max_minutes is None  # the whole lookback is the catch-up
    assert b.startup.history == "off"
    assert (b.run.chunk_minutes, b.run.max_consecutive_chunk_errors) == (1000, 3)


@pytest.mark.parametrize(
    "backfill",
    [
        {"run": {"chunk_minutes": 0}},
        {"run": {"chunk_minutes": 1001}},
        {"run": {"pair_budget_s": 700, "run_budget_s": 600}},
        {"run": {"write_timeout_s": 100, "write_join_timeout_s": 90}},
        {"run": {"max_consecutive_chunk_errors": 0}},
        {"run": {"typo": 1}},
        {"startup": {"history": "always"}},
        {"startup": {"catchup_max_minutes": 0}},
        {"lookback_days": 1, "startup": {"catchup_max_minutes": 1441}},
    ],
)
def test_invalid_ingestion_limits_rejected(backfill: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        MarketDataConfig.from_mapping(UNIVERSE, {"backfill": backfill})


def test_repo_yaml_selects_on_demand_history_and_bounded_runs() -> None:
    pytest.importorskip("yaml")
    cfg = load_config(REPO / "configs" / "universe.yaml", REPO / "configs" / "data_quality.yaml")
    b = cfg.quality.backfill
    assert b.lookback_days == 9  # the history target the paper warm-up needs is unchanged
    assert b.startup.history == "on_demand"
    assert b.startup.catchup_max_minutes == 1440
    import yaml

    paper = yaml.safe_load((REPO / "configs" / "paper.yaml").read_text(encoding="utf-8"))
    # a run that exhausts its budget must end before paper gives up waiting for it
    assert b.run.run_budget_s <= float(paper["backfill_wait_s"])


def test_dev_profile_narrows_to_one_pair_and_never_widens() -> None:
    pytest.importorskip("yaml")
    base = load_config(REPO / "configs" / "universe.yaml", REPO / "configs" / "data_quality.yaml")
    profile = load_dev_profile(REPO / "configs" / "ingest_dev.yaml")
    dev = apply_dev_profile(base, profile)
    assert dev.universe.pairs() == [(Market.SPOT, "BTCUSDT")]
    assert dev.quality.backfill.startup.catchup_max_minutes == 60
    assert dev.quality.backfill.run.chunk_minutes == 100
    assert dev.quality.ws == base.quality.ws  # only the backfill section is replaced
    # the base config is untouched (frozen models, copies)
    assert len(base.universe.pairs()) == 6
    outside = profile.model_copy(update={"spot": ("DOGEUSDT",)})
    with pytest.raises(ValueError, match="not in the spot universe"):
        apply_dev_profile(base, outside)
