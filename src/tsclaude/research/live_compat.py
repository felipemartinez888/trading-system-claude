"""Can a research configuration ever trade in the live baseline book? (handoff I-1, decision D-E)

Backtests apply no risk gate (`sim.book.allow_all`); the live baseline book applies the risk engine,
whose reward/risk rule (`risk.engine.rule_reward_risk`) requires a cost-adjusted net reward/risk of
at least ``min_rr_net`` (1.5 in `configs/risk.yaml`). A bracket's gross reward/risk is its ``tp_r``
and costs only lower it, so a geometry with ``tp_r < min_rr_net`` is rejected live on **every**
trade, however good its backtest.

This module answers that statically, with the live engine's own function (`cost_adjusted_rr`) on
synthetic **long market** bracket orders with **modelled** slippage (no live spread or volume:
the engine uses the larger of the modelled slippage and half the live spread, so a wide spread can
still reject a trade judged COMPATIBLE here; shorts are not assessed), so research and live cannot
drift apart:

- ``INCOMPATIBLE``: no stop distance passes (``tp_r`` is at or below the floor's reach);
- ``COMPATIBLE``: every trade the strategy can confirm passes (already at the smallest stop its own
  cost filter allows);
- ``CONDITIONAL``: only trades with a stop at least ``min_stop_fraction`` from price pass;
- ``UNKNOWN``: the strategy declares no bracket geometry.

It changes no threshold and decides nothing: which side gives way is owner decision D-E. A
configuration that is not COMPATIBLE is never presented as deployable.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from tsclaude.contracts.market import Market
from tsclaude.contracts.signals import EntryRuleKind, Side
from tsclaude.risk.config import RiskConfig
from tsclaude.risk.engine import cost_adjusted_rr
from tsclaude.risk.market import MarketView, SymbolMarket
from tsclaude.sim.book import RiskCheck
from tsclaude.sim.costs import CostModel
from tsclaude.sim.orders import EntryRequest
from tsclaude.strategies.base import PlanningCosts, Strategy

INCOMPATIBLE = "INCOMPATIBLE"
COMPATIBLE = "COMPATIBLE"
CONDITIONAL = "CONDITIONAL"
UNKNOWN = "UNKNOWN"
_PRICE = Decimal("100")


@dataclass(frozen=True)
class LiveCompatibility:
    status: str
    min_rr_net: str
    tp_r: float | None
    rr_at_min_stop: str | None
    min_stop_fraction: float | None  # smallest stop distance (fraction of price) that passes
    strategy_min_stop_fraction: float | None  # smallest stop the strategy's cost filter allows
    reason: str

    @property
    def deployable(self) -> bool:
        return self.status == COMPATIBLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "deployable_in_baseline_book": self.deployable,
            "min_rr_net": self.min_rr_net,
            "tp_r": self.tp_r,
            "rr_at_min_stop": self.rr_at_min_stop,
            "min_stop_fraction": self.min_stop_fraction,
            "strategy_min_stop_fraction": self.strategy_min_stop_fraction,
            "reason": self.reason,
            "decision": "D-E (open): live risk floor vs research geometries",
        }


def net_rr(
    *, stop_fraction: float, tp_r: float, cost_model: CostModel, market: Market, symbol: str
) -> Decimal | None:
    """The live engine's cost-adjusted net reward/risk of a long market bracket with a stop
    ``stop_fraction`` below the reference price and a take-profit ``tp_r`` stop distances above."""
    d = Decimal(repr(stop_fraction))
    stop = _PRICE * (Decimal(1) - d)
    tp = _PRICE * (Decimal(1) + Decimal(repr(tp_r)) * d)
    if stop <= 0:
        return None
    req = EntryRequest(
        request_id="live-compat",
        market=market,
        symbol=symbol,
        side=Side.LONG,
        kind=EntryRuleKind.MARKET_NEXT_OPEN,
        stop_price=stop,
        take_profit_price=tp,
        valid_until_ms=1,
    )
    check = RiskCheck(
        stage="submit", book="baseline", ts_ms=0, request=req, equity=Decimal("10000"),
        open_positions=0,
    )  # fmt: skip
    sym = SymbolMarket(
        market=market, symbol=symbol, unhealthy_streams=(), stream_count=1, open_gap_minutes=0,
        last_bar_close_ms=0, last_close=_PRICE, last_quote_volume=None, spread_bps=None,
        spread_as_of_ms=None, median_spread_bps=None, spread_samples=0,
    )  # fmt: skip
    view = MarketView(as_of_ms=0, clock_offset_ms=0, symbols={(market, symbol): sym})
    rr, _ = cost_adjusted_rr(check, view, cost_model)
    return rr


def assess(
    strategy: Strategy[Any],
    *,
    risk: RiskConfig,
    cost_model: CostModel,
    market: Market,
    symbol: str,
) -> LiveCompatibility:
    m = risk.min_rr_net
    geometry = getattr(strategy, "bracket_geometry", None)
    if geometry is None:
        why = "the strategy declares no bracket geometry"
        return LiveCompatibility(UNKNOWN, str(m), None, None, None, None, why)
    planning = PlanningCosts.from_cost_model(cost_model, market, symbol)
    tp_r, d_min = geometry(planning)

    def rr(d: float) -> Decimal | None:
        return net_rr(stop_fraction=d, tp_r=tp_r, cost_model=cost_model, market=market,
                      symbol=symbol)  # fmt: skip

    at_min = rr(d_min) if d_min is not None else None
    if Decimal(repr(tp_r)) <= m:
        return LiveCompatibility(
            INCOMPATIBLE, str(m), tp_r, None if at_min is None else str(at_min), None, d_min,
            f"gross reward/risk tp_r = {tp_r} cannot reach the live floor {m} after costs: every "
            "trade is rejected by the baseline book",
        )  # fmt: skip
    if at_min is not None and at_min >= m:
        why = (
            "every long trade the strategy can confirm passes the floor at modelled slippage "
            "(a wider live spread can still reject one)"
        )
        return LiveCompatibility(COMPATIBLE, str(m), tp_r, str(at_min), d_min, d_min, why)
    lo, hi = (d_min or 1e-6), 0.5
    hi_rr = rr(hi)
    if hi_rr is None or hi_rr < m:
        return LiveCompatibility(
            INCOMPATIBLE, str(m), tp_r, None if at_min is None else str(at_min), None, d_min,
            "no stop distance up to 50 % of price passes the live floor",
        )  # fmt: skip
    for _ in range(60):  # rr grows with the stop distance (costs are a fixed price fraction)
        mid = (lo + hi) / 2
        r = rr(mid)
        if r is not None and r >= m:
            hi = mid
        else:
            lo = mid
    return LiveCompatibility(
        CONDITIONAL, str(m), tp_r, None if at_min is None else str(at_min), hi, d_min,
        f"only trades with a stop at least {hi:.4%} from price pass the live floor",
    )  # fmt: skip


__all__ = [
    "COMPATIBLE",
    "CONDITIONAL",
    "INCOMPATIBLE",
    "UNKNOWN",
    "LiveCompatibility",
    "assess",
    "net_rr",
]
