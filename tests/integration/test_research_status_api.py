"""`GET /api/research/status` (MVP) through FastAPI's TestClient: read-only, SYSTEM-labelled."""

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


def test_research_status_reports_no_validated_strategy(client: TestClient) -> None:
    body = client.get("/api/research/status").json()
    status = body["status"]
    assert status["provenance"] == "SYSTEM"
    value = status["value"]
    assert value["state"] == "NO_VALIDATED_STRATEGY"
    assert value["validated"] == 0
    tiers = {s["strategy_id"]: s["tier"] for s in value["strategies"]}
    assert tiers["bracket_reference"] == "REFERENCE"
    assert "NO VALIDATED STRATEGY" in value["statement"]


def test_research_status_is_read_only(client: TestClient) -> None:
    assert client.post("/api/research/status").status_code == 405
