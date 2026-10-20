"""Books: sizing, positions, bracket exits, trade records and equity (SIMULATION §4-§5).

A `Book` owns one `Ledger` and processes closed 1m bars handed to it by the `Simulator`. Per bar,
for the bar's (market, symbol): **exits first** (positions held from before the bar), **then
entries** for orders whose earliest execution time has come, then mark-to-market at the bar close.

Sizing (fixed-fractional, always rounded toward smaller size)::

    qty_risk = risk_per_trade x equity / |entry_fill_price - stop|
    qty      = min(qty_risk, max_notional / price, cash / (price x (1 + fee_rate)))
    qty      = round_down(qty, lot_size); reject if qty == 0 or notional < min_notional

``equity`` is the mark-to-market equity known at the bar open (cash after this bar's exits plus open
positions at their last close), so sizing never uses the bar it executes on. ``max_notional`` =
``min(max_notional_fraction x equity, max_notional_abs)``; the default fraction 1 is the 1x notional
cap. Shorts require a USD-M instrument (perpetual) and lock 1x notional as margin.

R-multiple: ``1R = qty x |entry_fill_price - stop_price|`` (the planned risk at the actual entry
price). ``r_multiple = net_pnl / 1R`` where ``net_pnl`` includes slippage (embedded in fill prices),
entry and exit fees and funding. MAE/MFE are gross price excursions over the bars the position
experienced, in R, both reported as non-negative numbers.
In the fill bar of an intrabar entry (ordering unknown, L-04) MAE counts the bar's adverse extreme
and MFE only the close; a take-profit's excursion ends at the take-profit price.

Trades are flagged ``ambiguous_bar`` (SL-first resolution or a fill-bar stop-out) and
``spans_gap`` (the position lived through missing 1m bars); both are counted in `summary`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Any

from tsclaude.contracts.market import Bar, Market
from tsclaude.contracts.signals import Side
from tsclaude.contracts.timeutil import MS_PER_MINUTE
from tsclaude.sim.costs import CostModel, FillCosts, Liquidity, PricedExecution
from tsclaude.sim.fills import (
    BarPrices,
    ExitFill,
    ExitReason,
    resolve_entry,
    resolve_exit,
    resolve_fill_bar_exit,
)
from tsclaude.sim.ledger import (
    CAPITAL,
    CASH,
    FEES,
    FUNDING,
    REALIZED_PNL,
    Ledger,
    margin_account,
    position_account,
)
from tsclaude.sim.money import ONE, SIM_CONTEXT, ZERO, dstr, q8, q8_down, to_decimal
from tsclaude.sim.orders import (
    ENTRY_ORDER_TYPE,
    EntryRequest,
    InstrumentSpec,
    Order,
    OrderRole,
    OrderStatus,
    OrderType,
    entry_direction,
    exit_direction,
    side_sign,
)

type SymbolKey = tuple[Market, str]
type Payload = dict[str, Any]


class RejectReason(StrEnum):
    """Stable reason codes on ORDER_REJECTED events at fill time (reports count them)."""

    STOP_BREACHED_AT_ENTRY = "stop_breached_at_entry"  # execution price already beyond the stop
    TAKE_PROFIT_BREACHED_AT_ENTRY = "take_profit_breached_at_entry"
    BELOW_MIN_NOTIONAL = "below_min_notional"
    NON_POSITIVE_EQUITY = "non_positive_equity"
    SIZE_ZERO = "size_zero"  # suffixed with the binding constraint, e.g. size_zero:cash


class SimEventType(StrEnum):
    """Simulator event types (contract change requested: add to `contracts.events.EventType`)."""

    RUN_STARTED = "sim_run_started"
    RUN_COMPLETED = "sim_run_completed"
    BAR_GAP = "sim_bar_gap"
    REQUEST_REJECTED = "sim_request_rejected"
    ORDER_SUBMITTED = "sim_order_submitted"
    ORDER_FILLED = "sim_order_filled"
    ORDER_CANCELLED = "sim_order_cancelled"
    ORDER_EXPIRED = "sim_order_expired"
    ORDER_REJECTED = "sim_order_rejected"
    POSITION_OPENED = "sim_position_opened"
    POSITION_CLOSED = "sim_position_closed"
    EXIT_REQUESTED = "sim_exit_requested"
    FUNDING_APPLIED = "sim_funding_applied"
    LEDGER_POSTED = "sim_ledger_posted"
    SOURCE_ERROR = "sim_source_error"


# ---------------------------------------------------------------- risk hook
@dataclass(frozen=True, slots=True)
class RiskCheck:
    """Input to the risk hook.

    ``stage="submit"``: order acceptance; ``ts_ms`` is the decision time (the bar close the signal
    was computed from) and ``equity`` the mark-to-market equity at that close.
    ``stage="fill"``: just before execution; ``ts_ms`` is the **execution time**, the open of the
    bar the order executes on (for intrabar fills the earliest possible fill time), and ``equity``
    the sizing equity at the previous 1m close. The hook may only use data with
    ``close_time_ms < ts_ms`` -- i.e. up to the previous close -- never the executing bar.
    """

    stage: str  # "submit" or "fill"
    book: str
    ts_ms: int
    request: EntryRequest
    equity: Decimal
    open_positions: int


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allow: bool
    reason: str = ""


type RiskHook = Callable[[RiskCheck], RiskDecision]


def allow_all(check: RiskCheck) -> RiskDecision:
    """Stub risk hook (the deterministic risk engine arrives in M3)."""
    return RiskDecision(True, "stub_allow")


# ---------------------------------------------------------------- config and records
@dataclass(frozen=True, slots=True)
class BookConfig:
    risk_per_trade: Decimal = Decimal("0.005")
    max_notional_fraction: Decimal = Decimal("1")  # 1x notional/equity cap
    max_notional_abs: Decimal | None = None

    def __post_init__(self) -> None:
        if not ZERO < self.risk_per_trade < ONE:
            raise ValueError("risk_per_trade must be in (0, 1)")
        if not ZERO < self.max_notional_fraction <= ONE:
            raise ValueError("max_notional_fraction must be in (0, 1] (no leverage in MVP)")
        if self.max_notional_abs is not None and self.max_notional_abs <= 0:
            raise ValueError("max_notional_abs must be positive")


@dataclass(frozen=True, slots=True)
class TradeRecord:
    trade_id: str
    book: str
    market: Market
    symbol: str
    side: Side
    request_id: str
    signal_id: str | None
    entry_bar_open_ms: int
    entry_at_open: bool
    entry_price: Decimal
    entry_liquidity: Liquidity
    qty: Decimal
    stop_price: Decimal
    take_profit_price: Decimal | None
    exit_bar_open_ms: int
    exit_at_open: bool
    exit_price: Decimal
    exit_liquidity: Liquidity
    exit_reason: ExitReason
    gap_fill: bool
    ambiguous_bar: bool
    spans_gap: bool  # the position lived through missing 1m bars (L-04)
    gross_pnl: Decimal  # price P&L incl. slippage, before fees and funding
    fees: Decimal
    funding: Decimal  # received (+) / paid (-)
    slippage_cost: Decimal
    net_pnl: Decimal
    risk_amount: Decimal
    r_multiple: Decimal
    mae_r: Decimal
    mfe_r: Decimal
    alt_r_if_tp_first: Decimal | None  # ambiguous bars: R if resolved TP-first

    def to_payload(self) -> Payload:
        def d(v: Decimal | None) -> str | None:
            return None if v is None else dstr(v)

        return {
            "trade_id": self.trade_id,
            "book": self.book,
            "market": str(self.market),
            "symbol": self.symbol,
            "side": str(self.side),
            "request_id": self.request_id,
            "signal_id": self.signal_id,
            "entry_bar_open_ms": self.entry_bar_open_ms,
            "entry_at_open": self.entry_at_open,
            "entry_price": dstr(self.entry_price),
            "entry_liquidity": str(self.entry_liquidity),
            "qty": dstr(self.qty),
            "stop_price": dstr(self.stop_price),
            "take_profit_price": d(self.take_profit_price),
            "exit_bar_open_ms": self.exit_bar_open_ms,
            "exit_at_open": self.exit_at_open,
            "exit_price": dstr(self.exit_price),
            "exit_liquidity": str(self.exit_liquidity),
            "exit_reason": str(self.exit_reason),
            "gap_fill": self.gap_fill,
            "ambiguous_bar": self.ambiguous_bar,
            "spans_gap": self.spans_gap,
            "gross_pnl": dstr(self.gross_pnl),
            "fees": dstr(self.fees),
            "funding": dstr(self.funding),
            "slippage_cost": dstr(self.slippage_cost),
            "net_pnl": dstr(self.net_pnl),
            "risk_amount": dstr(self.risk_amount),
            "r_multiple": dstr(self.r_multiple),
            "mae_r": dstr(self.mae_r),
            "mfe_r": dstr(self.mfe_r),
            "alt_r_if_tp_first": d(self.alt_r_if_tp_first),
        }


@dataclass(frozen=True, slots=True)
class EquityPoint:
    ts_ms: int
    equity: Decimal


@dataclass(frozen=True, slots=True)
class DrawdownStats:
    max_drawdown: Decimal  # fraction of peak, non-negative (0.1 = -10 %)
    peak_ts_ms: int | None
    trough_ts_ms: int | None
    longest_duration_ms: int  # longest time spent below a previous peak


def drawdown_series(points: Sequence[EquityPoint]) -> list[Decimal]:
    """Drawdown at each point as a non-negative fraction of the running peak, quantized to 1e-8."""
    out: list[Decimal] = []
    peak: Decimal | None = None
    with localcontext(SIM_CONTEXT):
        for p in points:
            peak = p.equity if peak is None or p.equity > peak else peak
            out.append(q8((peak - p.equity) / peak) if peak > 0 else ZERO)
    return out


def drawdown_stats(points: Sequence[EquityPoint]) -> DrawdownStats:
    dd = drawdown_series(points)
    max_dd = q8(ZERO)
    peak_ts: int | None = None
    trough_ts: int | None = None
    running_peak_ts: int | None = None
    peak: Decimal | None = None
    under_since: int | None = None
    longest = 0
    for p, d in zip(points, dd, strict=True):
        if peak is None or p.equity >= peak:
            peak = p.equity
            running_peak_ts = p.ts_ms
            if under_since is not None:
                longest = max(longest, p.ts_ms - under_since)
                under_since = None
        elif under_since is None:
            under_since = running_peak_ts
        if d > max_dd:
            max_dd, peak_ts, trough_ts = d, running_peak_ts, p.ts_ms
    if under_since is not None and points:
        longest = max(longest, points[-1].ts_ms - under_since)
    return DrawdownStats(max_dd, peak_ts, trough_ts, longest)


def funding_cash_flow(rate: Decimal, side: Side, qty: Decimal, mark_price: Decimal) -> Decimal:
    """Signed funding cash flow for the book (+ received, - paid), quantized to 1e-8.

    ``amount = -side_sign x rate x qty x mark_price``: with a positive rate longs pay and shorts
    receive; with a negative rate the reverse. Rounded toward -infinity, i.e. adverse to the book
    (payments round up, receipts round down).
    """
    if qty <= 0 or mark_price <= 0:
        raise ValueError("funding needs a positive quantity and mark price")
    with localcontext(SIM_CONTEXT):
        return q8_down(-side_sign(side) * rate * qty * mark_price)


# ---------------------------------------------------------------- internal state
@dataclass(slots=True)
class _PendingEntry:
    order: Order
    request: EntryRequest
    stop: Decimal
    take_profit: Decimal | None


@dataclass(slots=True)
class _Position:
    spec: InstrumentSpec
    side: Side
    request: EntryRequest
    qty: Decimal
    entry_price: Decimal
    entry_notional: Decimal
    entry_liquidity: Liquidity
    entry_fee: Decimal
    entry_slippage: Decimal
    entry_bar_open_ms: int
    entry_at_open: bool
    stop: Decimal
    take_profit: Decimal | None
    sl_order_id: str
    tp_order_id: str | None
    time_exit_due_ms: int | None
    #: held strictly after this time: the bar open for at-open fills, the bar close for intrabar
    #: fills (the fill time inside the bar is unknown)
    entry_time_ms: int = 0
    exit_due_ms: int | None = None  # exit signal / invalidation: market at this open or later
    funding: Decimal = ZERO
    last_funding_ms: int | None = None
    spans_gap: bool = False
    best: Decimal = ZERO
    worst: Decimal = ZERO
    last_mark: Decimal = ZERO

    @property
    def risk_amount(self) -> Decimal:
        return q8(self.qty * abs(self.entry_price - self.stop))

    def observe(self, prices: Sequence[Decimal]) -> None:
        for p in prices:
            self.best = max(self.best, p) if self.side is Side.LONG else min(self.best, p)
            self.worst = min(self.worst, p) if self.side is Side.LONG else max(self.worst, p)


# ---------------------------------------------------------------- the book
class Book:
    def __init__(
        self,
        name: str,
        starting_equity: Decimal,
        cost_model: CostModel,
        risk_hook: RiskHook = allow_all,
        *,
        instruments: Sequence[InstrumentSpec],
        config: BookConfig | None = None,
    ) -> None:
        if not name:
            raise ValueError("book name is required")
        if not isinstance(starting_equity, Decimal) or starting_equity <= 0:
            raise ValueError("starting_equity must be a positive Decimal")
        self.name = name
        self.cost_model = cost_model
        self.risk_hook = risk_hook
        self.config = config or BookConfig()
        self.instruments: dict[SymbolKey, InstrumentSpec] = {}
        for spec in instruments:
            if spec.key in self.instruments:
                raise ValueError(f"duplicate instrument {spec.key}")
            self.instruments[spec.key] = spec
        self.starting_equity = q8(starting_equity)
        self.ledger = Ledger()
        self.trades: list[TradeRecord] = []
        self.equity_curve: list[EquityPoint] = []
        self._pending: dict[SymbolKey, _PendingEntry] = {}
        self._positions: dict[SymbolKey, _Position] = {}
        self._seen_requests: set[str] = set()
        self._rejections: dict[str, int] = {}  # fill-time rejection counts by reason
        self._last_open: dict[SymbolKey, int] = {}  # last bar open seen per instrument (gaps)
        self._now_ms = 0  # end of the latest bar processed (latest close + 1 ms)
        self._outbox: list[tuple[SimEventType, int, Payload]] = []
        self._opened = False
        # Causal sizing inputs: values as of the previous 1m close (see `sizing_basis`).
        self._equity_at_mark = self.starting_equity
        self._cash_at_mark = self.starting_equity

    # ------------------------------------------------------------ events
    def _emit(self, kind: SimEventType, ts_ms: int, payload: Payload) -> None:
        self._outbox.append((kind, ts_ms, {"book": self.name, **payload}))

    def drain(self) -> list[tuple[SimEventType, int, Payload]]:
        out, self._outbox = self._outbox, []
        return out

    def _post(self, ts_ms: int, memo: str, legs: list[tuple[str, Decimal]]) -> None:
        tx = self.ledger.post(ts_ms, memo, legs)
        self._emit(SimEventType.LEDGER_POSTED, ts_ms, tx.to_payload())

    def _post_fee(self, ts_ms: int, order_id: str, fee: Decimal) -> None:
        """Fee paid from cash; a zero fee (fee-free schedule) posts nothing."""
        if fee != 0:
            self._post(ts_ms, f"fee:{order_id}", [(FEES, fee), (CASH, -fee)])

    def open_capital(self, ts_ms: int) -> None:
        if self._opened:
            return
        self._opened = True
        self._post(
            ts_ms, "capital", [(CASH, self.starting_equity), (CAPITAL, -self.starting_equity)]
        )
        # Origin of the equity curve: starting equity at the first bar open (drawdown baseline).
        self.equity_curve.append(EquityPoint(ts_ms, self.starting_equity))

    def describe(self) -> Payload:
        return {
            "book": self.name,
            "starting_equity": dstr(self.starting_equity),
            "costs": self.cost_model.describe(),
            "risk_per_trade": dstr(self.config.risk_per_trade),
            "max_notional_fraction": dstr(self.config.max_notional_fraction),
            "max_notional_abs": (
                None if self.config.max_notional_abs is None else dstr(self.config.max_notional_abs)
            ),
            "instruments": [
                s.to_payload()
                for _, s in sorted(
                    self.instruments.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])
                )
            ],
        }

    # ------------------------------------------------------------ views
    @property
    def cash(self) -> Decimal:
        return self.ledger.balance(CASH)

    @property
    def open_positions(self) -> int:
        return len(self._positions)

    def position_qty(self, market: Market, symbol: str) -> Decimal:
        pos = self._positions.get((market, symbol))
        return ZERO if pos is None else side_sign(pos.side) * pos.qty

    def has_pending(self, market: Market, symbol: str) -> bool:
        return (market, symbol) in self._pending

    def _position_value(self, pos: _Position) -> Decimal:
        """Asset value at the last mark: spot inventory, or margin plus unrealized P&L."""
        marked = pos.qty * pos.last_mark
        if pos.spec.market is Market.SPOT:
            return marked
        if pos.side is Side.LONG:
            return marked  # margin + (marked - entry_notional)
        return 2 * pos.entry_notional - marked

    def equity(self) -> Decimal:
        """Mark-to-market equity at the last marks, quantized to 1e-8."""
        with localcontext(SIM_CONTEXT):
            total = self.cash + sum(
                (self._position_value(p) for p in self._positions.values()), ZERO
            )
            return q8(total)

    # ------------------------------------------------------------ requests
    def _reject_request(self, req: EntryRequest, ts_ms: int, reason: str) -> None:
        self._emit(
            SimEventType.REQUEST_REJECTED, ts_ms, {"request_id": req.request_id, "reason": reason}
        )

    def submit(self, req: EntryRequest, decision_time_ms: int, earliest_exec_ms: int) -> bool:
        """Accept an entry request as a pending order, or reject it (fail closed)."""
        key = (req.market, req.symbol)
        ts = decision_time_ms
        if req.request_id in self._seen_requests:
            self._reject_request(req, ts, "duplicate_request_id")
            return False
        self._seen_requests.add(req.request_id)
        spec = self.instruments.get(key)
        if spec is None:
            self._reject_request(req, ts, "unknown_instrument")
            return False
        if req.side is Side.SHORT and req.market is not Market.USDM:
            self._reject_request(req, ts, "short_requires_usdm")
            return False
        if key in self._positions or key in self._pending:
            self._reject_request(req, ts, "symbol_busy")
            return False
        if req.valid_until_ms < earliest_exec_ms:
            self._reject_request(req, ts, "validity_ends_before_execution")
            return False
        if not self._risk_allows("submit", req, ts):
            return False
        enter = entry_direction(req.side)
        leave = exit_direction(req.side)
        try:
            price = None if req.entry_price is None else spec.round_price(req.entry_price, enter)
            stop = spec.round_price(req.stop_price, leave)
            tp = (
                None
                if req.take_profit_price is None
                else spec.round_price(req.take_profit_price, leave)
            )
        except ValueError as exc:
            self._reject_request(req, ts, f"rounding_failed:{exc}")
            return False
        if price is not None:
            sign = side_sign(req.side)
            if sign * (price - stop) <= 0 or (tp is not None and sign * (tp - price) <= 0):
                self._reject_request(req, ts, "bracket_invalid_after_rounding")
                return False
        order = Order(
            order_id=f"{self.name}:{req.request_id}:entry",
            book=self.name,
            market=req.market,
            symbol=req.symbol,
            role=OrderRole.ENTRY,
            type=ENTRY_ORDER_TYPE[req.kind],
            direction=enter,
            price=price,
            decision_time_ms=decision_time_ms,
            earliest_exec_ms=earliest_exec_ms,
            valid_until_ms=req.valid_until_ms,
            request=req,
        )
        self._pending[key] = _PendingEntry(order, req, stop, tp)
        self._emit(
            SimEventType.ORDER_SUBMITTED,
            ts,
            {
                **order.to_payload(),
                "request": req.to_payload(),
                "stop_price": dstr(stop),
                "take_profit_price": None if tp is None else dstr(tp),
            },
        )
        return True

    def request_exit(
        self,
        market: Market,
        symbol: str,
        request_id: str,
        decision_time_ms: int,
        earliest_exec_ms: int,
    ) -> None:
        """Exit signal / invalidation: market exit at the next eligible open, or cancel entry."""
        key = (market, symbol)
        pos = self._positions.get(key)
        pending = self._pending.get(key)
        payload: Payload = {"request_id": request_id, "market": str(market), "symbol": symbol}
        if pos is not None:
            pos.exit_due_ms = (
                earliest_exec_ms
                if pos.exit_due_ms is None
                else min(pos.exit_due_ms, earliest_exec_ms)
            )
            self._emit(
                SimEventType.EXIT_REQUESTED,
                decision_time_ms,
                {**payload, "action": "market_exit", "exit_due_ms": pos.exit_due_ms},
            )
        elif pending is not None:
            self._finish_pending(key, OrderStatus.CANCELLED, decision_time_ms, "exit_signal")
        else:
            self._emit(SimEventType.EXIT_REQUESTED, decision_time_ms, {**payload, "action": "none"})

    def _risk_allows(
        self, stage: str, req: EntryRequest, ts: int, event_ts: int | None = None
    ) -> bool:
        """Ask the risk hook; `ts` goes to the hook, `event_ts` (default `ts`) stamps rejections."""
        equity = self.equity() if stage == "submit" else self.sizing_basis()[0]
        check = RiskCheck(stage, self.name, ts, req, equity, len(self._positions))
        ts = ts if event_ts is None else event_ts
        try:
            decision = self.risk_hook(check)
            allowed = isinstance(decision, RiskDecision) and decision.allow is True
            reason = decision.reason if isinstance(decision, RiskDecision) else "invalid_decision"
        except Exception as exc:  # fail closed: any error blocks the entry
            allowed, reason = False, f"risk_error:{type(exc).__name__}"
        if not allowed:
            if stage == "submit":
                self._reject_request(req, ts, f"risk_blocked:{reason}")
            else:
                key = (req.market, req.symbol)
                self._finish_pending(key, OrderStatus.REJECTED, ts, f"risk_blocked:{reason}")
        return allowed

    def _finish_pending(self, key: SymbolKey, status: OrderStatus, ts: int, reason: str) -> None:
        pending = self._pending.pop(key)
        pending.order.status = status
        kind = {
            OrderStatus.CANCELLED: SimEventType.ORDER_CANCELLED,
            OrderStatus.EXPIRED: SimEventType.ORDER_EXPIRED,
            OrderStatus.REJECTED: SimEventType.ORDER_REJECTED,
        }[status]
        reason = str(reason)
        if status is OrderStatus.REJECTED:
            self._rejections[reason] = self._rejections.get(reason, 0) + 1
        self._emit(kind, ts, {"order_id": pending.order.order_id, "reason": reason})

    # ------------------------------------------------------------ bar processing
    def on_bar(self, bar: Bar) -> None:
        key = (bar.market, bar.symbol)
        self._now_ms = max(self._now_ms, bar.close_time_ms + 1)
        spec = self.instruments.get(key)
        if spec is not None:
            prev = self._last_open.get(key)
            self._last_open[key] = bar.open_time_ms
            held = self._positions.get(key)
            if held is not None and prev is not None and bar.open_time_ms - prev > MS_PER_MINUTE:
                held.spans_gap = True  # L-04: the position lived through missing 1m bars
            prices = BarPrices.from_bar(bar)
            quote_volume = to_decimal(bar.quote_volume)
            with localcontext(SIM_CONTEXT):
                pos = self._positions.get(key)
                if pos is not None:
                    self._process_held(pos, prices, quote_volume)
                pending = self._pending.get(key)
                if pending is not None:
                    self._process_entry(pending, spec, prices, quote_volume)
                pos = self._positions.get(key)
                if pos is not None:
                    pos.last_mark = prices.close

    def mark(self, ts_ms: int) -> None:
        """Record mark-to-market equity at a 1m close (called once per minute by the engine)."""
        equity = self.equity()
        self.equity_curve.append(EquityPoint(ts_ms, equity))
        self._equity_at_mark = equity
        self._cash_at_mark = self.cash

    def sizing_basis(self) -> tuple[Decimal, Decimal]:
        """(equity, available cash) an entry may be sized with.

        Equity is the mark-to-market equity at the previous 1m close, so sizing never sees a close
        (of this or any other symbol) later than the bar open it executes at. Available cash is the
        smaller of the cash at that close and the cash now: same-minute proceeds of other symbols'
        exits are not spendable yet, same-minute spending already is subtracted.
        """
        return self._equity_at_mark, min(self._cash_at_mark, self.cash)

    def _process_held(self, pos: _Position, prices: BarPrices, quote_volume: Decimal) -> None:
        due: ExitReason | None = None
        if pos.exit_due_ms is not None and prices.open_time_ms >= pos.exit_due_ms:
            due = ExitReason.EXIT_SIGNAL
        elif pos.time_exit_due_ms is not None and prices.open_time_ms >= pos.time_exit_due_ms:
            due = ExitReason.TIME_EXIT
        fill = resolve_exit(
            prices.path(),
            pos.side,
            pos.stop,
            pos.take_profit,
            pos.spec.tick_size,
            held_at_open=True,
            market_exit_due=due,
            tp_liquidity=self._tp_liquidity(),
        )
        if fill is None:
            pos.observe((prices.high, prices.low))
            return
        pos.observe(fill.excursion)
        self._close(pos, fill, prices, quote_volume)

    def _tp_liquidity(self) -> Liquidity:
        return Liquidity.TAKER if self.cost_model.tp_as_taker else Liquidity.MAKER

    def _process_entry(
        self, pending: _PendingEntry, spec: InstrumentSpec, prices: BarPrices, quote_volume: Decimal
    ) -> None:
        order = pending.order
        key = spec.key
        if prices.open_time_ms < order.earliest_exec_ms:
            return  # not yet eligible (no execution before the next bar open after the decision)
        valid_until = order.valid_until_ms
        if valid_until is not None and prices.open_time_ms > valid_until:
            self._finish_pending(
                key, OrderStatus.EXPIRED, prices.close_time_ms, "valid_until_passed"
            )
            return
        open_ok = valid_until is None or prices.open_time_ms <= valid_until
        intrabar_ok = valid_until is None or prices.close_time_ms <= valid_until
        fill = resolve_entry(
            prices,
            order.type,
            order.direction,
            order.price,
            spec.tick_size,
            arrival=order.bars_seen == 0,
            open_ok=open_ok,
            intrabar_ok=intrabar_ok,
        )
        order.bars_seen += 1
        if fill is not None:
            self._open(
                pending,
                spec,
                prices,
                quote_volume,
                fill.raw_price,
                fill.liquidity,
                fill.at_open,
                fill.remaining,
                fill.cap,
            )
        elif valid_until is not None and prices.close_time_ms >= valid_until:
            self._finish_pending(
                key, OrderStatus.EXPIRED, prices.close_time_ms, "valid_until_passed"
            )

    # ------------------------------------------------------------ sizing
    def _size(
        self,
        spec: InstrumentSpec,
        execution: PricedExecution,
        stop: Decimal,
        equity: Decimal,
        cash: Decimal,
    ) -> tuple[Decimal, str]:
        price = execution.price
        distance = abs(price - stop)
        rate = self.cost_model.fee_rate(spec.market, execution.liquidity)
        cap_notional = self.config.max_notional_fraction * equity
        if self.config.max_notional_abs is not None:
            cap_notional = min(cap_notional, self.config.max_notional_abs)
        candidates = {
            "risk": self.config.risk_per_trade * equity / distance,
            "max_notional": cap_notional / price,
            "cash": max(cash, ZERO) / (price * (ONE + rate)),
        }
        binding = min(candidates, key=lambda k: (candidates[k], k))
        qty = spec.round_qty(candidates[binding])
        # Quantized notional + fee must fit the cash; step down one lot while it does not.
        while qty > 0:
            costs = self.cost_model.fill_costs(spec.market, execution, qty)
            if costs.notional + costs.fee <= cash and costs.notional <= cap_notional:
                break
            qty -= spec.lot_size
        return max(qty, ZERO), binding

    def _hint_qty(
        self, spec: InstrumentSpec, raw: Decimal, stop: Decimal, equity: Decimal
    ) -> Decimal:
        distance = abs(raw - stop)
        if distance == 0:
            return ZERO
        cap = self.config.max_notional_fraction * equity
        if self.config.max_notional_abs is not None:
            cap = min(cap, self.config.max_notional_abs)
        return min(self.config.risk_per_trade * equity / distance, cap / raw)

    def _open(
        self,
        pending: _PendingEntry,
        spec: InstrumentSpec,
        prices: BarPrices,
        quote_volume: Decimal,
        raw_price: Decimal,
        liquidity: Liquidity,
        at_open: bool,
        remaining: tuple[Decimal, ...],
        cap: Decimal | None,
    ) -> None:
        req = pending.request
        key = spec.key
        ts = prices.close_time_ms
        if not self._risk_allows("fill", req, prices.open_time_ms, event_ts=ts):
            return
        equity, cash = self.sizing_basis()
        if equity <= 0:
            self._finish_pending(key, OrderStatus.REJECTED, ts, RejectReason.NON_POSITIVE_EQUITY)
            return
        hint = self._hint_qty(spec, raw_price, pending.stop, equity)
        execution = self.cost_model.price(
            market=spec.market,
            symbol=spec.symbol,
            direction=pending.order.direction,
            liquidity=liquidity,
            raw_price=raw_price,
            tick_size=spec.tick_size,
            notional_hint=q8(hint * raw_price),
            bar_quote_volume=quote_volume,
            cap=cap,
        )
        sign = side_sign(req.side)
        if sign * (raw_price - pending.stop) <= 0 or sign * (execution.price - pending.stop) <= 0:
            self._finish_pending(key, OrderStatus.REJECTED, ts, RejectReason.STOP_BREACHED_AT_ENTRY)
            return
        if pending.take_profit is not None and sign * (pending.take_profit - execution.price) <= 0:
            self._finish_pending(
                key, OrderStatus.REJECTED, ts, RejectReason.TAKE_PROFIT_BREACHED_AT_ENTRY
            )
            return
        qty, binding = self._size(spec, execution, pending.stop, equity, cash)
        if qty <= 0:
            self._finish_pending(
                key, OrderStatus.REJECTED, ts, f"{RejectReason.SIZE_ZERO}:{binding}"
            )
            return
        costs = self.cost_model.fill_costs(spec.market, execution, qty)
        if costs.notional < spec.min_notional:
            self._finish_pending(key, OrderStatus.REJECTED, ts, RejectReason.BELOW_MIN_NOTIONAL)
            return
        del self._pending[key]
        order = pending.order
        order.status = OrderStatus.FILLED
        order.qty = qty
        asset = position_account(*key) if spec.market is Market.SPOT else margin_account(*key)
        self._post(
            ts, f"entry:{order.order_id}", [(asset, costs.notional), (CASH, -costs.notional)]
        )
        self._post_fee(ts, order.order_id, costs.fee)
        self._emit(
            SimEventType.ORDER_FILLED,
            ts,
            self._fill_payload(order, prices, at_open, execution, qty, costs, binding),
        )
        time_due = None
        if req.time_exit_bars is not None:
            step = req.strategy_interval.ms
            time_due = (prices.open_time_ms // step) * step + req.time_exit_bars * step
        pos = _Position(
            spec=spec,
            side=req.side,
            request=req,
            qty=qty,
            entry_price=execution.price,
            entry_notional=costs.notional,
            entry_liquidity=liquidity,
            entry_fee=costs.fee,
            entry_slippage=costs.slippage_cost,
            entry_bar_open_ms=prices.open_time_ms,
            entry_at_open=at_open,
            stop=pending.stop,
            take_profit=pending.take_profit,
            sl_order_id=f"{self.name}:{req.request_id}:sl",
            tp_order_id=None if pending.take_profit is None else f"{self.name}:{req.request_id}:tp",
            time_exit_due_ms=time_due,
            entry_time_ms=prices.open_time_ms if at_open else prices.close_time_ms,
            best=execution.price,
            worst=execution.price,
            last_mark=execution.price,
        )
        self._positions[key] = pos
        self._emit(
            SimEventType.POSITION_OPENED,
            ts,
            {
                "trade_id": f"{self.name}:{req.request_id}",
                "market": str(spec.market),
                "symbol": spec.symbol,
                "side": str(req.side),
                "qty": dstr(qty),
                "entry_price": dstr(execution.price),
                "risk_amount": dstr(pos.risk_amount),
                "sizing_equity": dstr(equity),
                "sizing_cash": dstr(cash),
                "sizing_binding": binding,
                "notional": dstr(costs.notional),
                "entry_fee": dstr(costs.fee),
                "time_exit_due_ms": time_due,
                "oco": [pos.sl_order_id] + ([] if pos.tp_order_id is None else [pos.tp_order_id]),
                "stop_price": dstr(pos.stop),
                "take_profit_price": None if pos.take_profit is None else dstr(pos.take_profit),
            },
        )
        # Same-bar rules (L-04). An entry filled at the open holds the position from the first trade
        # of the bar: normal rules, including open gaps (a resting limit filled at its price on a
        # bar that opened beyond the stop is stopped at that open). An entry filled inside the bar
        # may be stopped out in it (flagged ambiguous) but never books its take-profit in it.
        fill: ExitFill | None
        if at_open:
            fill = resolve_exit(
                remaining,
                pos.side,
                pos.stop,
                pos.take_profit,
                spec.tick_size,
                held_at_open=True,
                market_exit_due=None,
                tp_liquidity=self._tp_liquidity(),
            )
        else:
            fill = resolve_fill_bar_exit(pos.side, raw_price, pos.stop, prices.low, prices.high)
        if fill is None:
            if at_open:
                pos.observe(remaining)
            else:
                # Ordering inside the bar is unknown: count the bar's adverse extreme in MAE and
                # only the close in MFE (never a favourable extreme that may predate the fill).
                adverse = prices.low if pos.side is Side.LONG else prices.high
                pos.observe((adverse, prices.close))
            return
        pos.observe(fill.excursion)
        self._close(pos, fill, prices, quote_volume)

    def _fill_payload(
        self,
        order: Order,
        prices: BarPrices,
        at_open: bool,
        execution: PricedExecution,
        qty: Decimal,
        costs: FillCosts,
        binding: str | None,
    ) -> Payload:
        return {
            "order_id": order.order_id,
            "role": str(order.role),
            "bar_open_ms": prices.open_time_ms,
            "at_open": at_open,
            "direction": str(execution.direction),
            "raw_price": dstr(execution.raw_price),
            "price": dstr(execution.price),
            "qty": dstr(qty),
            "sizing_binding": binding,
            **costs.to_payload(),
        }

    # ------------------------------------------------------------ exits
    def _exit_execution(
        self, pos: _Position, raw: Decimal, liquidity: Liquidity, quote_volume: Decimal
    ) -> PricedExecution:
        return self.cost_model.price(
            market=pos.spec.market,
            symbol=pos.spec.symbol,
            direction=exit_direction(pos.side),
            liquidity=liquidity,
            raw_price=raw,
            tick_size=pos.spec.tick_size,
            notional_hint=q8(pos.qty * raw),
            bar_quote_volume=quote_volume,
        )

    def _gross(self, pos: _Position, exit_notional: Decimal) -> Decimal:
        return side_sign(pos.side) * (exit_notional - pos.entry_notional)

    def _close(
        self, pos: _Position, fill: ExitFill, prices: BarPrices, quote_volume: Decimal
    ) -> None:
        key = pos.spec.key
        ts = prices.close_time_ms
        execution = self._exit_execution(pos, fill.raw_price, fill.liquidity, quote_volume)
        costs = self.cost_model.fill_costs(pos.spec.market, execution, pos.qty)
        gross = self._gross(pos, costs.notional)
        exit_order_id = {
            ExitReason.STOP_LOSS: pos.sl_order_id,
            ExitReason.TAKE_PROFIT: pos.tp_order_id or pos.sl_order_id,
            ExitReason.TIME_EXIT: f"{self.name}:{pos.request.request_id}:time_exit",
            ExitReason.EXIT_SIGNAL: f"{self.name}:{pos.request.request_id}:exit",
        }[fill.reason]
        if pos.spec.market is Market.SPOT:
            basis = self.ledger.balance(position_account(*key))
            legs = [
                (CASH, costs.notional),
                (position_account(*key), -basis),
                (REALIZED_PNL, -(costs.notional - basis)),
            ]
        else:
            legs = [
                (CASH, pos.entry_notional + gross),
                (margin_account(*key), -pos.entry_notional),
                (REALIZED_PNL, -gross),
            ]
        self._post(ts, f"exit:{exit_order_id}", legs)
        self._post_fee(ts, exit_order_id, costs.fee)
        role = {
            ExitReason.STOP_LOSS: OrderRole.STOP_LOSS,
            ExitReason.TAKE_PROFIT: OrderRole.TAKE_PROFIT,
            ExitReason.TIME_EXIT: OrderRole.TIME_EXIT,
            ExitReason.EXIT_SIGNAL: OrderRole.EXIT,
        }[fill.reason]
        exit_order = Order(
            exit_order_id,
            self.name,
            pos.spec.market,
            pos.spec.symbol,
            role,
            OrderType.STOP
            if role is OrderRole.STOP_LOSS
            else (OrderType.LIMIT if role is OrderRole.TAKE_PROFIT else OrderType.MARKET),
            exit_direction(pos.side),
            fill.raw_price,
            ts,
            prices.open_time_ms,
            qty=pos.qty,
            status=OrderStatus.FILLED,
        )
        payload = self._fill_payload(
            exit_order, prices, fill.at_open, execution, pos.qty, costs, None
        )
        payload.update({"gap_fill": fill.gap, "ambiguous_bar": fill.ambiguous})
        self._emit(SimEventType.ORDER_FILLED, ts, payload)
        for sibling in (pos.sl_order_id, pos.tp_order_id):
            if sibling is not None and sibling != exit_order_id:
                self._emit(SimEventType.ORDER_CANCELLED, ts, {"order_id": sibling, "reason": "oco"})
        fees = pos.entry_fee + costs.fee
        net = gross - fees + pos.funding
        risk = pos.risk_amount
        sign = side_sign(pos.side)
        alt_r = None
        if fill.ambiguous and fill.alt_tp_raw_price is not None:
            alt_exec = self._exit_execution(
                pos, fill.alt_tp_raw_price, self._tp_liquidity(), quote_volume
            )
            alt_costs = self.cost_model.fill_costs(pos.spec.market, alt_exec, pos.qty)
            alt_net = (
                self._gross(pos, alt_costs.notional) - pos.entry_fee - alt_costs.fee + pos.funding
            )
            alt_r = q8(alt_net / risk)
        trade = TradeRecord(
            trade_id=f"{self.name}:{pos.request.request_id}",
            book=self.name,
            market=pos.spec.market,
            symbol=pos.spec.symbol,
            side=pos.side,
            request_id=pos.request.request_id,
            signal_id=pos.request.signal_id,
            entry_bar_open_ms=pos.entry_bar_open_ms,
            entry_at_open=pos.entry_at_open,
            entry_price=pos.entry_price,
            entry_liquidity=pos.entry_liquidity,
            qty=pos.qty,
            stop_price=pos.stop,
            take_profit_price=pos.take_profit,
            exit_bar_open_ms=prices.open_time_ms,
            exit_at_open=fill.at_open,
            exit_price=execution.price,
            exit_liquidity=fill.liquidity,
            exit_reason=fill.reason,
            gap_fill=fill.gap,
            ambiguous_bar=fill.ambiguous,
            spans_gap=pos.spans_gap,
            gross_pnl=gross,
            fees=fees,
            funding=pos.funding,
            slippage_cost=pos.entry_slippage + costs.slippage_cost,
            net_pnl=net,
            risk_amount=risk,
            r_multiple=q8(net / risk),
            mae_r=q8(max(ZERO, sign * (pos.entry_price - pos.worst)) * pos.qty / risk),
            mfe_r=q8(max(ZERO, sign * (pos.best - pos.entry_price)) * pos.qty / risk),
            alt_r_if_tp_first=alt_r,
        )
        del self._positions[key]
        self.trades.append(trade)
        self._emit(SimEventType.POSITION_CLOSED, ts, trade.to_payload())

    # ------------------------------------------------------------ funding
    def apply_funding(self, market: Market, symbol: str, ts_ms: int, amount: Decimal) -> None:
        """Post a funding cash flow (+ received, - paid) for an open USD-M position.

        Fails (ValueError) unless the timestamp lies within the open position: strictly after the
        entry time (a position opened at a funding timestamp is not charged or credited for it),
        strictly after the previous funding of this position, and not in the future of the
        simulator (at most the end of the latest processed bar). Use `funding_cash_flow` (or
        `apply_funding_rate`) to derive the amount from a funding rate.
        """
        pos = self._positions.get((market, symbol))
        if pos is None or market is not Market.USDM:
            raise ValueError(f"no open USD-M position for {market}:{symbol}")
        if ts_ms <= pos.entry_time_ms:
            raise ValueError(f"funding at {ts_ms} does not follow the entry {pos.entry_time_ms}")
        if pos.last_funding_ms is not None and ts_ms <= pos.last_funding_ms:
            raise ValueError(f"funding at {ts_ms} not after previous funding {pos.last_funding_ms}")
        if ts_ms > self._now_ms:
            raise ValueError(
                f"funding at {ts_ms} is in the future of the simulator ({self._now_ms})"
            )
        pos.last_funding_ms = ts_ms
        amount = q8(amount)
        if amount == 0:
            return
        self._post(
            ts_ms,
            f"funding:{self.name}:{pos.request.request_id}",
            [(CASH, amount), (FUNDING, -amount)],
        )
        pos.funding += amount
        self._emit(
            SimEventType.FUNDING_APPLIED,
            ts_ms,
            {"market": str(market), "symbol": symbol, "amount": dstr(amount)},
        )

    def apply_funding_rate(
        self, market: Market, symbol: str, ts_ms: int, rate: Decimal, mark_price: Decimal
    ) -> Decimal:
        """Funding from a rate at a funding timestamp; returns the signed amount posted."""
        pos = self._positions.get((market, symbol))
        if pos is None:
            raise ValueError(f"no open USD-M position for {market}:{symbol}")
        amount = funding_cash_flow(rate, pos.side, pos.qty, mark_price)
        self.apply_funding(market, symbol, ts_ms, amount)
        return amount

    # ------------------------------------------------------------ end of run
    def finish(self, ts_ms: int) -> None:
        """Close out pending entries at the end of data (open positions stay open, marked).

        Entries whose validity ended by `ts_ms` are EXPIRED; the rest are CANCELLED (end_of_data).
        """
        for key in sorted(self._pending, key=lambda k: (str(k[0]), k[1])):
            valid_until = self._pending[key].order.valid_until_ms
            if valid_until is not None and valid_until <= ts_ms:
                self._finish_pending(key, OrderStatus.EXPIRED, ts_ms, "valid_until_passed")
            else:
                self._finish_pending(key, OrderStatus.CANCELLED, ts_ms, "end_of_data")

    def summary(self) -> Payload:
        dd = drawdown_stats(self.equity_curve)
        report = self.ledger.reconcile()
        return {
            "book": self.name,
            "equity": dstr(self.equity()),
            "cash": dstr(self.cash),
            "ledger_equity_at_cost": dstr(report.equity_from_entries),
            "ledger_ok": report.ok,
            "ledger_transactions": report.transactions,
            "trades": len(self.trades),
            "ambiguous_trades": sum(1 for t in self.trades if t.ambiguous_bar),
            "gap_trades": sum(1 for t in self.trades if t.spans_gap),
            "flagged_trades": sum(1 for t in self.trades if t.ambiguous_bar or t.spans_gap),
            "rejections_by_reason": dict(sorted(self._rejections.items())),
            "open_positions": len(self._positions),
            "max_drawdown": dstr(dd.max_drawdown),
            "max_drawdown_peak_ts_ms": dd.peak_ts_ms,
            "max_drawdown_trough_ts_ms": dd.trough_ts_ms,
            "longest_drawdown_ms": dd.longest_duration_ms,
            "equity_points": len(self.equity_curve),
        }

    def unrealized_pnl(self) -> Decimal:
        """Sum over open positions of side x (qty x last mark - entry notional), unquantized."""
        with localcontext(SIM_CONTEXT):
            return sum(
                (
                    side_sign(p.side) * (p.qty * p.last_mark - p.entry_notional)
                    for p in self._positions.values()
                ),
                ZERO,
            )

    def positions_snapshot(self) -> Mapping[SymbolKey, Decimal]:
        return {k: side_sign(p.side) * p.qty for k, p in self._positions.items()}


__all__ = [
    "Book",
    "BookConfig",
    "DrawdownStats",
    "EquityPoint",
    "RejectReason",
    "RiskCheck",
    "RiskDecision",
    "RiskHook",
    "SimEventType",
    "TradeRecord",
    "allow_all",
    "drawdown_series",
    "drawdown_stats",
    "funding_cash_flow",
]
