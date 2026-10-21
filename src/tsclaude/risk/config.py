"""Versioned risk configuration (`configs/risk.yaml`, SIMULATION §6).

Loading is strict: unknown keys, missing keys, non-finite or out-of-range values raise
`RiskConfigError` -- a risk engine without a valid configuration cannot start (fail closed).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from tsclaude.contracts.events import canonical_json
from tsclaude.sim.money import ONE, ZERO, dstr


class RiskConfigError(ValueError):
    """The risk configuration is missing, malformed or out of range."""


@dataclass(frozen=True, slots=True)
class FreshnessConfig:
    require_streams_healthy: bool = True
    max_open_gap_minutes: int = 0
    max_view_age_ms: int = 5_000
    max_bar_age_ms: int = 120_000


@dataclass(frozen=True, slots=True)
class SpreadConfig:
    median_multiple: Decimal = Decimal("2")
    cap_bps: Decimal = Decimal("10")
    median_window_ms: int = 3_600_000
    min_samples: int = 30


@dataclass(frozen=True, slots=True)
class CorrelationConfig:
    max_per_cluster: int = 3
    default_cluster: str = "btc_beta"
    clusters: Mapping[str, str] = field(default_factory=dict)

    def cluster_of(self, symbol: str) -> str:
        return self.clusters.get(symbol, self.default_cluster)


@dataclass(frozen=True, slots=True)
class RiskConfig:
    version: str = "risk-config-v1"
    data_freshness: FreshnessConfig = field(default_factory=FreshnessConfig)
    clock_skew_max_ms: int = 1_000
    spread: SpreadConfig = field(default_factory=SpreadConfig)
    min_rr_net: Decimal = Decimal("1.5")
    require_take_profit: bool = True
    max_risk_per_trade: Decimal = Decimal("0.005")
    max_positions: int = 5
    max_positions_per_symbol: int = 1
    correlation: CorrelationConfig = field(default_factory=CorrelationConfig)
    max_gross_exposure: Decimal = Decimal("1")
    daily_loss_limit: Decimal = Decimal("0.02")
    drawdown_kill_switch: Decimal = Decimal("0.10")

    def __post_init__(self) -> None:
        if not self.version:
            raise RiskConfigError("version is required")
        f = self.data_freshness
        for name, v in (
            ("max_open_gap_minutes", f.max_open_gap_minutes),
            ("max_view_age_ms", f.max_view_age_ms),
            ("max_bar_age_ms", f.max_bar_age_ms),
            ("clock_skew_max_ms", self.clock_skew_max_ms),
            ("spread.median_window_ms", self.spread.median_window_ms),
            ("spread.min_samples", self.spread.min_samples),
        ):
            if v < 0:
                raise RiskConfigError(f"{name} must be >= 0")
        for name, n in (
            ("max_positions", self.max_positions),
            ("max_positions_per_symbol", self.max_positions_per_symbol),
            ("correlation.max_per_cluster", self.correlation.max_per_cluster),
        ):
            if n < 1:
                raise RiskConfigError(f"{name} must be >= 1")
        if self.spread.median_multiple <= 0 or self.spread.cap_bps < 0:
            raise RiskConfigError("spread.median_multiple must be > 0 and cap_bps >= 0")
        if self.min_rr_net <= 0:
            raise RiskConfigError("min_rr_net must be > 0")
        for name, d in (
            ("max_risk_per_trade", self.max_risk_per_trade),
            ("daily_loss_limit", self.daily_loss_limit),
            ("drawdown_kill_switch", self.drawdown_kill_switch),
        ):
            if not ZERO < d < ONE:
                raise RiskConfigError(f"{name} must be in (0, 1)")
        if not ZERO < self.max_gross_exposure <= ONE:
            raise RiskConfigError("max_gross_exposure must be in (0, 1] (no leverage in MVP)")

    def to_payload(self) -> dict[str, Any]:
        f, s, c = self.data_freshness, self.spread, self.correlation
        return {
            "version": self.version,
            "data_freshness": {
                "require_streams_healthy": f.require_streams_healthy,
                "max_open_gap_minutes": f.max_open_gap_minutes,
                "max_view_age_ms": f.max_view_age_ms,
                "max_bar_age_ms": f.max_bar_age_ms,
            },
            "clock_skew_max_ms": self.clock_skew_max_ms,
            "spread": {
                "median_multiple": dstr(s.median_multiple),
                "cap_bps": dstr(s.cap_bps),
                "median_window_ms": s.median_window_ms,
                "min_samples": s.min_samples,
            },
            "min_rr_net": dstr(self.min_rr_net),
            "require_take_profit": self.require_take_profit,
            "max_risk_per_trade": dstr(self.max_risk_per_trade),
            "max_positions": self.max_positions,
            "max_positions_per_symbol": self.max_positions_per_symbol,
            "correlation": {
                "max_per_cluster": c.max_per_cluster,
                "default_cluster": c.default_cluster,
                "clusters": dict(sorted(c.clusters.items())),
            },
            "max_gross_exposure": dstr(self.max_gross_exposure),
            "daily_loss_limit": dstr(self.daily_loss_limit),
            "drawdown_kill_switch": dstr(self.drawdown_kill_switch),
        }

    @property
    def config_hash(self) -> str:
        return hashlib.sha256(canonical_json(self.to_payload()).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- loading
def _take(raw: Mapping[str, Any], keys: set[str], where: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise RiskConfigError(f"{where} must be a mapping")
    unknown = set(raw) - keys
    missing = keys - set(raw)
    if unknown:
        raise RiskConfigError(f"{where}: unknown keys {sorted(unknown)}")
    if missing:
        raise RiskConfigError(f"{where}: missing keys {sorted(missing)}")
    return dict(raw)


def _dec(v: Any, name: str) -> Decimal:
    if isinstance(v, bool) or not isinstance(v, str | int):
        raise RiskConfigError(f"{name} must be a quoted decimal string or an integer")
    try:
        d = Decimal(v)
    except InvalidOperation as exc:
        raise RiskConfigError(f"{name}: not a decimal: {v!r}") from exc
    if not d.is_finite():
        raise RiskConfigError(f"{name} must be finite")
    return d


def _int(v: Any, name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise RiskConfigError(f"{name} must be an integer")
    return v


def _bool(v: Any, name: str) -> bool:
    if not isinstance(v, bool):
        raise RiskConfigError(f"{name} must be true or false")
    return v


def config_from_mapping(raw: Mapping[str, Any]) -> RiskConfig:
    top = _take(
        raw,
        {
            "version",
            "data_freshness",
            "clock_skew_max_ms",
            "spread",
            "min_rr_net",
            "require_take_profit",
            "max_risk_per_trade",
            "max_positions",
            "max_positions_per_symbol",
            "correlation",
            "max_gross_exposure",
            "daily_loss_limit",
            "drawdown_kill_switch",
        },
        "risk config",
    )
    f = _take(
        top["data_freshness"],
        {"require_streams_healthy", "max_open_gap_minutes", "max_view_age_ms", "max_bar_age_ms"},
        "data_freshness",
    )
    s = _take(
        top["spread"], {"median_multiple", "cap_bps", "median_window_ms", "min_samples"}, "spread"
    )
    c = _take(top["correlation"], {"max_per_cluster", "default_cluster", "clusters"}, "correlation")
    clusters = c["clusters"] or {}
    if not isinstance(clusters, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in clusters.items()
    ):
        raise RiskConfigError("correlation.clusters must map symbol -> cluster name")
    if not isinstance(top["version"], str) or not isinstance(c["default_cluster"], str):
        raise RiskConfigError("version and correlation.default_cluster must be strings")
    return RiskConfig(
        version=top["version"],
        data_freshness=FreshnessConfig(
            require_streams_healthy=_bool(f["require_streams_healthy"], "require_streams_healthy"),
            max_open_gap_minutes=_int(f["max_open_gap_minutes"], "max_open_gap_minutes"),
            max_view_age_ms=_int(f["max_view_age_ms"], "max_view_age_ms"),
            max_bar_age_ms=_int(f["max_bar_age_ms"], "max_bar_age_ms"),
        ),
        clock_skew_max_ms=_int(top["clock_skew_max_ms"], "clock_skew_max_ms"),
        spread=SpreadConfig(
            median_multiple=_dec(s["median_multiple"], "spread.median_multiple"),
            cap_bps=_dec(s["cap_bps"], "spread.cap_bps"),
            median_window_ms=_int(s["median_window_ms"], "spread.median_window_ms"),
            min_samples=_int(s["min_samples"], "spread.min_samples"),
        ),
        min_rr_net=_dec(top["min_rr_net"], "min_rr_net"),
        require_take_profit=_bool(top["require_take_profit"], "require_take_profit"),
        max_risk_per_trade=_dec(top["max_risk_per_trade"], "max_risk_per_trade"),
        max_positions=_int(top["max_positions"], "max_positions"),
        max_positions_per_symbol=_int(top["max_positions_per_symbol"], "max_positions_per_symbol"),
        correlation=CorrelationConfig(
            max_per_cluster=_int(c["max_per_cluster"], "correlation.max_per_cluster"),
            default_cluster=c["default_cluster"],
            clusters=dict(clusters),
        ),
        max_gross_exposure=_dec(top["max_gross_exposure"], "max_gross_exposure"),
        daily_loss_limit=_dec(top["daily_loss_limit"], "daily_loss_limit"),
        drawdown_kill_switch=_dec(top["drawdown_kill_switch"], "drawdown_kill_switch"),
    )


def load_risk_config(path: Path | str) -> RiskConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RiskConfigError(f"cannot read risk config {path}: {exc}") from exc
    if raw is None:
        raise RiskConfigError(f"risk config {path} is empty")
    return config_from_mapping(raw)
