"""Universe rule (D-04) on synthetic exchangeInfo and volumes."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from arc_helpers import DAY, ms

from tsclaude.contracts.market import Market
from tsclaude.marketdata.types import ExchangeInfo, SymbolInfo
from tsclaude.marketdata.universe import (
    SymbolData,
    UniverseRule,
    months_before,
    select_universe,
    snapshot_document,
    static_candidates,
    windows,
)

SNAP = date(2026, 10, 19)
RULE = UniverseRule(stablecoin_bases=frozenset({"USDC", "FDUSD"}), top_n=3, min_volume_days=85)


def _sym(
    symbol: str,
    base: str,
    quote: str = "USDT",
    status: str = "TRADING",
    contract: str | None = None,
    onboard: int | None = None,
) -> SymbolInfo:
    return SymbolInfo(symbol, status, base, quote, None, None, None, (), contract, onboard)


def _infos() -> tuple[ExchangeInfo, ExchangeInfo]:
    old = ms(2020, 1, 1)
    spot = [
        _sym("BTCUSDT", "BTC"),
        _sym("ETHUSDT", "ETH"),
        _sym("SOLUSDT", "SOL"),
        _sym("XRPUSDT", "XRP"),
        _sym("DOGEUSDT", "DOGE"),
        _sym("USDCUSDT", "USDC"),
        _sym("BTCUPUSDT", "BTCUP"),
        _sym("JUPUSDT", "JUP"),
        _sym("NEWUSDT", "NEW"),
        _sym("HALTUSDT", "HALT", status="BREAK"),
        _sym("SPOTONLYUSDT", "SPOTONLY"),
        _sym("BTCEUR", "BTC", quote="EUR"),
        _sym("QTRUSDT", "QTR"),
    ]
    fut = [
        _sym(s.symbol, s.base_asset, contract="PERPETUAL", onboard=old)
        for s in spot
        if s.symbol not in ("SPOTONLYUSDT", "BTCEUR")
    ]
    fut = [
        f
        if f.symbol != "NEWUSDT"
        else _sym("NEWUSDT", "NEW", contract="PERPETUAL", onboard=ms(2025, 6))
        for f in fut
    ]
    fut = [
        f
        if f.symbol != "QTRUSDT"
        else _sym("QTRUSDT", "QTR", contract="CURRENT_QUARTER", onboard=old)
        for f in fut
    ]

    def info(market: Market, syms: list[SymbolInfo]) -> ExchangeInfo:
        return ExchangeInfo(market, None, (), {s.symbol: s for s in syms})

    return info(Market.SPOT, spot), info(Market.USDM, fut)


def test_months_before_handles_month_ends() -> None:
    assert months_before(date(2026, 10, 19), 24) == date(2024, 10, 19)
    assert months_before(date(2026, 2, 28), 24) == date(2024, 2, 28)
    assert months_before(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert months_before(date(2028, 2, 29), 12) == date(2027, 2, 28)


def test_static_candidates_apply_exchange_info_rules() -> None:
    spot, fut = _infos()
    w = windows(SNAP, RULE)
    assert w.listing_cutoff_ms == ms(2024, 10, 19)
    assert w.volume == (ms(2026, 10, 19) - 90 * DAY, ms(2026, 10, 19))
    candidates, excluded = static_candidates(spot, fut, RULE, w)
    reasons = {e["symbol"]: e["reason"] for e in excluded}
    assert candidates == ["BTCUSDT", "DOGEUSDT", "ETHUSDT", "JUPUSDT", "SOLUSDT", "XRPUSDT"]
    assert reasons["USDCUSDT"] == "stablecoin base"
    assert reasons["BTCUPUSDT"] == "leveraged token"
    assert "JUPUSDT" not in reasons  # 'UP' suffix but J is not a base asset
    assert "after the 24-month cutoff" in reasons["NEWUSDT"]
    assert "status" in reasons["HALTUSDT"]
    assert "not listed on USD-M" in reasons["SPOTONLYUSDT"]
    assert "CURRENT_QUARTER" in reasons["QTRUSDT"]
    assert "BTCEUR" not in reasons  # not USDT-quoted: not a candidate at all


def _vols(level: float, days: int = 90) -> dict[int, float]:
    start = ms(2026, 10, 19) - 90 * DAY
    return {start + i * DAY: level * (1 + (i % 5) / 10) for i in range(days)}


def test_ranking_by_min_of_markets_and_data_requirements(tmp_path: Path) -> None:
    w = windows(SNAP, RULE)
    data = {
        "BTCUSDT": SymbolData(True, _vols(1000), _vols(5000)),
        "ETHUSDT": SymbolData(True, _vols(800), _vols(900)),
        "SOLUSDT": SymbolData(True, _vols(5000), _vols(300)),  # liquid spot, thin perp
        "XRPUSDT": SymbolData(True, _vols(600), _vols(600)),
        "DOGEUSDT": SymbolData(False, _vols(9999), _vols(9999)),  # spot too young
        "JUPUSDT": SymbolData(True, _vols(7000, days=60), _vols(7000)),  # not enough days
    }
    ranked, excluded = select_universe([*list(data), "MISSING"], data, RULE, w)
    assert [r["symbol"] for r in ranked] == ["BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT"]
    assert ranked[0]["score"] == ranked[0]["spot_median_daily_quote_volume"]
    reasons = {e["symbol"]: e["reason"] for e in excluded}
    assert "24 months" in reasons["DOGEUSDT"]
    assert "spot=60" in reasons["JUPUSDT"]
    assert reasons["MISSING"] == "no lake data"
    doc = snapshot_document(
        SNAP, RULE, w, ranked, excluded, {"dataset_ids": ["x"]}, ms(2026, 10, 19)
    )
    assert doc["symbols"] == ["BTCUSDT", "ETHUSDT", "XRPUSDT"]
    assert doc["spot"] == doc["usdm"] == doc["symbols"]
    assert doc["inputs"]["dataset_ids"] == ["x"]
    spot_rule = UniverseRule(rank_by="spot", top_n=1)
    ranked_spot, _ = select_universe(list(data), data, spot_rule, w)
    assert ranked_spot[0]["symbol"] == "SOLUSDT"


def test_rule_file_loads_and_rejects_unknown_keys() -> None:
    pytest.importorskip("yaml")
    from tsclaude.marketdata.universe import load_rule

    repo = Path(__file__).resolve().parents[3]
    rule = load_rule(repo / "configs" / "universe_rule.yaml")
    assert rule.top_n == 10
    assert rule.min_listing_months == 24
    assert "USDC" in rule.stablecoin_bases
    with pytest.raises(ValueError, match="unknown"):
        UniverseRule.from_mapping({"top_m": 3})
    with pytest.raises(ValueError, match="rank_by"):
        UniverseRule.from_mapping({"rank_by": "max"})
