"""Market-data configuration: universe and data-quality budgets (configs/*.yaml).

The models validate strictly (unknown keys rejected) so that a typo in a budget fails at startup
instead of silently using a default. `from_mapping` needs no YAML library; `load_config` parses the
YAML files with PyYAML (imported lazily).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Self

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
    # reset the supervisor's restart back-off if a connection task ran this long before dying
    supervisor_stable_after_s: float = Field(default=300.0, ge=0)
    fallback_after_failures: int = Field(default=3, ge=1)
    connect_budget: int = Field(
        default=30, ge=1
    )  # attempts per window, per IP (Binance: 300/5 min)
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


type HistoryPolicy = Literal["off", "on_demand", "background"]

MINUTES_PER_DAY = 1440


class BackfillStartupConfig(BaseModel):
    """What a monitoring Start loads, and when the larger history is loaded.

    - `catchup_max_minutes`: the bounded catch-up every Start runs (downtime since the last stored
      bar, capped, newest first). `None` (code default, legacy behavior) means the whole
      `lookback_days` window is the catch-up.
    - `history`: the rest of the `lookback_days` window. `off`: never; `on_demand`: only when a
      consumer needs it (paper with a warm-up strategy) or the operator asks; `background`: right
      after the catch-up on every Start.
    """

    model_config = _STRICT

    catchup_max_minutes: int | None = Field(default=None, ge=1)
    history: HistoryPolicy = "off"


class BackfillRunConfig(BaseModel):
    """Workload limits of one ingestion run (all enforced by `marketdata/backfill.py`)."""

    model_config = _STRICT

    #: minutes per chunk = rows per transaction = rows in flight (one chunk at a time)
    chunk_minutes: int = Field(default=1000, ge=1, le=1000)
    inter_chunk_pause_ms: int = Field(default=25, ge=0, le=60_000)
    #: one REST fetch of a chunk (cancelled when exceeded; nothing is written yet)
    chunk_timeout_s: float = Field(default=30.0, gt=0)
    #: a chunk write slower than this is reported as `write_slow`; it is never abandoned or
    #: overlapped: the run waits for the writer to finish (up to `write_join_timeout_s`)
    write_timeout_s: float = Field(default=30.0, gt=0)
    #: beyond this the writer is declared stuck: the run fails, the store is not closed
    write_join_timeout_s: float = Field(default=90.0, gt=0)
    pair_budget_s: float = Field(default=120.0, gt=0)
    run_budget_s: float = Field(default=600.0, gt=0)
    max_consecutive_chunk_errors: int = Field(default=3, ge=1)
    #: Stop waits this long for the run to finish its current chunk before cancelling a fetch
    stop_grace_s: float = Field(default=10.0, gt=0)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.pair_budget_s > self.run_budget_s:
            raise ValueError("pair_budget_s must be <= run_budget_s")
        if self.write_timeout_s > self.write_join_timeout_s:
            raise ValueError("write_timeout_s must be <= write_join_timeout_s")
        return self


class BackfillConfig(BaseModel):
    model_config = _STRICT

    lookback_days: float = Field(default=3.0, gt=0)
    max_backfill_days: float = Field(default=30.0, gt=0)
    startup: BackfillStartupConfig = BackfillStartupConfig()
    run: BackfillRunConfig = BackfillRunConfig()

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.max_backfill_days < self.lookback_days:
            raise ValueError("max_backfill_days must be >= lookback_days")
        cap = self.startup.catchup_max_minutes
        if cap is not None and cap > self.lookback_days * MINUTES_PER_DAY:
            raise ValueError("startup.catchup_max_minutes must be <= lookback_days in minutes")
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


class DevIngestProfile(BaseModel):
    """`configs/ingest_dev.yaml`: conservative profile for development (`TSC_INGEST_PROFILE=dev`).

    It narrows the live universe to the listed pairs and replaces the whole `backfill` section.
    It never widens the universe: every listed pair must exist in the base universe. Production
    runs without the profile and are unaffected.
    """

    model_config = _STRICT

    version: int = Field(ge=0)
    description: str = ""
    spot: tuple[str, ...] = ()
    usdm: tuple[str, ...] = ()
    backfill: BackfillConfig

    @model_validator(mode="after")
    def _one_pair_at_most(self) -> Self:
        if len(self.spot) + len(self.usdm) < 1:
            raise ValueError("the dev profile needs at least one pair")
        return self


def load_dev_profile(path: Path | str = Path("configs/ingest_dev.yaml")) -> DevIngestProfile:
    return DevIngestProfile.model_validate(_load_yaml(Path(path)))


def apply_dev_profile(config: MarketDataConfig, profile: DevIngestProfile) -> MarketDataConfig:
    """`config` restricted to the profile's pairs, with the profile's backfill section."""
    for market, wanted in ((Market.SPOT, profile.spot), (Market.USDM, profile.usdm)):
        unknown = [s for s in wanted if s not in config.universe.symbols(market)]
        if unknown:
            raise ValueError(f"dev profile pairs not in the {market} universe: {unknown}")
    universe = config.universe.model_copy(update={"spot": profile.spot, "usdm": profile.usdm})
    quality = config.quality.model_copy(update={"backfill": profile.backfill})
    return config.model_copy(update={"universe": universe, "quality": quality})
