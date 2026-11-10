"""Shared builders for the agent tests: a synthetic signal, packet inputs, scripted outputs.

All synthetic: no network, no API key, no real market data.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any

from tsclaude.agents.client import MockClient, MockStep
from tsclaude.agents.config import AgentMode, AgentsConfig, LoadedConfig, PacketConfig, Pricing
from tsclaude.agents.gate import AgentGate
from tsclaude.agents.packet import BarInput, PacketInputs
from tsclaude.contracts.market import Interval, Market
from tsclaude.contracts.signals import (
    Condition,
    EntryRule,
    EntryRuleKind,
    RunMode,
    Side,
    SignalRecord,
    SignalStatus,
)
from tsclaude.contracts.timeutil import ManualClock

DECISION_MS = 1_767_225_600_000  # 2026-01-01T00:00:00Z (synthetic)
BAR_MS = 15 * 60_000
SYMBOL = "SOLUSDT"
REF = 187.4321

PACKET_CFG = PacketConfig(
    max_bars=12,
    allowed_features=frozenset({"atr_pct_rank", "regime", "volume_ratio", "range_width_atr"}),
    allowed_health=frozenset({"streams_healthy", "open_gaps"}),
    allowed_risk=frozenset({"open_positions", "daily_loss_used_frac"}),
)


def make_config(mode: AgentMode = AgentMode.CONFIRMED, **overrides: Any) -> AgentsConfig:
    base: dict[str, Any] = {
        "version": "test",
        "mode": mode,
        "model": "mock-model",
        "pricing": Pricing(
            input_per_mtok=Decimal(2), output_per_mtok=Decimal(10), cached_input_per_mtok=Decimal(2)
        ),
        "daily_cap_usd": Decimal(1),
        "per_call_timeout_s": 2.0,
        "max_retries": 1,
        "max_output_tokens": 1024,
        "breaker_failure_threshold": 3,
        "breaker_cooldown_s": 600.0,
        "packet": PACKET_CFG,
    }
    return AgentsConfig.model_validate(base | overrides)


def make_signal(
    *,
    side: Side = Side.LONG,
    stage: SignalStatus = SignalStatus.CONFIRMED,
    run_mode: RunMode = RunMode.LIVE,
    conditions: tuple[Condition, ...] | None = None,
    market_conditions: dict[str, float | str | bool] | None = None,
) -> SignalRecord:
    long = side is Side.LONG
    stop = REF - 3.1 if long else REF + 3.1
    tp = REF + 6.2 if long else REF - 6.2
    return SignalRecord(
        signal_id="01KDTEST000000000000000000",
        run_mode=run_mode,
        market=Market.USDM,
        symbol=SYMBOL,
        timeframe=Interval.M15,
        strategy_id="bracket_reference",
        strategy_version="1",
        params_hash="p" * 16,
        prereg_id=None,
        evidence_grade="X",
        side=side,
        stage=stage,
        decision_time_ms=DECISION_MS,
        created_at_ms=DECISION_MS,
        snapshot_id=f"reference:usdm:{SYMBOL}:{DECISION_MS}",
        entry_conditions=conditions
        if conditions is not None
        else (
            Condition(
                name="atr_pct_rank", operator="<", threshold=0.2, observed=0.13, satisfied=True
            ),
            Condition(
                name="close_above_range_high",
                operator=">",
                threshold=REF - 0.5,
                observed=REF,
                satisfied=True,
            ),
        ),
        entry_rule=EntryRule(
            kind=EntryRuleKind.MARKET_NEXT_OPEN, valid_until_ms=DECISION_MS + BAR_MS
        ),
        ref_price=REF,
        stop_price=stop,
        stop_basis="1.5 x ATR below close",
        take_profit_price=tp,
        exit_policy="tp/sl; time exit after 16 bars",
        rr_gross=2.0,
        rr_net=1.83,
        est_cost_bps=12.0,
        break_even_win_rate=0.3529,
        rationale=f"{SYMBOL} broke out above {REF - 0.5} on 2026-01-01",
        market_conditions=market_conditions or {"regime": "range"},
    )


def make_bars(n: int = 16, end_ms: int = DECISION_MS) -> list[BarInput]:
    bars = []
    for i in range(n):
        close_ms = end_ms - (n - 1 - i) * BAR_MS
        base = REF - (n - 1 - i) * 0.11
        bars.append(
            BarInput(
                close_time_ms=close_ms,
                open=base - 0.05,
                high=base + 0.4,
                low=base - 0.37,
                close=base,
                volume=1000.0 + 37.0 * i,
            )
        )
    return bars


def make_inputs(signal: SignalRecord | None = None, **overrides: Any) -> PacketInputs:
    base: dict[str, Any] = {
        "signal": signal or make_signal(),
        "bars": make_bars(),
        "atr": 2.07,
        "features": {"atr_pct_rank": 0.13, "regime": "range", "volume_ratio": 1.42, "rsi": 61.0},
        "data_health": {"streams_healthy": True, "open_gaps": 0},
        "risk_state": {"open_positions": 1, "daily_loss_used_frac": 0.0},
        "spread_bps": 0.8,
        "funding_bps": 1.0,
    }
    return PacketInputs(**(base | overrides))


def dim(
    score: int, refs: Iterable[str] = ("feat.atr_pct_rank",), concerns: Iterable[str] = ()
) -> dict[str, Any]:
    return {"score": score, "evidence_refs": list(refs), "concerns": list(concerns)}


def analyst_payload(
    scores: tuple[int, int, int, int] = (1, 1, 0, 1), stance: str = "support", summary: str = ""
) -> dict[str, Any]:
    r, t, v, k = scores
    return {
        "dimensions": {
            "regime": dim(r, ["feat.regime"]),
            "technical": dim(t, ["cond.atr_pct_rank", "bars.ohlc_atr[-1]"]),
            "volatility_liquidity": dim(v, ["market.spread_bps", "costs.est_cost_bps"]),
            "risk": dim(k, ["costs.rr_net", "geometry.stop_atr"]),
        },
        "stance": stance,
        "summary": summary or "Compression in a range regime with acceptable net reward to risk.",
    }


def skeptic_payload(severities: Iterable[str] = ("minor",)) -> dict[str, Any]:
    return {
        "objections": [
            {
                "severity": s,
                "claim": "Breakout on modest relative volume.",
                "evidence_refs": ["bars.volume_rel"],
            }
            for s in severities
        ],
        "summary": "Weak confirmation; nothing decisive.",
    }


def valid_pair(
    scores: tuple[int, int, int, int] = (1, 1, 0, 1),
    stance: str = "support",
    severities: Iterable[str] = ("minor",),
) -> tuple[list[MockStep], list[MockStep]]:
    return [MockStep.valid(analyst_payload(scores, stance))], [
        MockStep.valid(skeptic_payload(severities))
    ]


def make_gate(
    client: MockClient | None,
    *,
    mode: AgentMode = AgentMode.CONFIRMED,
    clock: ManualClock | None = None,
    data_dir: Path | None = None,
    errors: tuple[str, ...] = (),
    **overrides: Any,
) -> AgentGate:
    cfg = make_config(mode, **overrides)
    return AgentGate(
        LoadedConfig(cfg, "test", errors),
        client=client,
        clock=clock or ManualClock(DECISION_MS + 1_000),
        data_dir=data_dir,
    )
