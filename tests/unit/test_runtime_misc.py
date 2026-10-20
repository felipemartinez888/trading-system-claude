from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from tsclaude.cli import main
from tsclaude.runtime.logging import REDACTED, JsonFormatter, redact, session_id_var
from tsclaude.runtime.settings import Settings


def test_settings_defaults_and_env() -> None:
    s = Settings.from_env({})
    assert s.host == "127.0.0.1"
    s2 = Settings.from_env({"TSC_PORT": "9000", "TSC_DATA_DIR": "x", "TSC_LOG_LEVEL": "debug"})
    assert (s2.port, s2.data_dir, s2.log_level) == (9000, Path("x"), "DEBUG")


def test_settings_refuse_non_loopback_host() -> None:
    with pytest.raises(ValidationError, match="loopback"):
        Settings.from_env({"TSC_HOST": "0.0.0.0"})


def test_redaction() -> None:
    assert redact("x", key="api_key") == REDACTED
    assert redact({"Authorization": "Bearer z", "n": 1}) == {"Authorization": REDACTED, "n": 1}
    assert "sk-ant-" not in redact("token=sk-ant-abc123_DEF")


def test_json_formatter_includes_session_and_redacts() -> None:
    token = session_id_var.set("S1")
    try:
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello %s", ("world",), None)
        record.anthropic_api_key = "sk-ant-secret"
        record.symbol = "BTCUSDT"
        out = json.loads(JsonFormatter().format(record))
    finally:
        session_id_var.reset(token)
    assert out["msg"] == "hello world"
    assert out["session_id"] == "S1"
    assert out["anthropic_api_key"] == REDACTED
    assert out["symbol"] == "BTCUSDT"
    assert out["ts"].endswith("Z")


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip()


def test_cli_verify_chain_without_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TSC_DATA_DIR", str(tmp_path))
    assert main(["verify-chain"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
