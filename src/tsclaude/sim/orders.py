"""Orders, entry requests and instrument rounding (SIMULATION §3).

Rounding rules (always adverse to the book):
- **Prices** round to the tick toward the *worse execution price* for the side that executes:
  buy-side prices (long entries, short stop-loss, short take-profit) round **up**; sell-side prices
  (short entries, long stop-loss, long take-profit) round **down**. A long stop rounded down also
  widens the risk distance, which shrinks the fixed-fractional size.
- **Quantities** round **down** to the lot size.

Order semantics:
- MARKET: fills at the open of the first 1m bar whose open is at/after the earliest execution time,
  taker, with slippage.
- LIMIT: fills only when a bar trades **through** the limit by at least one tick (buy: low <= limit
  - tick; sell: high >= limit + tick). Touching is not filling. Resting fills are maker fills at the
  limit price (never better). A limit that is already marketable when it *arrives* (the first
  eligible open is through it) executes as a taker at that open with slippage, capped at the limit.
- STOP: triggers when the 1m path reaches the stop (touch counts: buy high >= stop, sell low <=
  stop); fills at the stop, or **at the open if the bar gapped beyond it**, plus slippage, taker.
- Bracket children: stop-loss (STOP) and take-profit (LIMIT) are one-cancels-other.
- Time exit: market at the first 1m open at/after `floor(entry_open / I) * I + N * I`, i.e. after
  `N` strategy bars of interval `I` have closed since the entry.
- Expiry: an entry may fill at an open with `open_time_ms <= valid_until_ms`, or inside a bar only
  when the whole bar lies inside the window (`close_time_ms <= valid_until_ms`); afterwards it is
  expired.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from tsclaude.contracts.market import Interval, Market
from tsclaude.contracts.signals import EntryRuleKind, Side, SignalRecord
from tsclaude.sim.costs import Direction
from tsclaude.sim.money import ceil_to, dstr, floor_to, to_decimal


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class OrderRole(StrEnum):
    ENTRY = "entry"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TIME_EXIT = "time_exit"
    EXIT = "exit"  # invalidation / manual exit requested by the signal source


class OrderStatus(StrEnum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"


ENTRY_ORDER_TYPE: dict[EntryRuleKind, OrderType] = {
    EntryRuleKind.MARKET_NEXT_OPEN: OrderType.MARKET,
    EntryRuleKind.LIMIT: OrderType.LIMIT,
    EntryRuleKind.STOP: OrderType.STOP,
}


def entry_direction(side: Side) -> Direction:
    return Direction.BUY if side is Side.LONG else Direction.SELL


def exit_direction(side: Side) -> Direction:
    return Direction.SELL if side is Side.LONG else Direction.BUY


def side_sign(side: Side) -> int:
    return 1 if side is Side.LONG else -1


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    market: Market
    symbol: str
    tick_size: Decimal
    lot_size: Decimal
    min_notional: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        for name in ("tick_size", "lot_size"):
            v = getattr(self, name)
            if not isinstance(v, Decimal) or not v.is_finite() or v <= 0:
                raise ValueError(f"{name} must be a positive Decimal")
        if not self.min_notional.is_finite() or self.min_notional < 0:
            raise ValueError("min_notional must be a non-negative Decimal")

    @property
    def key(self) -> tuple[Market, str]:
        return (self.market, self.symbol)

    def round_price(self, price: Decimal, direction: Direction) -> Decimal:
        """Round to the tick toward the worse price for `direction` (buy up, sell down)."""
        if price <= 0:
            raise ValueError("price must be positive")
        if direction is Direction.BUY:
            return ceil_to(price, self.tick_size)
        rounded = floor_to(price, self.tick_size)
        if rounded <= 0:
            raise ValueError("price rounds to zero at this tick size")
        return rounded

    def round_qty(self, qty: Decimal) -> Decimal:
        """Round down to the lot size (never up)."""
        if qty <= 0:
            return Decimal(0)
        return floor_to(qty, self.lot_size)

    def to_payload(self) -> dict[str, str]:
        return {
            "market": str(self.market),
            "symbol": self.symbol,
            "tick_size": dstr(self.tick_size),
            "lot_size": dstr(self.lot_size),
            "min_notional": dstr(self.min_notional),
        }


@dataclass(frozen=True, slots=True)
class EntryRequest:
    """What a signal source submits. Prices are *unrounded* intent; the book rounds them.

    `decision_time_ms` is not part of the request: the simulator stamps it with the close time of
    the bar being delivered, so a source cannot claim an earlier decision.
    """

    request_id: str
    market: Market
    symbol: str
    side: Side
    kind: EntryRuleKind
    stop_price: Decimal
    valid_until_ms: int
    entry_price: Decimal | None = None
    take_profit_price: Decimal | None = None
    time_exit_bars: int | None = None
    strategy_interval: Interval = Interval.M1
    books: tuple[str, ...] | None = None  # None = every book
    signal_id: str | None = None

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("request_id is required")
        for name in ("stop_price", "entry_price", "take_profit_price"):
            v = getattr(self, name)
            if v is not None and (not isinstance(v, Decimal) or not v.is_finite() or v <= 0):
                raise ValueError(f"{name} must be a positive finite Decimal")
        needs_price = self.kind in (EntryRuleKind.LIMIT, EntryRuleKind.STOP)
        if needs_price and self.entry_price is None:
            raise ValueError(f"{self.kind} entries need entry_price")
        if not needs_price and self.entry_price is not None:
            raise ValueError("market entries must not carry entry_price")
        if self.time_exit_bars is not None and self.time_exit_bars < 1:
            raise ValueError("time_exit_bars must be >= 1")
        ref = self.entry_price
        if ref is not None:
            long = self.side is Side.LONG
            if long and not self.stop_price < ref:
                raise ValueError("long stop must be below the entry price")
            if not long and not self.stop_price > ref:
                raise ValueError("short stop must be above the entry price")
            tp = self.take_profit_price
            if tp is not None and ((long and tp <= ref) or (not long and tp >= ref)):
                raise ValueError("take-profit is on the wrong side of the entry price")

    @classmethod
    def from_signal(
        cls,
        record: SignalRecord,
        *,
        time_exit_bars: int | None = None,
        books: tuple[str, ...] | None = None,
    ) -> EntryRequest:
        """Translate a contract `SignalRecord` (floats) into an exact request (Decimals)."""
        rule = record.entry_rule
        return cls(
            request_id=record.signal_id,
            market=record.market,
            symbol=record.symbol,
            side=record.side,
            kind=rule.kind,
            entry_price=None if rule.price is None else to_decimal(rule.price),
            stop_price=to_decimal(record.stop_price),
            take_profit_price=(
                None if record.take_profit_price is None else to_decimal(record.take_profit_price)
            ),
            valid_until_ms=rule.valid_until_ms,
            time_exit_bars=time_exit_bars,
            strategy_interval=record.timeframe,
            books=books,
            signal_id=record.signal_id,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "signal_id": self.signal_id,
            "market": str(self.market),
            "symbol": self.symbol,
            "side": str(self.side),
            "kind": str(self.kind),
            "entry_price": None if self.entry_price is None else dstr(self.entry_price),
            "stop_price": dstr(self.stop_price),
            "take_profit_price": (
                None if self.take_profit_price is None else dstr(self.take_profit_price)
            ),
            "valid_until_ms": self.valid_until_ms,
            "time_exit_bars": self.time_exit_bars,
            "strategy_interval": str(self.strategy_interval),
            "books": None if self.books is None else list(self.books),
        }


@dataclass(slots=True)
class Order:
    """A simulated order. Entries carry no quantity until execution (sized at fill time)."""

    order_id: str
    book: str
    market: Market
    symbol: str
    role: OrderRole
    type: OrderType
    direction: Direction
    price: Decimal | None  # tick-rounded (adverse); None for market orders
    decision_time_ms: int
    earliest_exec_ms: int  # first eligible bar open time
    valid_until_ms: int | None = None
    qty: Decimal | None = None
    request: EntryRequest | None = None
    status: OrderStatus = OrderStatus.PENDING
    bars_seen: int = 0  # eligible bars evaluated (0 = next eligible bar is its arrival bar)

    def to_payload(self) -> dict[str, object]:
        return {
            "order_id": self.order_id,
            "book": self.book,
            "market": str(self.market),
            "symbol": self.symbol,
            "role": str(self.role),
            "type": str(self.type),
            "direction": str(self.direction),
            "price": None if self.price is None else dstr(self.price),
            "qty": None if self.qty is None else dstr(self.qty),
            "decision_time_ms": self.decision_time_ms,
            "earliest_exec_ms": self.earliest_exec_ms,
            "valid_until_ms": self.valid_until_ms,
        }
