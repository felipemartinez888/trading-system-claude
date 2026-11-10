"""Key-presence check, the real-client stub (never exercised against an API) and the status
snapshot behind GET /api/agents/status. No network, and the fake key below is not a key."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from agent_helpers import DECISION_MS, make_config, make_gate, make_inputs, make_signal

from tsclaude.agents.budget import CallOutcome, CostBasis, CostEntry, CostLedger
from tsclaude.agents.config import AgentMode
from tsclaude.agents.gate import LEDGER_FILE
from tsclaude.agents.real_client import AnthropicClientStub, api_key_present, build_client
from tsclaude.agents.status import agent_status
from tsclaude.contracts.signals import AgentReviewState

REPO_CONFIG = Path(__file__).resolve().parents[3] / "configs" / "agents.yaml"
FAKE = "sk-test-not-a-real-key-000"


def test_mode_off_builds_no_client_even_with_a_key(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"ANTHROPIC_API_KEY={FAKE}\n", encoding="utf-8")
    assert build_client(make_config(AgentMode.OFF), env={"ANTHROPIC_API_KEY": FAKE}) is None
    assert build_client(make_config(AgentMode.OFF), env={}, dotenv=dotenv) is None


def test_key_presence_from_env_or_dotenv(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    assert not api_key_present({}, dotenv)  # file missing
    dotenv.write_text("# comment\nOTHER=1\nANTHROPIC_API_KEY=\n", encoding="utf-8")
    assert not api_key_present({}, dotenv)  # empty value
    dotenv.write_text(f'export ANTHROPIC_API_KEY="{FAKE}"\n', encoding="utf-8")
    assert api_key_present({}, dotenv)
    assert api_key_present({"ANTHROPIC_API_KEY": FAKE}, None)
    assert not api_key_present({"ANTHROPIC_API_KEY": "  "}, None)


def test_stub_degrades_to_ai_unavailable_and_never_shows_the_key() -> None:
    cfg = make_config(AgentMode.CONFIRMED)
    assert build_client(cfg, env={}) is None
    client = build_client(cfg, env={"ANTHROPIC_API_KEY": FAKE})
    assert isinstance(client, AnthropicClientStub)
    assert FAKE not in repr(client)
    gate = make_gate(None)
    gate.client = client
    out = gate.review(make_signal(), risk_passed=True, inputs=make_inputs)
    assert out.state is AgentReviewState.AI_UNAVAILABLE
    assert out.verdict is None
    assert FAKE not in repr(out)
    assert FAKE not in str(gate.snapshot())


def test_status_from_repo_config_is_off_with_empty_ledger(tmp_path: Path) -> None:
    snap = agent_status(REPO_CONFIG, tmp_path, DECISION_MS)
    assert snap["mode"] == "off"
    assert snap["config_ok"] is True
    assert snap["daily_cap_usd"] == "1.00000000"
    assert snap["ledger"]["calls_today"] == 0
    assert snap["runtime_wired"] is False
    assert snap["prompts"]["analyst"].startswith("analyst-v1@")


def test_status_reports_spend_today_and_invalid_config(tmp_path: Path) -> None:
    ledger = CostLedger(tmp_path / LEDGER_FILE)
    for day_offset, cost in ((0, "0.0123"), (-1, "0.5")):
        at = DECISION_MS + day_offset * 86_400_000
        ledger.append(
            CostEntry(
                at_ms=at,
                day=at // 86_400_000,
                review_key="k",
                signal_id="s",
                role="analyst",
                attempt=0,
                model="m",
                input_tokens=1,
                output_tokens=1,
                cached_input_tokens=0,
                cost_usd=Decimal(cost),
                cost_basis=CostBasis.MEASURED,
                latency_ms=5,
                outcome=CallOutcome.OK,
            )
        )
    snap = agent_status(REPO_CONFIG, tmp_path, DECISION_MS)
    assert snap["ledger"]["calls_today"] == 1
    assert snap["ledger"]["spent_today_usd"] == "0.01230000"
    assert snap["ledger"]["calls_total"] == 2

    bad = tmp_path / "bad.yaml"
    bad.write_text("mode: confirmed\nunknown: 1\n", encoding="utf-8")
    snap = agent_status(bad, tmp_path, DECISION_MS)
    assert snap["mode"] == "off"
    assert snap["config_ok"] is False
    assert snap["config_errors"]

    (tmp_path / LEDGER_FILE).write_text("garbage\n", encoding="utf-8")
    assert "unreadable" in agent_status(REPO_CONFIG, tmp_path, DECISION_MS)["ledger"]["error"]
