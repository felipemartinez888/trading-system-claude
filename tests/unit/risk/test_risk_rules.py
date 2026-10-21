"""Golden risk-rule cases (SIMULATION §6): a baseline check that passes every rule, then each rule
alone blocks it. Hand computations for the cost-adjusted reward/risk are in the docstrings."""

from __future__ import annotations

from decimal import Decimal as D

import pytest
from risk_helpers import DECISION, EXEC, check, position, request, view

from tsclaude.contracts.market import Market
from tsclaude.contracts.signals import EntryRuleKind, Side
from tsclaude.risk.config import CorrelationConfig, RiskConfig
from tsclaude.risk.engine import (
    RULES,
    RiskEngine,
    RiskStatus,
    Rule,
    cost_adjusted_rr,
    planned_notional,
)
from tsclaude.sim.costs import CostModel
from tsclaude.sim.testing import minute

ENGINE = RiskEngine(RiskConfig(), CostModel())


def rules_failing(verdict_reasons: tuple[str, ...]) -> list[str]:
    return [r.split(":", 1)[0] for r in verdict_reasons]


def test_baseline_passes_every_rule() -> None:
    for stage in ("submit", "fill"):
        v = ENGINE.evaluate(check(stage), view(as_of_ms=DECISION - 400))
        assert v.status is RiskStatus.PASSED, v.reasons
        assert v.decision().allow is True


def test_rule_order_and_codes_are_stable() -> None:
    assert [str(r) for r, _ in RULES] == [
        "market_view",
        "data_freshness",
        "clock_skew",
        "spread",
        "reward_risk",
        "risk_per_trade",
        "max_positions",
        "symbol_positions",
        "correlated_exposure",
        "gross_exposure",
        "daily_loss",
        "drawdown",
        "signal_validity",
    ]


def test_cost_adjusted_rr_hand_computed() -> None:
    """Long market, ref = last close 1000, stop 990, TP 1030, spot fees 0.1 % maker and taker,
    slippage max(model 2 + 1 bps, live half-spread 0.5 bps) = 3 bps on taker sides.
    entry cost = 1000 x (0.001 + 0.0003) = 1.3; TP (maker) cost = 1030 x 0.001 = 1.03;
    SL (taker) cost = 990 x 0.0013 = 1.287.
    reward = 30 - 1.3 - 1.03 = 27.67; risk = 10 + 1.3 + 1.287 = 12.587; rr = 2.19829983..."""
    rr, detail = cost_adjusted_rr(check(), view(), CostModel())
    assert rr == D("2.19829983")
    assert detail == "ref=1000"
    # planned notional = min(0.005 x 10000 / 10 x 1000, 1 x 10000) = 5000
    assert planned_notional(check(), view()) == D("5000")


