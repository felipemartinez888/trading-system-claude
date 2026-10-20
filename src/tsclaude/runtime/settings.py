"""Application settings from environment variables (prefix TSC_) with safe defaults.

Secrets (e.g. ANTHROPIC_API_KEY) are read only where needed and are never part of this model,
so they can never be logged or serialized with the settings.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    data_dir: Path = Field(default=Path("data"))
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)
    open_browser: bool = True
    log_level: str = "INFO"
    heartbeat_interval_s: float = Field(default=5.0, gt=0)

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        if v not in _LOOPBACK:
            raise ValueError("the API must bind to a loopback address only (security rule)")
        return v

    @field_validator("log_level")
    @classmethod
    def _level(cls, v: str) -> str:
        level = v.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError(f"invalid log level {v!r}")
        return level

    @property
    def state_db_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        source = os.environ if env is None else env
        raw: dict[str, object] = {}
        mapping = {
            "TSC_DATA_DIR": "data_dir",
            "TSC_HOST": "host",
            "TSC_PORT": "port",
            "TSC_OPEN_BROWSER": "open_browser",
            "TSC_LOG_LEVEL": "log_level",
            "TSC_HEARTBEAT_INTERVAL_S": "heartbeat_interval_s",
        }
        for env_key, field in mapping.items():
            if env_key in source:
                raw[field] = source[env_key]
        return cls.model_validate(raw)
