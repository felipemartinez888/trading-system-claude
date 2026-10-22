"""Builders for risk-engine tests: a passing baseline check and market view, perturbed per test."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D
from typing import Any

from tsclaude.contracts.market import Market, StreamState
from tsclaude.contracts.provenance import Provenance, Sourced
from tsclaude.contracts.signals import EntryRuleKind, Side
from tsclaude.marketdata.models import BookTop, MarketSnapshot, StreamStatus, SymbolSnapshot
from tsclaude.risk.market import MarketView, SymbolMarket
from tsclaude.sim.book import PositionView, RiskCheck
from tsclaude.sim.orders import EntryRequest
from tsclaude.sim.testing import minute

DECISION = minute(10) - 1  # close of bar 9
EXEC = minute(10)


def request(**kw: Any) -> EntryRequest:
    args: dict[str, Any] = {
        "request_id": "sig-1",
        "market": Market.SPOT,
        "symbol": "BTCUSDT",
        "side": Side.LONG,
        "kind": EntryRuleKind.MARKET_NEXT_OPEN,
        "stop_price": D("990"),
        "take_profit_price": D("1030"),
        "valid_until_ms": minute(20),
    }
    args.update(kw)
    return EntryRequest(**args)


def check(stage: str = "submit", **kw: Any) -> RiskCheck:
    """Passing baseline: flat book at 10 000, 0.5 % risk, no positions."""
    req = kw.pop("request", None) or request()
    positions = tuple(kw.pop("positions", ()))
    args: dict[str, Any] = {
        "stage": stage,
        "book": "baseline",
        "ts_ms": DECISION if stage == "submit" else EXEC,
        "request": req,
        "equity": D("10000"),
        "open_positions": len(positions),
        "positions": positions,
        "pending_entries": 0,
        "start_of_day_equity": D("10000"),
        "peak_equity": D("10000"),
        "earliest_exec_ms": EXEC,
        "risk_per_trade": D("0.005"),
        "max_notional_fraction": D("1"),
    }
    args.update(kw)
    return RiskCheck(**args)


def position(symbol: str, notional: str = "100", market: Market = Market.SPOT) -> PositionView:
    n = D(notional)
    return PositionView(market, symbol, Side.LONG, n / 100, D("100"), n, D("100"))


def symbol_market(**kw: Any) -> SymbolMarket:
    base = SymbolMarket(
        market=Market.SPOT,
        symbol="BTCUSDT",
        unhealthy_streams=(),
        stream_count=2,
        open_gap_minutes=0,
        last_bar_close_ms=DECISION,
        last_close=D("1000"),
        last_quote_volume=D("1000000"),
        spread_bps=D("1"),
        spread_as_of_ms=DECISION - 500,
        median_spread_bps=D("1"),
        spread_samples=60,
    )
    return replace(base, **kw)


def view(as_of_ms: int = DECISION - 400, offset: int | None = 20, **kw: Any) -> MarketView:
    sym = symbol_market(**kw)
    return MarketView(as_of_ms, offset, {(sym.market, sym.symbol): sym})


# ---------------------------------------------------------------- market snapshots
def _stream(market: Market, symbol: str, kind: str, state: StreamState, since: int, now: int):
    return StreamStatus(
        stream=f"{market}:{symbol}:{kind}",
        market=market,
        symbol=symbol,
        kind=kind,
        state=Sourced[StreamState](value=state, provenance=Provenance.SYSTEM, as_of_ms=now),
        since_ms=since,
        budget_ms=5000,
        last_message_ms=now,
        last_fresh_ms=now,
        disconnects=0,
    )


def snapshot(
    now: int,
    *,
    bid: float = 999.95,
    ask: float = 1000.05,
    states: dict[str, tuple[StreamState, int]] | None = None,
    gap_minutes: int = 0,
    offset: int | None = 15,
    running: bool = True,
    symbol: str = "BTCUSDT",
    market: Market = Market.SPOT,
    symbols: tuple[tuple[Market, str], ...] | None = None,
) -> MarketSnapshot:
    """A market-data snapshot at `now` for one symbol (or `symbols`), all with the same values."""
    pairs = symbols or ((market, symbol),)
    states = states or {
        "kline_1m": (StreamState.HEALTHY, now - 600_000),
        "book_ticker": (StreamState.HEALTHY, now - 600_000),
    }
    streams = [
        _stream(m, sym, k, st, since, now) for m, sym in pairs for k, (st, since) in states.items()
    ]
    top = BookTop(
        bid_price=bid,
        ask_price=ask,
        bid_qty=1.0,
        ask_qty=1.0,
        update_id=now,
        received_at_ms=now - 100,
        event_time_ms=None,
    )
    syms = [
        SymbolSnapshot(
            market=m,
            symbol=sym,
            last_closed_bar=Sourced.unavailable("not used"),
            partial_candle=Sourced.unavailable("not used"),
            best_bid_ask=Sourced[BookTop](
                value=top, provenance=Provenance.LIVE, as_of_ms=now - 100
            ),
            mark_price=Sourced.unavailable("spot"),
            funding=Sourced.unavailable("spot"),
            open_gap_minutes=gap_minutes,
            exchange_status=Sourced.unavailable("not loaded"),
        )
        for m, sym in pairs
    ]
    off = (
        Sourced[int](value=offset, provenance=Provenance.LIVE, as_of_ms=now)
        if offset is not None
        else Sourced[int].unavailable("not sampled")
    )
    return MarketSnapshot(
        as_of_ms=now,
        running=running,
        clock_offset_ms=off,
        rest_banned={},
        streams=streams,
        symbols=syms,
        connections=[],
    )
