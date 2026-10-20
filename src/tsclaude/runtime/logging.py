"""Structured JSON logging with session correlation and secret redaction."""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

from tsclaude.contracts.timeutil import to_iso

session_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tsc_session_id", default=None
)

_SECRET_KEY = re.compile(r"(key|token|secret|password|authorization)", re.IGNORECASE)
_SECRET_VALUE = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")
_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}
REDACTED = "***REDACTED***"


def redact(value: Any, key: str | None = None) -> Any:
    if key is not None and _SECRET_KEY.search(key):
        return REDACTED
    if isinstance(value, str):
        return _SECRET_VALUE.sub(REDACTED, value)
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": to_iso(int(record.created * 1000)),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        sid = session_id_var.get()
        if sid:
            entry["session_id"] = sid
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                entry[k] = redact(v, k)
        if record.exc_info:
            entry["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(entry, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO", logs_dir: Path | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    formatter = JsonFormatter()
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    root.addHandler(console)
    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(logs_dir / "tsclaude.jsonl", encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
