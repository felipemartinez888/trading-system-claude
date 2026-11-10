"""`GET /api/agents/status` (M5) through FastAPI's TestClient: read-only, SYSTEM-labelled.

The router reads ``<cwd>/configs/agents.yaml``; each test runs in a temporary working directory
with a known config, so the asserted state is the configured one. fastapi is not installed in
every container: this module is skipped there and runs in CI.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from api_testkit import BASE_URL, ContextFactory
from fastapi.testclient import TestClient

from tsclaude.api.app import create_app

REPO_CONFIG = Path(__file__).resolve().parents[2] / "configs" / "agents.yaml"

ClientFactory = Callable[[str], TestClient]


@pytest.fixture
def client_with_config(
    make_context: ContextFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[ClientFactory]:
    opened: list[TestClient] = []

    def factory(config_text: str) -> TestClient:
        cwd = tmp_path / "cwd"
        (cwd / "configs").mkdir(parents=True, exist_ok=True)
        (cwd / "configs" / "agents.yaml").write_text(config_text, encoding="utf-8")
        monkeypatch.chdir(cwd)
        static = tmp_path / "no-static"
        static.mkdir(exist_ok=True)
        app = create_app(make_context(), static_dir=static, heartbeat=False)
        c = TestClient(app, base_url=BASE_URL).__enter__()
        opened.append(c)
        return c

    yield factory
    for c in opened:
        c.__exit__(None, None, None)


def _status(client: TestClient) -> dict[str, object]:
    body = client.get("/api/agents/status").json()
    assert body["generated_at"].endswith("Z")
    status = body["status"]
    assert status["provenance"] == "SYSTEM", status
    value: dict[str, object] = status["value"]
    return value


def test_agents_status_reports_the_configured_mode(client_with_config: ClientFactory) -> None:
    text = REPO_CONFIG.read_text(encoding="utf-8").replace('mode: "off"', 'mode: "confirmed"')
    value = _status(client_with_config(text))
    assert value["mode"] == "confirmed"
    assert value["config_ok"] is True
    assert value["daily_cap_usd"] == "1.00000000"
    assert value["runtime_wired"] is True  # M5-3: wired; counts need an agent_gated session
    assert value["runtime"]["provenance"] in ("UNAVAILABLE", "SYSTEM")


def test_agents_status_reports_invalid_config_as_off(client_with_config: ClientFactory) -> None:
    value = _status(client_with_config('mode: "confirmed"\nunknown: 1\n'))
    assert value["mode"] == "off"
    assert value["config_ok"] is False
    assert value["config_errors"]


def test_agents_status_is_read_only(client_with_config: ClientFactory) -> None:
    client = client_with_config(REPO_CONFIG.read_text(encoding="utf-8"))
    assert client.post("/api/agents/status").status_code == 405
