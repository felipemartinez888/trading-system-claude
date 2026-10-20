"""Deterministic bar factories for simulator tests, golden trades and replay fixtures.

Not used by the engine itself. Bars carry `BarSource.DERIVED` only to satisfy the contract; they are
synthetic and must never be presented as market data (label results DEMO/BACKTEST accordingly).
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from tsclaude.contracts.market import Bar, BarSource, Interval, Market
from tsclaude.contracts.signals import EntryRuleKind, Side
from tsclaude.contracts.timeutil import MS_PER_MINUTE
from tsclaude.sim.book import Book, BookConfig, RiskHook, SimEventType, allow_all
from tsclaude.sim.clock import HistoricalClock
from tsclaude.sim.costs import CostModel
from tsclaude.sim.engine import SimResult, Simulator, SimView
from tsclaude.sim.money import q8
from tsclaude.sim.orders import EntryRequest, ExitRequest, InstrumentSpec

#: 2025-01-01T00:00:00Z
T0 = 1_735_689_600_000

type OHLC = tuple[
    float | str | Decimal, float | str | Decimal, float | str | Decimal, float | str | Decimal
]


def bar(
    i: int,
    o: float | str | Decimal,
    h: float | str | Decimal,
    lo: float | str | Decimal,
    c: float | str | Decimal,
    *,
    symbol: str = "BTCUSDT",
    market: Market = Market.SPOT,
    start_ms: int = T0,
    quote_volume: float = 1_000_000.0,
) -> Bar:
    """Closed 1m bar number `i` after `start_ms` (prices given as exact decimal text or floats)."""
    open_ms = start_ms + i * MS_PER_MINUTE
    return Bar(
        market=market,
        symbol=symbol,
        interval=Interval.M1,
        open_time_ms=open_ms,
        close_time_ms=open_ms + MS_PER_MINUTE - 1,
        open=float(o),
        high=float(h),
        low=float(lo),
        close=float(c),
        volume=1.0,
        quote_volume=quote_volume,
        trades=1,
        taker_buy_volume=0.5,
        taker_buy_quote_volume=quote_volume / 2,
        source=BarSource.DERIVED,
        ingested_at_ms=open_ms + MS_PER_MINUTE,
    )


def series(
    rows: Sequence[OHLC],
    *,
    symbol: str = "BTCUSDT",
    market: Market = Market.SPOT,
    start_ms: int = T0,
    first_index: int = 0,
) -> list[Bar]:
    return [
        bar(first_index + i, *row, symbol=symbol, market=market, start_ms=start_ms)
        for i, row in enumerate(rows)
    ]


def chunked[T](items: Sequence[T], sizes: Iterable[int]) -> list[list[T]]:
    """Split `items` into consecutive chunks of the given sizes (the last chunk takes the rest)."""
    out: list[list[T]] = []
    pos = 0
    for size in sizes:
        if pos >= len(items):
            break
        out.append(list(items[pos : pos + size]))
        pos += size
    if pos < len(items):
        out.append(list(items[pos:]))
    return out


def minute(i: int, start_ms: int = T0) -> int:
    """Open time of bar `i`."""
    return start_ms + i * MS_PER_MINUTE


class ScriptedSource:
    """Signal source that submits pre-scripted requests at given bar closes.

    `script` maps (bar open_time_ms, symbol) -> requests emitted when that bar is delivered.
    `seen` records every bar delivered, to check nothing is delivered twice or out of order.
    """

    def __init__(self, script: Mapping[tuple[int, str], Sequence[EntryRequest | ExitRequest]]):
        self.script = dict(script)
        self.seen: list[tuple[int, str]] = []

    def on_bar(self, bar: Bar, view: SimView) -> Sequence[EntryRequest | ExitRequest]:
        self.seen.append((bar.open_time_ms, bar.symbol))
        return list(self.script.get((bar.open_time_ms, bar.symbol), ()))


def spot_spec(symbol: str = "BTCUSDT", tick: str = "0.1", lot: str = "0.001") -> InstrumentSpec:
    return InstrumentSpec(Market.SPOT, symbol, Decimal(tick), Decimal(lot))


def usdm_spec(symbol: str = "BTCUSDT", tick: str = "0.1", lot: str = "0.001") -> InstrumentSpec:
    return InstrumentSpec(Market.USDM, symbol, Decimal(tick), Decimal(lot))


def run_book(
    bars: Sequence[Bar],
    script: Mapping[tuple[int, str], Sequence[EntryRequest | ExitRequest]],
    *,
    specs: Sequence[InstrumentSpec],
    cost_model: CostModel | None = None,
    equity: Decimal = Decimal(10_000),
    config: BookConfig | None = None,
    risk_hook: RiskHook = allow_all,
    decision_latency_ms: int = 0,
) -> tuple[Book, SimResult]:
    """Run one book named `baseline` over `bars` with a scripted source."""
    book = Book(
        "baseline",
        equity,
        cost_model or CostModel(),
        risk_hook,
        instruments=specs,
        config=config,
    )
    sim = Simulator(
        HistoricalClock(bars),
        [book],
        ScriptedSource(script),
        decision_latency_ms=decision_latency_ms,
    )
    return book, sim.run()


def events_of(result: SimResult, kind: str) -> list[dict[str, Any]]:
    return [e["payload"] for e in result.events if e["type"] == kind]


@dataclass(frozen=True, slots=True)
class Scenario:
    bars: list[Bar]
    script: dict[tuple[int, str], list[EntryRequest | ExitRequest]]
    specs: list[InstrumentSpec]
    flat_from_ms: int  # every position is asked to exit at this bar close; no entries afterwards


def random_scenario(
    seed: int,
    *,
    n_bars: int = 240,
    symbols: Sequence[tuple[Market, str]] = ((Market.SPOT, "BTCUSDT"), (Market.USDM, "ETHUSDT")),
    signal_prob: float = 0.08,
    gap_prob: float = 0.02,
) -> Scenario:
    """Seeded random-walk bars plus random entry/exit requests (all order kinds, both sides).

    Prices live on a 0.1 grid so they are exact in both float and Decimal. Shorts are generated only
    for USD-M symbols. The last 10 bars only carry exit requests, so runs end flat.
    """
    rng = random.Random(seed)  # noqa: S311 - deterministic test data, not cryptography
    tick = Decimal("0.1")
    bars: list[Bar] = []
    script: dict[tuple[int, str], list[EntryRequest | ExitRequest]] = {}
    specs = [InstrumentSpec(m, s, tick, Decimal("0.001"), Decimal("5")) for m, s in symbols]
    flat_index = n_bars - 10
    for m, s in symbols:
        price = rng.randint(5_000, 20_000)  # in ticks
        for i in range(n_bars):
            if i and rng.random() < gap_prob:
                continue  # missing minute: exercises gap reporting and gap-through opens
            o = price + (rng.randint(-30, 30) if rng.random() < 0.1 else rng.randint(-2, 2))
            c = max(10, o + rng.randint(-15, 15))
            h = max(o, c) + rng.randint(0, 10)
            lo = max(1, min(o, c) - rng.randint(0, 10))
            price = c
            bars.append(bar(i, o * tick, h * tick, lo * tick, c * tick, symbol=s, market=m))
            key = (minute(i), s)
            if i >= flat_index:
                if i == flat_index:
                    script.setdefault(key, []).append(ExitRequest(f"x{seed}-{s}-{i}", m, s))
                continue
            if rng.random() >= signal_prob:
                continue
            side = Side.SHORT if m is Market.USDM and rng.random() < 0.5 else Side.LONG
            sign = 1 if side is Side.LONG else -1
            kind = rng.choice(list(EntryRuleKind))
            ref = c
            entry: int | None = None
            if kind is EntryRuleKind.LIMIT:
                entry = ref - sign * rng.randint(0, 20)
            elif kind is EntryRuleKind.STOP:
                entry = ref + sign * rng.randint(1, 20)
            anchor = ref if entry is None else entry
            stop = max(1, anchor - sign * rng.randint(5, 60))
            tp = anchor + sign * rng.randint(5, 120) if rng.random() < 0.8 else None
            if tp is not None and tp <= 0:
                tp = None
            reqs = script.setdefault(key, [])
            if rng.random() < 0.15:
                reqs.append(ExitRequest(f"x{seed}-{s}-{i}", m, s))
            reqs.append(
                EntryRequest(
                    request_id=f"e{seed}-{s}-{i}",
                    market=m,
                    symbol=s,
                    side=side,
                    kind=kind,
                    entry_price=None if entry is None else entry * tick,
                    stop_price=stop * tick,
                    take_profit_price=None if tp is None else tp * tick,
                    valid_until_ms=minute(i + rng.randint(1, 8)) + MS_PER_MINUTE - 1,
                    time_exit_bars=rng.choice([None, None, 3, 10]),
                    strategy_interval=rng.choice([Interval.M1, Interval.M5]),
                )
            )
    return Scenario(bars, script, specs, minute(flat_index))


def check_book_invariants(book: Book, result: SimResult) -> list[str]:
    """Ledger and sizing invariants of a finished run; returns a list of violations (empty = ok)."""
    problems: list[str] = []
    report = book.ledger.reconcile()
    problems.extend(report.problems)
    cash = Decimal(0)
    for tx in book.ledger.entries:
        if sum((amount for _, amount in tx.legs), Decimal(0)) != 0:
            problems.append(f"tx {tx.tx_id} unbalanced")
        cash += sum((amount for account, amount in tx.legs if account == "cash"), Decimal(0))
        if cash < 0:
            problems.append(f"cash negative after tx {tx.tx_id}: {cash}")
    if q8(report.equity_from_entries + book.unrealized_pnl()) != book.equity():
        problems.append("equity is not reconstructible from ledger entries plus marks")
    if book.equity_curve and book.equity_curve[-1].equity != book.equity():
        problems.append("last equity point differs from final equity")
    cfg = book.config
    for e in result.events:
        if e["type"] != SimEventType.POSITION_OPENED or e["aggregate_id"] != book.name:
            continue
        p = e["payload"]
        spec = book.instruments[(Market(p["market"]), p["symbol"])]
        qty = Decimal(p["qty"])
        notional = Decimal(p["notional"])
        equity = Decimal(p["sizing_equity"])
        distance = abs(Decimal(p["entry_price"]) - Decimal(p["stop_price"]))
        cap = cfg.max_notional_fraction * equity
        if cfg.max_notional_abs is not None:
            cap = min(cap, cfg.max_notional_abs)
        tid = p["trade_id"]
        if qty <= 0 or qty % spec.lot_size != 0:
            problems.append(f"{tid}: qty {qty} not a positive multiple of the lot")
        if notional < spec.min_notional:
            problems.append(f"{tid}: notional below min_notional")
        if notional > cap:
            problems.append(f"{tid}: notional {notional} exceeds cap {cap}")
        if qty * distance > cfg.risk_per_trade * equity:
            problems.append(f"{tid}: planned risk exceeds risk_per_trade x equity")
        if notional + Decimal(p["entry_fee"]) > Decimal(p["sizing_cash"]):
            problems.append(f"{tid}: notional + fee exceeds available cash")
    for t in book.trades:
        if t.net_pnl != t.gross_pnl - t.fees + t.funding:
            problems.append(f"{t.trade_id}: net != gross - fees + funding")
        if t.r_multiple != q8(t.net_pnl / t.risk_amount):
            problems.append(f"{t.trade_id}: r_multiple inconsistent")
        if t.mae_r < 0 or t.mfe_r < 0:
            problems.append(f"{t.trade_id}: negative MAE/MFE")
    return problems


class InvariantSource:
    """Wraps a source and checks, at every bar, that equity is reconstructible from the ledger."""

    def __init__(self, inner: ScriptedSource, books: Sequence[Book]) -> None:
        self.inner = inner
        self.books = books
        self.problems: list[str] = []
        self.checks = 0

    def on_bar(self, bar: Bar, view: SimView) -> Sequence[EntryRequest | ExitRequest]:
        for book in self.books:
            self.checks += 1
            report = book.ledger.reconcile()
            if not report.ok:
                self.problems.append(f"{bar.open_time_ms}: {report.problems}")
            if q8(report.equity_from_entries + book.unrealized_pnl()) != book.equity():
                self.problems.append(f"{bar.open_time_ms}: equity not reconstructible")
            if book.cash < 0:
                self.problems.append(f"{bar.open_time_ms}: negative cash {book.cash}")
        return self.inner.on_bar(bar, view)
