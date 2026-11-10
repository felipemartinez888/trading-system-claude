"""The live risk policy in backtests (queue P1-6, decision D-E; owner direction 2026-11-10: keep
the paper/live floor ``min_rr_net`` 1.5 and apply identical risk policies in backtests and Paper).

Backtests used `sim.book.allow_all`, so research could certify trades the live baseline book never
accepts. With ``risk_policy: live`` in the backtest config, every backtest book (base, cost stress,
random-entry control, the max-loss estimate) asks the **paper engine itself** -- the same
`risk.engine.RiskEngine`, rule functions, `configs/risk.yaml` and base cost model -- before every
order is accepted and again before every fill, exactly as the paper baseline book does, and halts
at the same drawdown kill switch.

**Policy rules: identical to Paper.** reward_risk (net reward/risk >= ``min_rr_net`` after fees and
slippage, take-profit required), risk_per_trade, max_positions, symbol_positions,
correlated_exposure, gross_exposure, daily_loss, drawdown, signal_validity, plus the book's kill
switch at ``drawdown_kill_switch``.

**Operational rules: substituted, stated in every manifest.** market_view, data_freshness,
clock_skew and spread judge the *live* environment (WebSocket stream health, the PC clock against
Binance, order-book spread samples). History has none of these inputs: the lake holds closed 1m
bars only, no book_top, and a simulated clock has no skew. Their backtest counterpart is the run's
integrity grading, which is stricter in one way (any missing 1m bar in range makes the run grade X,
RESEARCH_PROTOCOL §6) and weaker in another (no spread: the engine's slippage is the modelled one,
``max(modelled, live half-spread)`` has no live term). The cost-stress runs (x1.5, x2) are the
evidence against that second gap.

The market view a check sees is built from the 1m bars observed so far with ``close_time_ms <=``
the check's cutoff (`risk.market.cutoff_ms`): at submit, the decision close; at fill, the close
before the executing bar's open. The values are the paper tracker's (`to_decimal` of the bar's
close and quote volume), so the same bar gives the same reference price and slippage estimate.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from tsclaude.contracts.market import Bar, Market
from tsclaude.risk.config import RiskConfig
from tsclaude.risk.engine import RISK_ENGINE_VERSION, RULES, RiskEngine, Rule
from tsclaude.risk.market import MarketView, SymbolMarket, cutoff_ms
from tsclaude.sim.book import RiskCheck, RiskDecision
from tsclaude.sim.costs import CostModel
from tsclaude.sim.money import to_decimal

ALLOW_ALL = "allow_all"
LIVE = "live"
MODES = (ALLOW_ALL, LIVE)
POLICY_VERSION = "backtest-risk-policy-v1"

#: Rules that judge the live environment; history has no input for them (see module docstring).
OPERATIONAL_RULES: frozenset[Rule] = frozenset(
    {Rule.MARKET_VIEW, Rule.DATA_FRESHNESS, Rule.CLOCK_SKEW, Rule.SPREAD}
)
#: The paper engine's own rule functions, in its order, minus the operational ones.
POLICY_RULES = tuple((rule, fn) for rule, fn in RULES if rule not in OPERATIONAL_RULES)

SUBSTITUTIONS: Mapping[str, str] = {
    str(Rule.MARKET_VIEW): "the view is built from closed 1m bars at the cutoff; none -> blocked",
    str(Rule.DATA_FRESHNESS): "run integrity grading: any missing 1m bar in range -> grade X",
    str(Rule.CLOCK_SKEW): "simulated clock: no skew exists",
    str(Rule.SPREAD): "no order-book history: modelled slippage only; cost stress x1.5, x2",
}

_KEEP_BARS = 4  # the newest bar at or before any cutoff is among the last few observed


@dataclass
class BarViews:
    """Market views for backtest risk checks, from the 1m bars observed so far."""

    _bars: dict[tuple[Market, str], deque[tuple[int, Decimal, Decimal]]] = field(
        default_factory=dict
    )

    def observe(self, bar: Bar) -> None:
        key = (bar.market, bar.symbol)
        seq = self._bars.setdefault(key, deque(maxlen=_KEEP_BARS))
        if seq and bar.close_time_ms <= seq[-1][0]:
            raise ValueError(f"bars out of order for {bar.market}:{bar.symbol}")
        seq.append((bar.close_time_ms, to_decimal(bar.close), to_decimal(bar.quote_volume)))

    def view(self, check: RiskCheck) -> MarketView | None:
        cutoff = cutoff_ms(check.stage, check.ts_ms)
        key = (check.request.market, check.request.symbol)
        last = next((b for b in reversed(self._bars.get(key, ())) if b[0] <= cutoff), None)
        if last is None:
            return None  # no reference price -> reward/risk undefined -> blocked (fail closed)
        sym = SymbolMarket(
            market=key[0],
            symbol=key[1],
            unhealthy_streams=(),
            stream_count=1,
            open_gap_minutes=0,
            last_bar_close_ms=last[0],
            last_close=last[1],
            last_quote_volume=last[2],
            spread_bps=None,
            spread_as_of_ms=None,
            median_spread_bps=None,
            spread_samples=0,
        )
        return MarketView(as_of_ms=last[0], clock_offset_ms=0, symbols={key: sym})


class BacktestRiskHook:
    """A `sim.book.RiskHook` applying the paper engine's policy rules to backtest checks."""

    def __init__(self, risk: RiskConfig, cost_model: CostModel, views: BarViews) -> None:
        self.engine = RiskEngine(risk, cost_model, POLICY_RULES)
        self.views = views
        self.blocked_by_rule: dict[str, int] = {}
        self.checks = 0

    def __call__(self, check: RiskCheck) -> RiskDecision:
        self.checks += 1
        verdict = self.engine.evaluate(check, self.views.view(check))
        for reason in verdict.reasons:
            rule = reason.split(":", 1)[0]
            self.blocked_by_rule[rule] = self.blocked_by_rule.get(rule, 0) + 1
        return verdict.decision()


def describe(mode: str, risk: RiskConfig | None) -> dict[str, Any]:
    """What a manifest records about the risk policy (hashed with the backtest config)."""
    if mode == ALLOW_ALL or risk is None:
        return {"mode": ALLOW_ALL, "note": "sim.book.allow_all: no risk rule applies"}
    return {
        "mode": LIVE,
        "policy_version": POLICY_VERSION,
        "engine_version": RISK_ENGINE_VERSION,
        "risk_config_version": risk.version,
        "risk_config_hash": risk.config_hash,
        "rules": [str(r) for r, _ in POLICY_RULES],
        "kill_switch_drawdown": str(risk.drawdown_kill_switch),
        "substituted": dict(SUBSTITUTIONS),
    }


__all__ = [
    "ALLOW_ALL",
    "LIVE",
    "MODES",
    "OPERATIONAL_RULES",
    "POLICY_RULES",
    "POLICY_VERSION",
    "SUBSTITUTIONS",
    "BacktestRiskHook",
    "BarViews",
    "describe",
]
