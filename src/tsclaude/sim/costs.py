"""Trading costs: fees and slippage, every component reported separately (SIMULATION §3).

Slippage = half-spread + impact, both in basis points of the raw execution price, supplied by a
`SlippageEstimator`. The default `FixedSlippage` is a deterministic, deliberately pessimistic
placeholder (labeled `ESTIMATED`, `basis="pessimistic_default"`) until the live `book_top`
calibration exists. Slippage applies only to taker executions (market, stop, time exit, and
take-profit when `tp_as_taker=True`); maker executions fill at their limit price.

Rounding is always adverse to the book (CLAUDE.md money rules, SIMULATION §4):
- execution prices round to the tick **up for buys, down for sells**;
- buy notionals and fees round **up** to 1e-8; sell proceeds round **down** to 1e-8.

The fee defaults are Binance VIP-0 list rates as documented in the plan (spot 0.10 % / 0.10 %,
USD-M 0.02 % maker / 0.05 % taker). They are **unverified** against the current fee schedule; the
version string says so and the evaluation protocol freeze must confirm them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Protocol

from tsclaude.contracts.market import Market
from tsclaude.contracts.provenance import Provenance
from tsclaude.sim.money import BPS, ONE, SIM_CONTEXT, ZERO, ceil_to, dstr, floor_to, q8, q8_down, q8_up


class Liquidity(StrEnum):
    MAKER = "maker"
    TAKER = "taker"


class Direction(StrEnum):
    """Direction of a single execution (not of a position)."""

    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    maker: Decimal
    taker: Decimal

    def __post_init__(self) -> None:
        for rate in (self.maker, self.taker):
            if not isinstance(rate, Decimal) or not rate.is_finite() or rate < 0 or rate >= 1:
                raise ValueError(f"fee rate must be a Decimal in [0, 1), got {rate!r}")

    def rate(self, liquidity: Liquidity) -> Decimal:
        return self.maker if liquidity is Liquidity.MAKER else self.taker


DEFAULT_FEES: Mapping[Market, FeeSchedule] = {
    Market.SPOT: FeeSchedule(maker=Decimal("0.001"), taker=Decimal("0.001")),
    Market.USDM: FeeSchedule(maker=Decimal("0.0002"), taker=Decimal("0.0005")),
}
DEFAULT_COST_VERSION = "costs-v0-vip0-unverified"


@dataclass(frozen=True, slots=True)
class SlippageEstimate:
    half_spread_bps: Decimal
    impact_bps: Decimal
    provenance: Provenance = Provenance.ESTIMATED
    basis: str = "pessimistic_default"

    def __post_init__(self) -> None:
        for v in (self.half_spread_bps, self.impact_bps):
            if not isinstance(v, Decimal) or not v.is_finite() or v < 0:
                raise ValueError(f"slippage components must be non-negative Decimals, got {v!r}")

    @property
    def total_bps(self) -> Decimal:
        return self.half_spread_bps + self.impact_bps


class SlippageEstimator(Protocol):
    """Deterministic estimate for one execution.

    `notional_hint` is the quote notional at the raw price for a size computed *before* slippage
    (an upper bound for the final notional), so impact is never underestimated by the circularity
    size -> price -> size.
    """

    def estimate(
        self, market: Market, symbol: str, notional_hint: Decimal, bar_quote_volume: Decimal
    ) -> SlippageEstimate: ...


@dataclass(frozen=True, slots=True)
class FixedSlippage:
    """Constant half-spread and impact in bps (default 2 + 1 bps per side, pessimistic for majors)."""

    half_spread_bps: Decimal = Decimal("2")
    impact_bps: Decimal = Decimal("1")

    def estimate(
        self, market: Market, symbol: str, notional_hint: Decimal, bar_quote_volume: Decimal
    ) -> SlippageEstimate:
        return SlippageEstimate(self.half_spread_bps, self.impact_bps)


@dataclass(frozen=True, slots=True)
class ScaledSlippage:
    """Wraps an estimator and multiplies both components (cost stress tests x1.5, x2)."""

    inner: SlippageEstimator
    factor: Decimal

    def estimate(
        self, market: Market, symbol: str, notional_hint: Decimal, bar_quote_volume: Decimal
    ) -> SlippageEstimate:
        base = self.inner.estimate(market, symbol, notional_hint, bar_quote_volume)
        return SlippageEstimate(
            base.half_spread_bps * self.factor,
            base.impact_bps * self.factor,
            base.provenance,
            f"{base.basis}*{self.factor}",
        )


@dataclass(frozen=True, slots=True)
class PricedExecution:
    """Execution price and slippage decomposition for one fill (amounts per unit, not totals)."""

    direction: Direction
    liquidity: Liquidity
    raw_price: Decimal  # open / stop / limit price before slippage
    price: Decimal  # tick-rounded execution price
    slippage: SlippageEstimate | None  # None for maker fills


@dataclass(frozen=True, slots=True)
class FillCosts:
    """Every cost component of one fill, in the quote asset, quantized to 1e-8."""

    notional: Decimal  # qty x price, rounded adversely
    fee: Decimal
    fee_rate: Decimal
    liquidity: Liquidity
    half_spread_cost: Decimal
    impact_cost: Decimal
    tick_rounding_cost: Decimal  # adverse rounding of the slipped price to the tick
    slippage_basis: str | None

    @property
    def slippage_cost(self) -> Decimal:
        return self.half_spread_cost + self.impact_cost + self.tick_rounding_cost

    def to_payload(self) -> dict[str, str | None]:
        return {
            "notional": dstr(self.notional),
            "fee": dstr(self.fee),
            "fee_rate": dstr(self.fee_rate),
            "liquidity": str(self.liquidity),
            "half_spread_cost": dstr(self.half_spread_cost),
            "impact_cost": dstr(self.impact_cost),
            "tick_rounding_cost": dstr(self.tick_rounding_cost),
            "slippage_basis": self.slippage_basis,
        }


@dataclass(frozen=True)
class CostModel:
    fees: Mapping[Market, FeeSchedule] = field(default_factory=lambda: dict(DEFAULT_FEES))
    slippage: SlippageEstimator = field(default_factory=FixedSlippage)
    tp_as_taker: bool = False  # robustness runs model take-profits as taker fills
    version: str = DEFAULT_COST_VERSION

    def fee_rate(self, market: Market, liquidity: Liquidity) -> Decimal:
        schedule = self.fees.get(market)
        if schedule is None:
            raise KeyError(f"no fee schedule for market {market}")  # fail closed
        return schedule.rate(liquidity)

    def scaled(self, factor: Decimal) -> CostModel:
        """Cost stress variant: fees and slippage multiplied by `factor` (e.g. 1.5, 2)."""
        if factor <= 0:
            raise ValueError("stress factor must be positive")
        fees = {
            m: FeeSchedule(maker=s.maker * factor, taker=s.taker * factor)
            for m, s in self.fees.items()
        }
        return CostModel(
            fees=fees,
            slippage=ScaledSlippage(self.slippage, factor),
            tp_as_taker=self.tp_as_taker,
            version=f"{self.version}*{factor}",
        )

    def describe(self) -> dict[str, object]:
        return {
            "version": self.version,
            "tp_as_taker": self.tp_as_taker,
            "fees": {
                str(m): {"maker": dstr(s.maker), "taker": dstr(s.taker)}
                for m, s in sorted(self.fees.items(), key=lambda kv: str(kv[0]))
            },
            "slippage": type(self.slippage).__name__,
        }

    def price(
        self,
        *,
        market: Market,
        symbol: str,
        direction: Direction,
        liquidity: Liquidity,
        raw_price: Decimal,
        tick_size: Decimal,
        notional_hint: Decimal,
        bar_quote_volume: Decimal,
    ) -> PricedExecution:
        """Execution price: maker fills at the raw (limit) price; taker fills pay slippage."""
        with localcontext(SIM_CONTEXT):
            if liquidity is Liquidity.MAKER:
                return PricedExecution(direction, liquidity, raw_price, raw_price, None)
            est = self.slippage.estimate(market, symbol, notional_hint, bar_quote_volume)
            frac = est.total_bps / BPS
            if direction is Direction.BUY:
                price = ceil_to(raw_price * (ONE + frac), tick_size)
            else:
                price = floor_to(raw_price * (ONE - frac), tick_size)
            if price <= 0:
                raise ValueError("slippage drove the execution price to zero")
            return PricedExecution(direction, liquidity, raw_price, price, est)

    def fill_costs(self, market: Market, execution: PricedExecution, qty: Decimal) -> FillCosts:
        with localcontext(SIM_CONTEXT):
            exact = execution.price * qty
            buy = execution.direction is Direction.BUY
            notional = q8_up(exact) if buy else q8_down(exact)
            rate = self.fee_rate(market, execution.liquidity)
            fee = q8_up(notional * rate)
            est = execution.slippage
            if est is None:
                spread = impact = rounding = ZERO
                basis = None
            else:
                raw_notional = execution.raw_price * qty
                spread = q8(raw_notional * est.half_spread_bps / BPS)
                impact = q8(raw_notional * est.impact_bps / BPS)
                total = q8(abs(execution.price - execution.raw_price) * qty)
                rounding = total - spread - impact
                basis = est.basis
            return FillCosts(
                notional=notional,
                fee=fee,
                fee_rate=rate,
                liquidity=execution.liquidity,
                half_spread_cost=spread,
                impact_cost=impact,
                tick_rounding_cost=rounding,
                slippage_basis=basis,
            )