GOLDEN: list[tuple[str, Rule, dict[str, object], dict[str, object]]] = [
    # (case id, rule expected to fail alone, check overrides, view overrides)
    ("view_missing", Rule.MARKET_VIEW, {}, {"__none__": True}),
    ("view_after_cutoff", Rule.MARKET_VIEW, {}, {"as_of_ms": DECISION + 1}),
    ("view_stale", Rule.MARKET_VIEW, {}, {"as_of_ms": DECISION - 5_001}),
    (
        "stream_unhealthy",
        Rule.DATA_FRESHNESS,
        {},
        {"unhealthy_streams": ("spot:BTCUSDT:kline_1m",)},
    ),
    ("no_stream_status", Rule.DATA_FRESHNESS, {}, {"stream_count": 0}),
    ("open_gap", Rule.DATA_FRESHNESS, {}, {"open_gap_minutes": 1}),
    ("bar_stale", Rule.DATA_FRESHNESS, {}, {"last_bar_close_ms": DECISION - 120_001}),
    ("bar_after_cutoff", Rule.DATA_FRESHNESS, {}, {"last_bar_close_ms": DECISION + 60_000}),
    ("skew_too_large", Rule.CLOCK_SKEW, {}, {"offset": -1_001}),
    ("skew_unknown", Rule.CLOCK_SKEW, {}, {"offset": None}),
    (
        "spread_above_cap_no_median",
        Rule.SPREAD,
        {},
        {"spread_bps": D("12"), "median_spread_bps": None},
    ),
    (
        "spread_above_2x_median",
        Rule.SPREAD,
        {},
        {"spread_bps": D("12.5"), "median_spread_bps": D("6")},
    ),
    ("spread_unknown", Rule.SPREAD, {}, {"spread_bps": None}),
    ("rr_below_min", Rule.REWARD_RISK, {"request": request(take_profit_price=D("1020"))}, {}),
    ("rr_no_tp", Rule.REWARD_RISK, {"request": request(take_profit_price=None)}, {}),
    ("risk_per_trade", Rule.RISK_PER_TRADE, {"risk_per_trade": D("0.0051")}, {}),
    ("risk_per_trade_unknown", Rule.RISK_PER_TRADE, {"risk_per_trade": None}, {}),
    ("max_positions", Rule.MAX_POSITIONS, {"positions": [position(f"S{i}") for i in range(5)]}, {}),
    (
        "symbol_busy",
        Rule.SYMBOL_POSITIONS,
        {"positions": [position("BTCUSDT", market=Market.USDM)]},
        {},
    ),
    ("cluster", Rule.CORRELATED_EXPOSURE, {"positions": [position(f"C{i}") for i in range(3)]}, {}),
    ("gross", Rule.GROSS_EXPOSURE, {"positions": [position("ETHUSDT", "5001")]}, {}),
    ("daily_loss", Rule.DAILY_LOSS, {"equity": D("9800"), "peak_equity": D("9800")}, {}),
    ("drawdown", Rule.DRAWDOWN, {"peak_equity": D("11112")}, {}),
    ("validity_submit", Rule.SIGNAL_VALIDITY, {"request": request(valid_until_ms=EXEC - 1)}, {}),
    ("validity_unknown_exec", Rule.SIGNAL_VALIDITY, {"earliest_exec_ms": None}, {}),
]

CLUSTERED = RiskConfig(correlation=CorrelationConfig(clusters={f"S{i}": f"c{i}" for i in range(5)}))


@pytest.mark.parametrize(("case", "rule", "ck", "vw"), GOLDEN, ids=[g[0] for g in GOLDEN])
def test_each_rule_alone_blocks(
    case: str, rule: Rule, ck: dict[str, object], vw: dict[str, object]
) -> None:
    engine = RiskEngine(CLUSTERED, CostModel()) if case == "max_positions" else ENGINE
    vw = dict(vw)
    market_view = None if vw.pop("__none__", False) else view(**vw)  # type: ignore[arg-type]
    v = engine.evaluate(check(**ck), market_view)
    assert v.status is RiskStatus.BLOCKED
    failing = rules_failing(v.reasons)
    if case == "view_missing":
        # No view: every market-data rule is blind and blocks too, and so do the rules that need
        # the reference price of a market order (R:R, planned notional for gross exposure).
        assert failing == [
            "market_view",
            "data_freshness",
            "clock_skew",
            "spread",
            "reward_risk",
            "gross_exposure",
        ]
    elif case == "risk_per_trade_unknown":  # unknown sizing fraction: notional unknown too
        assert failing == ["risk_per_trade", "gross_exposure"]
    else:
        assert failing == [str(rule)], v.reasons
    assert v.decision().allow is False
    assert v.decision().reason.startswith("RISK_BLOCKED:")


