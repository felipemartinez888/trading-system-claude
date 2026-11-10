"""`GET /api/agents/status` (M5) through FastAPI's TestClient: read-only, SYSTEM-labelled.

fastapi is not installed in every container: this module is skipped there and runs in CI.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from api_testkit import BASE_URL, ContextFactory
from fastapi.testclient import TestClient

from tsclaude.api.app import create_app


@pytest.fixture
def client(make_context: ContextFactory, tmp_path: Path) -> Iterator[TestClient]:
    static = tmp_path / "no-static"
    static.mkdir()
    ctx = make_context()
    with TestClient(create_app(ctx, static_dir=static, heartbeat=False), base_url=BASE_URL) as c:
        yield c


def test_agents_status_is_system_and_reports_mode(client: TestClient) -> None:
    body = client.get("/api/agents/status").json()
    status = body["status"]
    assert body["generated_at"].endswith("Z")
    if status["provenance"] == "UNAVAILABLE":  # fail closed, with a reason
        assert status["value"] is None
        assert status["reason"]
        return
    assert status["provenance"] == "SYSTEM"
    value = status["value"]
    # the repository config is `off`; a missing config (other cwd) is also reported as off
    assert value["mode"] == "off"
    assert value["runtime_wired"] is False
    assert "ANTHROPIC_API_KEY" not in str(body)


def test_agents_status_is_read_only(client: TestClient) -> None:
    assert client.post("/api/agents/status").status_code == 405
