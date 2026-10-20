"""Market-data configuration: universe and data-quality budgets (configs/*.yaml).

The models validate strictly (unknown keys rejected) so that a typo in a budget fails at startup
instead of silently using a default. `from_mapping` needs no YAML library; `load_config` parses the
YAML files with PyYAML (imported lazily).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tsclaude.contracts.market import Market
from tsclaude.marketdata.types import StreamKind

_STRICT = ConfigDict(frozen=True, extra="forbid")


class UniverseConfig(BaseModel):
    model_config = _STRICT

    version: int = Field(ge=0)
    snapshot_date: str
    rule: str
    spot: tuple[str, ...]
    usdm: tuple[str, ...]

    @field_validator("spot", "usdm")
    @classmethod
    def _symbols(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for s in v:
            if not s or s != s.upper() or not s.isalnum():
                raise ValueError(f"symbol {s!r} must be upper-case alphanumeric (e.g. BTCUSDT)")
        if len(set(v)) != len(v):
            raise ValueError("duplicate symbols")
        return v

    def symbols(self, market: Market) -> tuple[str, ...]:
        return self.spot if market is Market.SPOT else self.usdm

    def pairs(self) -> list[tuple[Market, str]]:
        return [(Market.SPOT, s) for s in self.spot] + [(Market.USDM, s) for s in self.usdm]


class FreshnessConfig(BaseModel):
    """Freshness budgets (DATA_ARCHITECTURE §3), seconds."""

    model_config = _STRICT

    kline_1m_s: float = Field(default=75.0, gt=0)  # since last closed bar
    book_ticker_s: float = Field(default=10.0, gt=0)  # since last message
    mark_price_s: float = Field(default=5.0, gt=0)  # since last message
    tick_interval_s: float = Field(default=1.0, gt=0)

    def budget_ms(self, kind: StreamKind) -> int:
        seconds = {
            StreamKind.KLINE_1M: self.kline_1m_s,
            StreamKind.BOOK_TICKER: self.book_ticker_s,
            StreamKind.MARK_PRICE: self.mark_price_s,
        }[kind]
        return int(seconds * 1000)


class WatermarkConfig(BaseModel):
    model_config = _STRICT

    grace_s: float = Field(default=5.0, ge=0)
    check_interval_s: float = Field(default=1.0, gt=0)
    gap_retry_interval_s: float = Field(default=60.0, gt=0)
    gap_retry_max_attempts: int = Field(default=5, ge=0)


class WsConfig(BaseModel):
    model_config = _STRICT

    recycle_after_s: float = Field(default=23 * 3600, gt=0)
    first_message_timeout_s: float = Field(default=30.0, gt=0)
    recycle_retry_s: float = Field(default=300.0, gt=0)
    backoff_base_s: float = Field(default=1.0, gt=0)
    backoff_cap_s: float = Field(default=60.0, gt=0)
    stable_after_s: float = Field(default=30.0, ge=0)  # reset back-off after this long connected
    fallback_after_failures: int = Field(default=3, ge=1)
    connect_budget: int = Field(default=30, ge=1)  # attempts per window, per IP (Binance: 300/5 min)
    connect_budget_window_s: float = Field(default=300.0, gt=0)
    control_msgs_per_s_spot: int = Field(default=4, ge=1, le=4)  # Binance limit 5/s
    control_msgs_per_s_futures: int = Field(default=8, ge=1, le=8)  # Binance limit 10/s
    max_streams_per_connection: int = Field(default=200, ge=1, le=1024)
    open_timeout_s: float = Field(default=10.0, gt=0)
    ping_interval_s: float | None = Field(default=20.0, gt=0)
    ping_timeout_s: float | None = Field(default=20.0, gt=0)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.backoff_cap_s < self.backoff_base_s:
            raise ValueError("backoff_cap_s must be >= backoff_base_s")
        if self.recycle_after_s >= 24 * 3600:
            raise ValueError("recycle_after_s must be below Binance's 24 h connection lifetime")
        return self


class RestConfig(BaseModel):
    model_config = _STRICT

    timeout_s: float = Field(default=10.0, gt=0)
    max_retries: int = Field(default=3, ge=0)
    weight_budget_fraction: float = Field(default=0.5, gt=0, le=1)
    # Safety cap until exchangeInfo has seeded the weight limiter (not a guess of Binance's limit).
    unseeded_requests_per_s: int = Field(default=2, ge=1)
    # /fapi/v1/fundingRate has its own documented 500 / 5 min / IP budget; we use a fraction.
    funding_rate_requests_per_5min: int = Field(default=250, ge=1, le=500)


class BackfillConfig(BaseModel):
    model_config = _STRICT

    lookback_days: float = Field(default=3.0, gt=0)
    max_backfill_days: float = Field(default=30.0, gt=0)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.max_backfill_days < self.lookback_days:
            raise ValueError("max_backfill_days must be >= lookback_days")
        return self


class ClockConfig(BaseModel):
    model_config = _STRICT

    sample_interval_s: float = Field(default=300.0, gt=0)
    samples_per_round: int = Field(default=5, ge=1)
    skew_threshold_ms: int = Field(default=1000, ge=1)


class EndpointsConfig(BaseModel):
    """Public endpoints. Tests point these at 127.0.0.1; production uses the defaults."""

    model_config = _STRICT

    spot_rest: tuple[str, ...] = ("https://api.binance.com", "https://data-api.binance.vision")
    usdm_rest: tuple[str, ...] = ("https://fapi.binance.com",)
    spot_ws: tuple[str, ...] = (
        "wss://stream.binance.com:9443",
        "wss://data-stream.binance.vision",
    )
    usdm_ws_market: tuple[str, ...] = ("wss://fstream.binance.com/market",)
    usdm_ws_public: tuple[str, ...] = ("wss://fstream.binance.com/public",)

    @field_validator("spot_rest", "usdm_rest", "spot_ws", "usdm_ws_market", "usdm_ws_public")
    @classmethod
    def _non_empty(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("at least one endpoint is required")
        return tuple(u.rstrip("/") for u in v)


class DataQualityConfig(BaseModel):
    model_config = _STRICT

    version: int = Field(default=0, ge=0)
    freshness: FreshnessConfig = FreshnessConfig()
    watermark: WatermarkConfig = WatermarkConfig()
    ws: WsConfig = WsConfig()
    rest: RestConfig = RestConfig()
    backfill: BackfillConfig = BackfillConfig()
    clock: ClockConfig = ClockConfig()
    endpoints: EndpointsConfig = EndpointsConfig()


class MarketDataConfig(BaseModel):
    model_config = _STRICT

    universe: UniverseConfig
    quality: DataQualityConfig = DataQualityConfig()

    @classmethod
    def from_mapping(
        cls, universe: Mapping[str, Any], quality: Mapping[str, Any] | None = None
    ) -> MarketDataConfig:
        return cls.model_validate({"universe": dict(universe), "quality": dict(quality or {})})


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml  # PyYAML; lazily imported so the pure modules stay dependency-free

    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def load_config(
    universe_path: Path | str = Path("configs/universe.yaml"),
    quality_path: Path | str = Path("configs/data_quality.yaml"),
) -> MarketDataConfig:
    """Load and validate both files. Missing or invalid files raise (fail closed)."""
    return MarketDataConfig.from_mapping(
        _load_yaml(Path(universe_path)), _load_yaml(Path(quality_path))
    )
