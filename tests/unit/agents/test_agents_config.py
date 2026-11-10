"""configs/agents.yaml and the fail-closed loader."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tsclaude.agents.config import AgentMode, load_config, parse_config

REPO = Path(__file__).resolve().parents[3]
REPO_CONFIG = REPO / "configs" / "agents.yaml"


def test_repo_config_is_valid_and_off_with_d03_budget() -> None:
    loaded = load_config(REPO_CONFIG)
    assert loaded.ok, loaded.errors
    cfg = loaded.config
    assert cfg.mode is AgentMode.OFF
    assert cfg.daily_cap_usd == Decimal(1)  # D-03
    assert cfg.per_call_timeout_s == 30  # SIGNALS_AND_AGENTS §4
    assert cfg.max_retries == 1
    assert cfg.version == "agents-config-v1"


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "agents.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def _valid_text() -> str:
    return REPO_CONFIG.read_text(encoding="utf-8").replace('mode: "off"', 'mode: "confirmed"')


def test_valid_confirmed_config_loads(tmp_path: Path) -> None:
    loaded = load_config(_write(tmp_path, _valid_text()))
    assert loaded.ok
    assert loaded.config.mode is AgentMode.CONFIRMED


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('mode: "confirmed"', "mode: off"),  # YAML 1.1 boolean false, not the string
        ('mode: "confirmed"', 'mode: "on"'),
        ('daily_cap_usd: "1"', 'daily_cap_usd: "0"'),
        ('daily_cap_usd: "1"', 'daily_cap_usd: "-1"'),
        ("max_retries: 1", "max_retries: 2"),
        ("per_call_timeout_s: 30", "per_call_timeout_s: 0"),
        ("version: agents-config-v1", "version: agents-config-v1\nunknown_key: 1"),
        ("  max_bars: 48", "  max_bars: 48\n  allow_symbol: true"),
        ('  input_per_mtok: "2"', '  input_per_mtok: "-2"'),
    ],
)
def test_invalid_config_forces_off_and_reports(tmp_path: Path, old: str, new: str) -> None:
    text = _valid_text()
    assert old in text
    loaded = load_config(_write(tmp_path, text.replace(old, new)))
    assert not loaded.ok
    assert loaded.config.mode is AgentMode.OFF
    assert any("forced off" in e for e in loaded.errors)


def test_missing_unparseable_or_non_mapping_config_is_off(tmp_path: Path) -> None:
    for loaded in (
        load_config(tmp_path / "absent.yaml"),
        load_config(_write(tmp_path, "mode: [unclosed")),
        load_config(_write(tmp_path, "- just\n- a list\n")),
        parse_config(None),
    ):
        assert loaded.config.mode is AgentMode.OFF
        assert not loaded.ok
