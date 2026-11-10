"""Agent configuration (``configs/agents.yaml``) and its fail-closed loader.

Any problem with the file (missing, unreadable, not a mapping, unknown key, out-of-range value)
yields the *off* configuration plus a list of human-readable errors. Agents therefore never run
on a configuration nobody wrote on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

DEFAULT_CONFIG_PATH = Path("configs/agents.yaml")


class AgentMode(StrEnum):
    OFF = "off"  # deterministic only; no agent code runs on any signal path
    CONFIRMED = "confirmed"  # review CONFIRMED signals that passed the risk gate, within budget
    RESEARCH = "research"  # offline review of REPLAY/BACKTEST signals, labeled indicative


class Pricing(BaseModel):
    """USD per million tokens. Decimal strings in YAML: exact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_per_mtok: Decimal = Field(ge=0)
    output_per_mtok: Decimal = Field(ge=0)
    cached_input_per_mtok: Decimal = Field(ge=0)


class PacketConfig(BaseModel):
    """What the evidence packet builder may include. Names outside these allowlists are dropped
    (and listed by name only in the packet's ``masked`` section)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_bars: int = Field(default=48, ge=4, le=200)
    allowed_features: frozenset[str] = frozenset()
    allowed_health: frozenset[str] = frozenset()
    allowed_risk: frozenset[str] = frozenset()


class AgentsConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    mode: AgentMode = AgentMode.OFF
    model: str = Field(min_length=1)
    pricing: Pricing
    daily_cap_usd: Decimal = Field(gt=0, le=Decimal(100))
    per_call_timeout_s: float = Field(default=30.0, gt=0, le=120)
    max_retries: int = Field(default=1, ge=0, le=1)  # "one bounded retry"
    max_output_tokens: int = Field(default=1024, ge=64, le=4096)
    breaker_failure_threshold: int = Field(default=3, ge=1, le=20)
    breaker_cooldown_s: float = Field(default=600.0, gt=0)
    packet: PacketConfig = PacketConfig()

    @model_validator(mode="after")
    def _priced_when_on(self) -> Self:
        # zero prices would make every call free and the cap meaningless
        p = self.pricing
        if (
            self.mode is not AgentMode.OFF
            and min(p.input_per_mtok, p.output_per_mtok, p.cached_input_per_mtok) <= 0
        ):
            raise ValueError("pricing must be > 0 unless mode is off")
        return self


# Used whenever the file cannot be trusted: mode off, with harmless budget values.
OFF_CONFIG = AgentsConfig(
    version="fallback-off",
    mode=AgentMode.OFF,
    model="none",
    pricing=Pricing(
        input_per_mtok=Decimal(0), output_per_mtok=Decimal(0), cached_input_per_mtok=Decimal(0)
    ),
    daily_cap_usd=Decimal(1),
)


@dataclass(frozen=True)
class LoadedConfig:
    config: AgentsConfig
    source: str
    errors: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return not self.errors


def parse_config(raw: Any, source: str = "<memory>") -> LoadedConfig:
    if not isinstance(raw, dict):
        return LoadedConfig(OFF_CONFIG, source, ("config is not a mapping; agents forced off",))
    try:
        cfg = AgentsConfig.model_validate(raw)
    except ValidationError as exc:
        errors = tuple(
            f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()
        )
        return LoadedConfig(OFF_CONFIG, source, (*errors, "invalid config; agents forced off"))
    return LoadedConfig(cfg, source)


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys (PyYAML keeps the last one silently)."""


def _construct_unique_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    keys: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node)
        if key in keys:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        keys.add(key)
    return loader.construct_mapping(node)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> LoadedConfig:
    """Load and validate; never raises. Invalid or missing -> mode off with errors."""
    source = str(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return LoadedConfig(
            OFF_CONFIG, source, (f"cannot read config: {type(exc).__name__}; agents forced off",)
        )
    try:
        raw = yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        problem = getattr(exc, "problem", None) or type(exc).__name__
        return LoadedConfig(OFF_CONFIG, source, (f"YAML error: {problem}; agents forced off",))
    return parse_config(raw, source)