def test_boundaries_are_inclusive_where_the_spec_says_so() -> None:
    """Skew exactly 1 s passes; spread exactly at the limit passes; gross exactly 100 % passes;
    daily loss exactly -2 % blocks; drawdown exactly -10 % blocks; validity == exec passes."""
    assert ENGINE.evaluate(check(), view(offset=1000)).allowed
    assert ENGINE.evaluate(check(), view(spread_bps=D("12"), median_spread_bps=D("6"))).allowed
    assert ENGINE.evaluate(check(positions=[position("ETHUSDT", "5000")]), view()).allowed
    blocked = ENGINE.evaluate(check(equity=D("9800"), peak_equity=D("9800")), view())
    assert rules_failing(blocked.reasons) == ["daily_loss"]
    assert ENGINE.evaluate(check(equity=D("9801"), peak_equity=D("9801")), view()).allowed
    dd = ENGINE.evaluate(check(peak_equity=D("11111.11111112")), view())
    assert rules_failing(dd.reasons) == ["drawdown"]
    assert ENGINE.evaluate(check(request=request(valid_until_ms=EXEC)), view()).allowed


def test_fill_stage_uses_previous_close_cutoff_and_validity() -> None:
    """Fill at EXEC (minute-10 open): cutoff = EXEC - 1 = DECISION. A view stamped at EXEC is
    after the cutoff; an order past its validity is blocked."""
    assert ENGINE.evaluate(check("fill"), view(as_of_ms=DECISION)).allowed
    late = ENGINE.evaluate(check("fill"), view(as_of_ms=EXEC))
    assert rules_failing(late.reasons) == ["market_view"]
    expired = ENGINE.evaluate(
        check("fill", ts_ms=minute(21), request=request(valid_until_ms=minute(20))),
        view(as_of_ms=minute(21) - 500, last_bar_close_ms=minute(21) - 1),
    )
    assert rules_failing(expired.reasons) == ["signal_validity"]


def test_rule_exception_blocks_and_names_the_rule() -> None:
    def broken(*_: object) -> str | None:
        raise ZeroDivisionError

    engine = RiskEngine(RiskConfig(), CostModel(), [*RULES[:-1], (Rule.SIGNAL_VALIDITY, broken)])
    v = engine.evaluate(check(), view())
    assert v.status is RiskStatus.BLOCKED
    assert v.reasons == ("signal_validity:rule_error:ZeroDivisionError",)


def test_engine_level_exception_blocks() -> None:
    """A malformed view object (not a MarketView) never yields an allow."""
    v = ENGINE.evaluate(check(), object())  # type: ignore[arg-type]
    assert v.status is RiskStatus.BLOCKED


def test_short_rr_and_limit_entry_use_order_price_and_maker_fee() -> None:
    """Short USD-M limit at 1000, stop 1010, TP 970. Fees maker 0.02 %, taker 0.05 %, slip 3 bps.
    entry (maker) = 1000 x 0.0002 = 0.2; TP (maker) = 970 x 0.0002 = 0.194;
    SL (taker + slip) = 1010 x 0.0008 = 0.808. reward = 30 - 0.2 - 0.194 = 29.606;
    risk = 10 + 0.2 + 0.808 = 11.008; rr = 2.68949855..."""
    req = request(
        market=Market.USDM,
        side=Side.SHORT,
        kind=EntryRuleKind.LIMIT,
        entry_price=D("1000"),
        stop_price=D("1010"),
        take_profit_price=D("970"),
    )
    rr, detail = cost_adjusted_rr(check(request=req), None, CostModel())
    assert rr == D("2.68949855")
    assert detail == "ref=1000"


def test_rr_no_take_profit_allowed_only_when_configured() -> None:
    cfg = RiskConfig(require_take_profit=False)
    v = RiskEngine(cfg, CostModel()).evaluate(
        check(request=request(take_profit_price=None)), view()
    )
    assert v.allowed


def test_engine_describe_carries_config_hash() -> None:
    d = ENGINE.describe()
    assert d["config_hash"] == RiskConfig().config_hash
    assert d["rules"] == [str(r) for r, _ in RULES]
    assert minute(0) < DECISION
