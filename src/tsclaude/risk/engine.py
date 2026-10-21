"""Deterministic, fail-closed risk engine (SIMULATION §6).

``RiskEngine.evaluate(check, market)`` is a **pure function** of

- the proposal and the book state (`sim.book.RiskCheck`: positions, equity, start-of-day and peak
  equity, sizing config, timing), and
- a market view (`risk.market.MarketView`) stamped at or before the check's cutoff (the decision
  close for ``submit``, the previous 1m close for ``fill``).

Every rule runs on every check; the verdict lists all failing rules in rule order. A rule that
raises blocks with ``rule_error:<rule>:<ExceptionType>``; a failure outside the rules (bad view,
bad config) blocks with ``risk_error:<ExceptionType>``. ``RISK_BLOCKED`` is the verdict status of
every non-passing decision -- there is no path from an exception to an allowed entry.

The drawdown kill switch has two halves: this engine blocks entries once the drawdown limit is
reached, and the book itself halts and flattens at its next 1m mark (`Book(kill_switch_drawdown=)`,
wired by `kill_switch_drawdown` below); only a manual, logged reset re-opens it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum

from tsclaude.contracts.signals import EntryRuleKind, Side
from tsclaude.risk.config import RiskConfig
from tsclaude.risk.market import MarketView, SymbolMarket, cutoff_ms
from tsclaude.sim.book import RiskCheck, RiskDecision
from tsclaude.sim.costs import CostModel, Liquidity
from tsclaude.sim.money import BPS, SIM_CONTEXT, ZERO, dstr, q8

RISK_ENGINE_VERSION = "risk-engine-v1"


class RiskStatus(StrEnum):
    PASSED = "RISK_PASSED"
    BLOCKED = "RISK_BLOCKED"


class Rule(StrEnum):
    """Rule codes in evaluation order (stable: reports and tests key on them)."""

    MARKET_VIEW = "market_view"  # view present, not after the cutoff, not stale
    DATA_FRESHNESS = "data_freshness"
    CLOCK_SKEW = "clock_skew"
    SPREAD = "spread"
    REWARD_RISK = "reward_risk"
    RISK_PER_TRADE = "risk_per_trade"
    MAX_POSITIONS = "max_positions"
    SYMBOL_POSITIONS = "symbol_positions"
    CORRELATED_EXPOSURE = "correlated_exposure"
    GROSS_EXPOSURE = "gross_exposure"
    DAILY_LOSS = "daily_loss"
    DRAWDOWN = "drawdown"
    SIGNAL_VALIDITY = "signal_validity"


@dataclass(frozen=True, slots=True)
class RiskVerdict:
    status: RiskStatus
    reasons: tuple[str, ...]  # "<rule>:<detail>" for every failing rule, in rule order

    @property
    def allowed(self) -> bool:
        return self.status is RiskStatus.PASSED

    def decision(self) -> RiskDecision:
        if self.allowed:
            return RiskDecision(True, str(RiskStatus.PASSED))
        return RiskDecision(False, f"{RiskStatus.BLOCKED}:{'|'.join(self.reasons)}")


type RuleFn = Callable[[RiskCheck, MarketView | None, RiskConfig, CostModel], str | None]


# ---------------------------------------------------------------- helpers
def _symbol(check: RiskCheck, view: MarketView | None) -> SymbolMarket | None:
    if view is None:
        return None
    return view.get(check.request.market, check.request.symbol)


def reference_price(check: RiskCheck, view: MarketView | None) -> Decimal | None:
    """Expected entry price: the order price, or the latest close at the cutoff (market orders)."""
    req = check.request
    if req.entry_price is not None:
        return req.entry_price
    sym = _symbol(check, view)
    return None if sym is None else sym.last_close


def _slip_bps(costs: CostModel, check: RiskCheck, sym: SymbolMarket | None) -> Decimal:
    req = check.request
    volume = ZERO if sym is None or sym.last_quote_volume is None else sym.last_quote_volume
    model = costs.slippage.estimate(req.market, req.symbol, ZERO, volume).total_bps
    live = ZERO if sym is None or sym.spread_bps is None else sym.spread_bps / 2
    return max(model, live)


def cost_adjusted_rr(
    check: RiskCheck, view: MarketView | None, costs: CostModel
) -> tuple[Decimal | None, str]:
    """(net reward / net risk per unit, detail). None when undefined (no TP or no reference)."""
    req = check.request
    tp = req.take_profit_price
    if tp is None:
        return None, "no_take_profit"
    ref = reference_price(check, view)
    if ref is None:
        return None, "no_reference_price"
    sym = _symbol(check, view)
    slip = _slip_bps(costs, check, sym) / BPS
    taker = costs.fee_rate(req.market, Liquidity.TAKER)
    entry_liq = Liquidity.MAKER if req.kind is EntryRuleKind.LIMIT else Liquidity.TAKER
    entry_slip = slip if entry_liq is Liquidity.TAKER else ZERO
    entry_rate = costs.fee_rate(req.market, entry_liq) + entry_slip
    tp_liq = Liquidity.TAKER if costs.tp_as_taker else Liquidity.MAKER
    tp_rate = costs.fee_rate(req.market, tp_liq) + (slip if tp_liq is Liquidity.TAKER else ZERO)
    sl_rate = taker + slip
    sign = 1 if req.side is Side.LONG else -1
    with localcontext(SIM_CONTEXT):
        entry_cost = ref * entry_rate
        reward = sign * (tp - ref) - entry_cost - tp * tp_rate
        risk = sign * (ref - req.stop_price) + entry_cost + req.stop_price * sl_rate
        if risk <= 0:
            return None, "non_positive_risk"
        return q8(reward / risk), f"ref={dstr(ref)}"


def planned_notional(check: RiskCheck, view: MarketView | None) -> Decimal | None:
    """Upper bound of the new position's notional under the book's fixed-fractional sizing."""
    ref = reference_price(check, view)
    if ref is None or check.risk_per_trade is None or check.max_notional_fraction is None:
        return None
    distance = abs(ref - check.request.stop_price)
    if distance == 0:
        return None
    with localcontext(SIM_CONTEXT):
        by_risk = check.risk_per_trade * check.equity / distance * ref
        return q8(min(by_risk, check.max_notional_fraction * check.equity))


# ---------------------------------------------------------------- rules
def rule_market_view(
    check: RiskCheck, view: MarketView | None, cfg: RiskConfig, costs: CostModel
) -> str | None:
    if view is None:
        return "no_market_view"
    cutoff = cutoff_ms(check.stage, check.ts_ms)
    if view.as_of_ms > cutoff:
        return f"view_after_cutoff:{view.as_of_ms}>{cutoff}"
    if cutoff - view.as_of_ms > cfg.data_freshness.max_view_age_ms:
        return f"view_stale:{cutoff - view.as_of_ms}ms"
    if _symbol(check, view) is None:
        return "no_symbol_data"
    return None


def rule_data_freshness(
    check: RiskCheck, view: MarketView | None, cfg: RiskConfig, costs: CostModel
) -> str | None:
    sym = _symbol(check, view)
    if sym is None:
        return "no_symbol_data"
    f = cfg.data_freshness
    if f.require_streams_healthy and not sym.streams_healthy:
        if sym.stream_count == 0:
            return "no_stream_status"
        return f"streams_unhealthy:{','.join(sym.unhealthy_streams)}"
    if sym.open_gap_minutes > f.max_open_gap_minutes:
        return f"unresolved_gap:{sym.open_gap_minutes}m"
    cutoff = cutoff_ms(check.stage, check.ts_ms)
    if sym.last_bar_close_ms is None:
        return "no_closed_bar"
    if sym.last_bar_close_ms > cutoff:
        return "bar_after_cutoff"
    if cutoff - sym.last_bar_close_ms > f.max_bar_age_ms:
        return f"bar_stale:{cutoff - sym.last_bar_close_ms}ms"
    return None


def rule_clock_skew(
    check: RiskCheck, view: MarketView | None, cfg: RiskConfig, costs: CostModel
) -> str | None:
    if view is None or view.clock_offset_ms is None:
        return "skew_unknown"
    if abs(view.clock_offset_ms) > cfg.clock_skew_max_ms:
        return f"skew:{view.clock_offset_ms}ms"
    return None


def rule_spread(
    check: RiskCheck, view: MarketView | None, cfg: RiskConfig, costs: CostModel
) -> str | None:
    sym = _symbol(check, view)
    if sym is None or sym.spread_bps is None:
        return "spread_unknown"
    limit = cfg.spread.cap_bps
    if sym.median_spread_bps is not None:
        limit = max(cfg.spread.median_multiple * sym.median_spread_bps, limit)
    if sym.spread_bps > limit:
        return f"spread_wide:{dstr(sym.spread_bps)}>{dstr(limit)}bps"
    return None


def rule_reward_risk(
    check: RiskCheck, view: MarketView | None, cfg: RiskConfig, costs: CostModel
) -> str | None:
    rr, detail = cost_adjusted_rr(check, view, costs)
    if rr is None:
        if detail == "no_take_profit" and not cfg.require_take_profit:
            return None
        return f"rr_undefined:{detail}"
    if rr < cfg.min_rr_net:
        return f"rr_net:{dstr(rr)}<{dstr(cfg.min_rr_net)}"
    return None


def rule_risk_per_trade(
    check: RiskCheck, view: MarketView | None, cfg: RiskConfig, costs: CostModel
) -> str | None:
    if check.risk_per_trade is None:
        return "risk_per_trade_unknown"
    if check.risk_per_trade > cfg.max_risk_per_trade:
        return f"risk_per_trade:{dstr(check.risk_per_trade)}>{dstr(cfg.max_risk_per_trade)}"
    if check.equity <= 0:
        return "non_positive_equity"
    return None


def rule_max_positions(
    check: RiskCheck, view: MarketView | None, cfg: RiskConfig, costs: CostModel
) -> str | None:
    if len(check.positions) >= cfg.max_positions:
        return f"positions:{len(check.positions)}>={cfg.max_positions}"
    return None


def rule_symbol_positions(
    check: RiskCheck, view: MarketView | None, cfg: RiskConfig, costs: CostModel
) -> str | None:
    req = check.request
    same = sum(1 for p in check.positions if p.symbol == req.symbol)
    if same >= cfg.max_positions_per_symbol:
        return f"symbol_positions:{req.symbol}:{same}>={cfg.max_positions_per_symbol}"
    return None


def rule_correlated_exposure(
    check: RiskCheck, view: MarketView | None, cfg: RiskConfig, costs: CostModel
) -> str | None:
    corr = cfg.correlation
    cluster = corr.cluster_of(check.request.symbol)
    n = sum(1 for p in check.positions if corr.cluster_of(p.symbol) == cluster)
    if n >= corr.max_per_cluster:
        return f"cluster:{cluster}:{n}>={corr.max_per_cluster}"
    return None


def rule_gross_exposure(
    check: RiskCheck, view: MarketView | None, cfg: RiskConfig, costs: CostModel
) -> str | None:
    new = planned_notional(check, view)
    if new is None:
        return "planned_notional_unknown"
    if check.equity <= 0:
        return "non_positive_equity"
    with localcontext(SIM_CONTEXT):
        held = sum((max(p.marked_notional, p.entry_notional) for p in check.positions), ZERO)
        gross = q8((held + new) / check.equity)
    if gross > cfg.max_gross_exposure:
        return f"gross:{dstr(gross)}>{dstr(cfg.max_gross_exposure)}"
    return None


def rule_daily_loss(
    check: RiskCheck, view: MarketView | None, cfg: RiskConfig, costs: CostModel
) -> str | None:
    sod = check.start_of_day_equity
    if sod is None or sod <= 0:
        return "start_of_day_equity_unknown"
    with localcontext(SIM_CONTEXT):
        change = q8((check.equity - sod) / sod)
    if change <= -cfg.daily_loss_limit:
        return f"daily_loss:{dstr(change)}<=-{dstr(cfg.daily_loss_limit)}"
    return None


def rule_drawdown(
    check: RiskCheck, view: MarketView | None, cfg: RiskConfig, costs: CostModel
) -> str | None:
    peak = check.peak_equity
    if peak is None or peak <= 0:
        return "peak_equity_unknown"
    with localcontext(SIM_CONTEXT):
        dd = q8((peak - check.equity) / peak)
    if dd >= cfg.drawdown_kill_switch:
        return f"drawdown:{dstr(dd)}>={dstr(cfg.drawdown_kill_switch)}"
    return None


def rule_signal_validity(
    check: RiskCheck, view: MarketView | None, cfg: RiskConfig, costs: CostModel
) -> str | None:
    valid_until = check.request.valid_until_ms
    if check.stage == "submit":
        if check.earliest_exec_ms is None:
            return "earliest_execution_unknown"
        if valid_until < check.earliest_exec_ms:
            return "expires_before_execution"
        return None
    if check.ts_ms > valid_until:
        return "expired"
    return None


RULES: tuple[tuple[Rule, RuleFn], ...] = (
    (Rule.MARKET_VIEW, rule_market_view),
    (Rule.DATA_FRESHNESS, rule_data_freshness),
    (Rule.CLOCK_SKEW, rule_clock_skew),
    (Rule.SPREAD, rule_spread),
    (Rule.REWARD_RISK, rule_reward_risk),
    (Rule.RISK_PER_TRADE, rule_risk_per_trade),
    (Rule.MAX_POSITIONS, rule_max_positions),
    (Rule.SYMBOL_POSITIONS, rule_symbol_positions),
    (Rule.CORRELATED_EXPOSURE, rule_correlated_exposure),
    (Rule.GROSS_EXPOSURE, rule_gross_exposure),
    (Rule.DAILY_LOSS, rule_daily_loss),
    (Rule.DRAWDOWN, rule_drawdown),
    (Rule.SIGNAL_VALIDITY, rule_signal_validity),
)


class RiskEngine:
    def __init__(
        self,
        config: RiskConfig,
        cost_model: CostModel,
        rules: Sequence[tuple[Rule, RuleFn]] = RULES,
    ) -> None:
        self.config = config
        self.cost_model = cost_model
        self.rules = tuple(rules)
        if not self.rules:
            raise ValueError("a risk engine needs rules")

    def describe(self) -> dict[str, object]:
        return {
            "engine_version": RISK_ENGINE_VERSION,
            "config_version": self.config.version,
            "config_hash": self.config.config_hash,
            "rules": [str(r) for r, _ in self.rules],
        }

    def evaluate(self, check: RiskCheck, market: MarketView | None) -> RiskVerdict:
        try:
            reasons: list[str] = []
            for rule, fn in self.rules:
                try:
                    failed = fn(check, market, self.config, self.cost_model)
                except Exception as exc:  # fail closed per rule
                    failed = f"rule_error:{type(exc).__name__}"
                if failed is not None:
                    reasons.append(f"{rule}:{failed}")
            status = RiskStatus.BLOCKED if reasons else RiskStatus.PASSED
            return RiskVerdict(status, tuple(reasons))
        except Exception as exc:  # fail closed
            return RiskVerdict(RiskStatus.BLOCKED, (f"risk_error:{type(exc).__name__}",))


def kill_switch_drawdown(config: RiskConfig) -> Decimal:
    """Drawdown fraction at which a book halts itself (pass to `Book(kill_switch_drawdown=)`)."""
    return config.drawdown_kill_switch


__all__ = [
    "RISK_ENGINE_VERSION",
    "RULES",
    "RiskEngine",
    "RiskStatus",
    "RiskVerdict",
    "Rule",
    "cost_adjusted_rr",
    "kill_switch_drawdown",
    "planned_notional",
    "reference_price",
]
