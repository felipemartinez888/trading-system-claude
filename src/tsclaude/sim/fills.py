"""Deterministic fill engine on 1m OHLC paths (SIMULATION §2-§3).

Everything here is a pure function of one closed 1m bar and the order/position parameters, so the
same bar always produces the same decision on every platform.

Assumed intrabar path
---------------------
``O -> (H or L, whichever is nearer to O) -> (the other) -> C``, moving monotonically between those
points. On a tie (|H - O| == |O - L|) the low is assumed first. The path is used only to decide what
happens *after* an intrabar entry (which part of the bar the position experiences) and to measure
excursions; it never decides SL-versus-TP order, which is fixed by the ambiguity rule.

The open is the first trade of the bar. A position held from before the bar therefore resolves
gap-throughs at the open first (stop-loss before take-profit before a due time/market exit) and only
then looks at the rest of the bar.

Intrabar ambiguity rule
-----------------------
If the stop-loss and the take-profit are both reachable inside the remaining part of one 1m bar, the
outcome is **stop-loss first** and the exit is flagged ``ambiguous_bar=True``.

Trigger levels (``tick`` = instrument tick size)
------------------------------------------------
- Stop (entry or stop-loss): buy triggers at price >= stop, sell at price <= stop (touch counts).
  Gap beyond the stop at the open -> fill at the open; otherwise at the stop. Taker.
- Limit (entry or take-profit): buy fills only when price <= limit - tick, sell only when
  price >= limit + tick (trade-through by one tick; touching is not filling). Fill at the limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from tsclaude.contracts.market import Bar
from tsclaude.contracts.signals import Side
from tsclaude.sim.costs import Direction, Liquidity
from tsclaude.sim.money import to_decimal
from tsclaude.sim.orders import OrderType

type Path = tuple[Decimal, ...]


class ExitReason(StrEnum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TIME_EXIT = "time_exit"
    EXIT_SIGNAL = "exit_signal"


@dataclass(frozen=True, slots=True)
class BarPrices:
    """Exact Decimal view of a closed 1m bar."""

    open_time_ms: int
    close_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    @classmethod
    def from_bar(cls, bar: Bar) -> BarPrices:
        return cls(
            bar.open_time_ms,
            bar.close_time_ms,
            to_decimal(bar.open),
            to_decimal(bar.high),
            to_decimal(bar.low),
            to_decimal(bar.close),
        )

    def path(self) -> Path:
        o, h, lo = self.open, self.high, self.low
        if h - o < o - lo:
            return (o, h, lo, self.close)
        return (o, lo, h, self.close)


def _beyond(price: Decimal, level: Decimal, down: bool) -> bool:
    return price <= level if down else price >= level


def split_at(path: Path, level: Decimal, down: bool) -> tuple[Path, Path] | None:
    """Split the path at the first point where it reaches `level` (moving down or up).

    Returns ``(before, after)`` where ``before`` ends at the touch point and ``after`` starts at it.
    If the first point is already beyond the level, the touch point is that first point (a gap).
    Returns None if the path never reaches the level.
    """
    if _beyond(path[0], level, down):
        return (path[:1], path)
    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]
        if (min(a, b) <= level) if down else (max(a, b) >= level):
            # path[i] is not beyond the level (else an earlier segment matched), so the touch is
            # exactly at `level`, inside this monotone segment.
            return ((*path[: i + 1], level), (level, *path[i + 1 :]))
    return None


@dataclass(frozen=True, slots=True)
class EntryFill:
    raw_price: Decimal  # price before slippage (open, stop or limit)
    liquidity: Liquidity
    at_open: bool
    remaining: Path  # part of the bar the new position experiences
    cap: Decimal | None = None  # marketable-limit price cap


def resolve_entry(
    prices: BarPrices,
    order_type: OrderType,
    direction: Direction,
    price: Decimal | None,
    tick: Decimal,
    *,
    arrival: bool,
    open_ok: bool,
    intrabar_ok: bool,
) -> EntryFill | None:
    """Decide whether an eligible entry order fills on this bar, and how.

    `arrival` is True on the first eligible bar (the order reaches the market at its open);
    `open_ok` / `intrabar_ok` encode the validity window (see `orders` module docstring).
    """
    path = prices.path()
    o = prices.open
    buy = direction is Direction.BUY
    if order_type is OrderType.MARKET:
        return EntryFill(o, Liquidity.TAKER, True, path) if open_ok else None
    if price is None:
        raise ValueError(f"{order_type} order without price")
    if order_type is OrderType.LIMIT:
        level = price - tick if buy else price + tick
        if _beyond(o, level, down=buy):
            if not open_ok:
                return None
            if arrival:  # marketable on arrival: taker at the open, never worse than the limit
                return EntryFill(o, Liquidity.TAKER, True, path, cap=price)
            return EntryFill(price, Liquidity.MAKER, True, path)  # resting, gapped through
        if not intrabar_ok:
            return None
        split = split_at(path, level, down=buy)
        return None if split is None else EntryFill(price, Liquidity.MAKER, False, split[1])
    # STOP entry
    if _beyond(o, price, down=not buy):
        return EntryFill(o, Liquidity.TAKER, True, path) if open_ok else None
    if not intrabar_ok:
        return None
    split = split_at(path, price, down=not buy)
    return None if split is None else EntryFill(price, Liquidity.TAKER, False, split[1])


@dataclass(frozen=True, slots=True)
class ExitFill:
    reason: ExitReason
    raw_price: Decimal
    liquidity: Liquidity
    at_open: bool
    gap: bool  # stop filled at a gapped open beyond the stop
    ambiguous: bool  # SL and TP both reachable in this bar -> SL first
    excursion: Path  # prices the position experienced on this bar up to the exit
    alt_tp_raw_price: Decimal | None = None  # TP-first counterfactual for ambiguous bars


def resolve_exit(
    path: Path,
    side: Side,
    stop: Decimal,
    take_profit: Decimal | None,
    tick: Decimal,
    *,
    held_at_open: bool,
    market_exit_due: ExitReason | None,
    tp_liquidity: Liquidity,
    bar_low: Decimal | None = None,
    bar_high: Decimal | None = None,
) -> ExitFill | None:
    """Decide whether an open position exits on the given (remaining) path of one bar.

    `held_at_open`: the position existed before this bar, so ``path[0]`` is the bar open and the
    open-gap rules apply. `market_exit_due`: a time exit or exit signal executes at this open.

    `bar_low` / `bar_high` (same-bar entries only): the assumed path cannot prove that the bar's
    extreme beyond the stop came *before* an intrabar entry, so if the stop lies anywhere inside
    the bar's range it is treated as reachable after the entry (stop-loss, ``ambiguous_bar=True``
    when the remaining path alone would not have reached it). This only matters for stop entries;
    for limit entries the extreme always lies on the remaining path.
    """
    long = side is Side.LONG
    o = path[0]
    tp_level = None if take_profit is None else (take_profit + tick if long else take_profit - tick)
    if held_at_open:
        if _beyond(o, stop, down=long):
            return ExitFill(ExitReason.STOP_LOSS, o, Liquidity.TAKER, True, o != stop, False, (o,))
        if take_profit is not None and tp_level is not None and _beyond(o, tp_level, down=not long):
            return ExitFill(
                ExitReason.TAKE_PROFIT, take_profit, tp_liquidity, True, False, False, (o,)
            )
        if market_exit_due is not None:
            return ExitFill(market_exit_due, o, Liquidity.TAKER, True, False, False, (o,))
    sl_split = split_at(path, stop, down=long)
    tp_split = None if tp_level is None else split_at(path, tp_level, down=not long)
    sl_in_range = False
    if not held_at_open and sl_split is None:
        extreme = bar_low if long else bar_high
        sl_in_range = extreme is not None and _beyond(extreme, stop, down=long)
    if (sl_split is not None and tp_split is not None) or sl_in_range:
        return ExitFill(
            ExitReason.STOP_LOSS,
            stop,
            Liquidity.TAKER,
            False,
            False,
            True,
            (o, stop),  # SL-first: assume a direct move from the start to the stop
            alt_tp_raw_price=take_profit if tp_split is not None else None,
        )
    if sl_split is not None:
        return ExitFill(
            ExitReason.STOP_LOSS, stop, Liquidity.TAKER, False, False, False, sl_split[0]
        )
    if tp_split is not None and take_profit is not None:
        return ExitFill(
            ExitReason.TAKE_PROFIT, take_profit, tp_liquidity, False, False, False, tp_split[0]
        )
    return None
