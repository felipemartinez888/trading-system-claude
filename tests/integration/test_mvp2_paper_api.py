"""MVP-2 paper endpoints (P7): provenance labels, the token-protected new-session action, and the
paper state in `/api/status`. Fake market data, no network; nothing here can place an order."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from api_testkit import BASE_URL, PORT, TOKEN
from api_testkit import T0 as _T0
from fastapi.testclient import TestClient
from m1t2_fakes import FakeFactory

from tsclaude.api.app import create_app
from tsclaude.api.context import AppContext
from tsclaude.contracts.timeutil import ManualClock
from tsclaude.runtime.paper_runtime import AUDIT_FILE, PaperRuntime, PaperSettings
from tsclaude.runtime.supervisor import MonitoringSupervisor
from tsclaude.signals.demo import DemoDetectorConfig
from tsclaude.sim.paper import PaperConfig
from tsclaude.sim.testing import spot_spec, usdm_spec
from tsclaude.store.eventstore import EventStore

T0 = _T0 - _T0 % 60_000
ORIGIN = f"http://127.0.0.1:{PORT}"
AUTH = {"X-TSC-Token": TOKEN, "Origin": ORIGIN}


def _app(tmp_path: Path, *, enabled: bool = True, paper: bool = True) -> TestClient:
    store = EventStore(tmp_path / "state.db")
    clock = ManualClock(T0)
    supervisor = MonitoringSupervisor(
        store, clock, service_factory=FakeFactory(), demo=DemoDetectorConfig(enabled=False)
    )
    if paper:
        supervisor.attach_paper(
            PaperRuntime(
                PaperSettings(enabled=enabled, poll_s=0.01),
                store,
                clock,
                config_factory=lambda: PaperConfig(
                    instruments=(spot_spec(), usdm_spec()), starting_equity=Decimal(10_000)
                ),
                audit_path=tmp_path / AUDIT_FILE,
            )
        )
    ctx = AppContext(
        store=store,
        clock=clock,
        app_version="test",
        port=PORT,
        heartbeat_interval_s=5.0,
        token=TOKEN,
        supervisor=supervisor,
    )
    ctx.start()
    static = tmp_path / "no-static"
    static.mkdir(exist_ok=True)
    app = create_app(ctx, static_dir=static, heartbeat=False, push_interval_s=None)
    return TestClient(app, base_url=BASE_URL)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with _app(tmp_path) as c:
        yield c


def _new(client: TestClient, note: str = "clean start", rid: str = "req-1000", **h: Any) -> Any:
    headers = h.get("headers", AUTH)
    return client.post(
        "/api/paper/session/new", json={"note": note, "request_id": rid}, headers=headers
    )


def test_p7_status_and_values_are_labelled(client: TestClient) -> None:
    status = client.get("/api/paper/status").json()["status"]
    assert status["provenance"] == "SYSTEM"
    assert status["value"]["state"] == "IDLE"
    assert any("not guaranteed" in s for s in status["value"]["limitations"])
    books = client.get("/api/paper/books").json()["books"]
    assert books["provenance"] == "UNAVAILABLE"  # no session open while monitoring is stopped
    assert client.get("/api/status").json()["paper"]["value"].startswith("IDLE")

    assert client.post("/api/monitoring/start", headers=AUTH).status_code == 200
    status = client.get("/api/paper/status").json()["status"]
    assert status["value"]["state"] == "RUNNING"
    assert status["value"]["session_id"] == "paper"
    books = client.get("/api/paper/books").json()["books"]
    assert books["provenance"] == "SIMULATED"
    assert set(books["value"]["books"]) == {"baseline", "shadow", "benchmark"}
    fills = client.get("/api/paper/fills", params={"limit": 5}).json()["fills"]
    assert fills["provenance"] == "SIMULATED"
    assert fills["value"] == []
    assert client.get("/api/status").json()["paper"]["value"] == "RUNNING"

    assert client.post("/api/monitoring/stop", headers=AUTH).status_code == 200
    books = client.get("/api/paper/books").json()["books"]
    assert books["provenance"] == "STALE"  # last known values once paper stopped
    assert books["as_of_ms"] is not None


def test_p7_new_session_requires_token_note_and_a_stopped_paper(
    client: TestClient, tmp_path: Path
) -> None:
    assert _new(client, headers={"Origin": ORIGIN}).status_code == 403  # no token
    assert _new(client, note="").status_code == 422
    assert _new(client, rid="short").status_code == 422
    assert client.post("/api/monitoring/start", headers=AUTH).status_code == 200
    assert _new(client).status_code == 409  # refused while paper is RUNNING
    assert client.post("/api/monitoring/stop", headers=AUTH).status_code == 200
    first = _new(client)
    assert first.status_code == 200
    body = first.json()
    assert body["created"] is True
    assert body["previous_session_id"] == "paper"
    assert body["new_session_id"].startswith("paper-")
    again = _new(client).json()
    assert again["created"] is False
    assert again["new_session_id"] == body["new_session_id"]
    assert _new(client, note="different").status_code == 409
    assert len((tmp_path / AUDIT_FILE).read_text(encoding="utf-8").splitlines()) == 1
    status = client.get("/api/paper/status").json()["status"]["value"]
    assert status["next_session_id"] == body["new_session_id"]


def test_p7_disabled_and_unconfigured_paper(tmp_path: Path) -> None:
    with _app(tmp_path / "a", enabled=False) as c:
        assert c.get("/api/paper/status").json()["status"]["value"]["state"] == "DISABLED"
        assert c.post("/api/monitoring/start", headers=AUTH).status_code == 200
        assert c.get("/api/paper/status").json()["status"]["value"]["state"] == "DISABLED"
        assert _new(c).status_code == 409
    with _app(tmp_path / "b", paper=False) as c:
        assert c.get("/api/paper/status").json()["status"]["provenance"] == "UNAVAILABLE"
        assert c.get("/api/status").json()["paper"]["provenance"] == "UNAVAILABLE"
        assert _new(c).status_code == 409
