"""Strategy interface (RESEARCH_PROTOCOL §1, SIMULATION §2).

A strategy is a pure function of the closed strategy bars up to the decision bar: on each closed
strategy bar it receives a trailing window (`BarArrays`, last row = the decision bar, nothing
later exists) and returns WATCH / CONFIRMED proposals with an entry rule, a stop and an exit
policy. Parameters are a frozen dataclass; ``params_hash`` is SHA-256 over the canonical JSON of
``{strategy_id, version, params}`` and identifies the configuration in manifests, the trial
registry and the holdout guard (``strategy_hash``).

Strategies never see costs as outcomes; they may use the *planning* cost estimate
(`PlanningCosts`, a deliberately pessimistic all-taker round trip from the cost model) to refuse
geometries whose costs eat the edge. They never size positions (the simulator's `Book` does).
"""

from __future__ import annotations

import dataclasses
import hashlib
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, ClassVar

from tsclaude.contracts.events import canonical_json
from tsclaude.contracts.market import Interval, Market
from tsclaude.contracts.signals import EntryRuleKind, Side, SignalStatus
from tsclaude.features.core import BarArrays
from tsclaude.sim.costs import CostModel, Liquidity

#: Extra bars kept in the trailing window beyond the declared warm-up (live recomputation margin).
WINDOW_MARGIN = 5


@dataclass(frozen=True, slots=True)
class PlanningCosts:
    """Pessimistic round-trip cost as a fraction of price: taker fee and slippage on both legs."""

    round_trip_frac: float
    basis: str

    @classmethod
    def from_cost_model(cls, model: CostModel, market: Market, symbol: str) -> PlanningCosts:
        fee = float(model.fee_rate(market, Liquidity.TAKER))
        est = model.slippage.estimate(market, symbol, Decimal(0), Decimal(0))
        slip = float(est.total_bps) / 10_000.0
        return cls(2.0 * (fee + slip), f"2x(taker {fee:g} + slippage {slip:g}) [{model.version}]")

    def cost_r(self, ref_price: float, stop_price: float) -> float:
        """Planned round-trip cost in R for a stop at `stop_price` from `ref_price`."""
        dist = abs(ref_price - stop_price)
        return float("inf") if dist <= 0 else self.round_trip_frac * ref_price / dist


@dataclass(frozen=True, slots=True)
class Proposal:
    """What a strategy proposes at a decision bar (prices are unrounded floats)."""

    stage: SignalStatus  # WATCH or CONFIRMED
    side: Side
    entry_kind: EntryRuleKind
    ref_price: float
    stop_price: float
    take_profit_price: float | None
    valid_bars: int  # validity window in strategy bars after the decision bar
    exit_policy: str
    entry_price: float | None = None
    time_exit_bars: int | None = None
    planned_cost_r: float | None = None
    conditions: Mapping[str, float | bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.stage not in (SignalStatus.WATCH, SignalStatus.CONFIRMED):
            raise ValueError("proposals are WATCH or CONFIRMED")
        if self.valid_bars < 1:
            raise ValueError("valid_bars must be >= 1")

    def to_payload(self) -> dict[str, Any]:
        return {
            "stage": str(self.stage),
            "side": str(self.side),
            "entry_kind": str(self.entry_kind),
            "ref_price": self.ref_price,
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "take_profit_price": self.take_profit_price,
            "valid_bars": self.valid_bars,
            "time_exit_bars": self.time_exit_bars,
            "exit_policy": self.exit_policy,
            "planned_cost_r": self.planned_cost_r,
            "conditions": dict(self.conditions),
        }


class Strategy[P](ABC):
    """Base class. Subclasses set ``strategy_id`` / ``version`` and implement the rules."""

    strategy_id: ClassVar[str]
    version: ClassVar[str]

    def __init__(self, params: P) -> None:
        if not dataclasses.is_dataclass(params) or not type(params).__dataclass_params__.frozen:  # type: ignore[attr-defined]
            raise TypeError("strategy params must be a frozen dataclass")
        self.params = params

    # ------------------------------------------------------------ identity
    def params_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self.params)  # type: ignore[call-overload,no-any-return]

    @property
    def params_hash(self) -> str:
        body = {
            "strategy_id": self.strategy_id,
            "version": self.version,
            "params": self.params_dict(),
        }
        return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()

    @property
    def strategy_hash(self) -> str:
        """Identifier given to the holdout guard and preregistrations (= params hash)."""
        return self.params_hash

    def describe(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "version": self.version,
            "interval": str(self.interval),
            "params": self.params_dict(),
            "params_hash": self.params_hash,
            "warmup_bars": self.warmup_bars,
        }

    # ------------------------------------------------------------ declared shape
    @property
    @abstractmethod
    def interval(self) -> Interval: ...

    @property
    @abstractmethod
    def warmup_bars(self) -> int:
        """Strategy bars needed before the first decision (max feature warm-up + lags)."""

    @property
    def window_bars(self) -> int:
        return self.warmup_bars + WINDOW_MARGIN

    # ------------------------------------------------------------ rules
    @abstractmethod
    def on_bar(self, window: BarArrays, costs: PlanningCosts) -> Sequence[Proposal]:
        """Proposals at the last bar of `window` (none while it is shorter than the warm-up)."""

    @abstractmethod
    def exit_geometry(self, window: BarArrays, side: Side, costs: PlanningCosts) -> Proposal | None:
        """A CONFIRMED market entry at the last bar with this strategy's exits (random control)."""

    def design_p_star(self, costs: PlanningCosts) -> float | None:
        """Break-even win rate fixed by design (pure bracket exits) or None (realized p*)."""
        return None

    def stop_loss_r(self, costs: PlanningCosts) -> float | None:
        """Planned loss at the stop including costs, in R (positive), if fixed by design."""
        return None
