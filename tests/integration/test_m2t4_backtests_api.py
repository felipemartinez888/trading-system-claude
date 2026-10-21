"""`GET /api/backtests` and `/api/backtests/{run_id}` (M2-T4) through FastAPI's TestClient."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from api_testkit import BASE_URL, ContextFactory  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from tsclaude.api.app import create_app  # noqa: E402
from tsclaude.contracts.events import canonical_json  # noqa: E402

RUN_ID = "bt-20261021T000000Z-abcdef0123"


def _write_run(runs: Path, *, tamper: bool = False) -> None:
    d = runs / RUN_ID
    d.mkdir(parents=True)
    metrics = {
        "evidence_grade": "C",
        "n_trials": 3,
        "trades": {"n": 12, "win_rate": 0.5, "win_rate_ci": {"lower": 0.2, "upper": 0.8}},
        "certification": {"p_star": 0.6, "margin_lcb": -0.3},
    }
    text = canonical_json(metrics)
    (d / "metrics.json").write_text(text + "\n", encoding="utf-8")
    manifest = {
        "run_id": RUN_ID,
        "created_at": "2026-10-21T00:00:00.000Z",
        "strategy": {"strategy_id": "bracket_reference", "version": "1.0.0", "params_hash": "x"},
        "symbols": ["spot:BTCUSDT"],
        "range": {"from": "2024-03-03T00:00:00.000Z", "to": "2024-03-15T00:00:00.000Z"},
        "segment": "development",
        "outputs": {"metrics_sha256": hashlib.sha256(text.encode()).hexdigest()},
    }
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if tamper:
        (d / "metrics.json").write_text(text.replace('"C"', '"A"') + "\n", encoding="utf-8")


@pytest.fixture
def client(make_context: ContextFactory, tmp_path: Path) -> Iterator[TestClient]:
    static = tmp_path / "no-static"
    static.mkdir()
    ctx = make_context()
    with TestClient(create_app(ctx, static_dir=static, heartbeat=False), base_url=BASE_URL) as c:
        yield c


def test_list_and_get_backtests(client: TestClient, db_path: Path) -> None:
    assert client.get("/api/backtests").json() == {"provenance": "BACKTEST", "runs": []}
    _write_run(db_path.parent / "runs")
    body = client.get("/api/backtests").json()
    assert body["provenance"] == "BACKTEST"
    (run,) = body["runs"]
    assert run["provenance"] == "BACKTEST"
    assert run["value"]["run_id"] == RUN_ID
    assert run["value"]["n_trials"] == 3
    assert run["value"]["metrics_verified"] is True
    one = client.get(f"/api/backtests/{RUN_ID}").json()
    for part in ("summary", "manifest", "metrics"):
        assert one[part]["provenance"] == "BACKTEST"
    assert one["metrics"]["value"]["evidence_grade"] == "C"


def test_tampered_metrics_are_flagged(client: TestClient, db_path: Path) -> None:
    _write_run(db_path.parent / "runs", tamper=True)
    one = client.get(f"/api/backtests/{RUN_ID}").json()
    assert one["summary"]["value"]["metrics_verified"] is False


@pytest.mark.parametrize("bad", ["bt-missing", "..", "not-a-run", "bt-..%2F..%2Fstate.db"])
def test_unknown_or_unsafe_ids_are_404(client: TestClient, bad: str) -> None:
    assert client.get(f"/api/backtests/{bad}").status_code == 404
