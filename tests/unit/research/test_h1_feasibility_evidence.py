"""The recorded H1-1 findings (docs/research/H1_FEASIBILITY_FINDINGS.md) are pinned to the
owner's count files, kept byte for byte in docs/research/evidence. Counts only: no outcome, no P&L.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from tsclaude.research import power_plan as pp
from tsclaude.research.feasibility import content_hash
from tsclaude.research.walkforward import plan_folds

REPO = Path(__file__).resolve().parents[3]
EVIDENCE = REPO / "docs" / "research" / "evidence"
FILES = {
    "15m": (
        "h1_feasibility_15m_feas-7b59bd4cee56.json",
        "17433166af2b40ce4f2dab38c90251a3aa77a6104cb946452b11c6ef25b556a9",
        1440,
    ),
    "1h": (
        "h1_feasibility_1h_feas-df6adc0d843f.json",
        "33907a13987d3a603787b4ab7823234b66753491f45ef77197660f78a1694d95",
        4320,
    ),
}
ADDED = {"config_id", "draft", "backtest_config_hash", "code"}
MIN = 60_000


def _load(interval: str) -> dict[str, Any]:
    name, sha, _ = FILES[interval]
    data = (EVIDENCE / name).read_bytes()
    assert hashlib.sha256(data).hexdigest() == sha, "evidence bytes changed"
    doc = json.loads(data.decode("utf-8"))
    assert content_hash({k: v for k, v in doc.items() if k not in ADDED}) == doc["content_hash"]
    assert name.endswith(f"feas-{doc['content_hash'][:12]}.json")
    return doc


def _plan(interval: str, tp: str = "2,2.5,3") -> dict[str, Any]:
    doc = _load(interval)
    folds = plan_folds(
        int(doc["range"]["from_ms"]) - 28 * 24 * 60 * MIN,  # anchor 2022-02-01
        int(doc["range"]["to_ms"]),
        initial_train_months=12,
        test_months=3,
        embargo_ms=FILES[interval][2] * MIN,
    )
    return pp.plan_from_feasibility(
        doc,
        test_windows=pp.test_windows_of(folds),
        configurations=24,
        geometries=[(f"{b}R", float(b)) for b in tp.split(",")],
        cost_r=0.2,
        train_windows=pp.train_windows_of(folds),
        n_min=300,
    )


def _candidates(doc: dict[str, Any]) -> dict[str, int]:
    return {k: s["total"]["cumulative"][-1]["bars"] for k, s in doc["symbols"].items()}


@pytest.mark.parametrize("interval", ["15m", "1h"])
def test_the_evidence_is_the_owners_file_and_a_complete_count(interval: str) -> None:
    doc = _load(interval)
    assert doc["kind"] == "feasibility_count"
    assert doc["pnl_free"] is True
    assert doc["segment"] == "development"
    assert doc["strategy"]["interval"] == interval
    assert doc["backtest_config_hash"].startswith("1b67ca1a8d29")
    params = doc["strategy"]["params"]
    assert (params["rank_max"], params["vol_z_min"], params["stop_atr"]) == (0.35, 1.0, 1.5)
    assert params["max_cost_r"] == 0.2
    r = doc["range"]
    minutes = (r["to_ms"] - r["read_from_ms"]) // MIN
    for sym in doc["symbols"].values():
        assert sym["conflicts"] == 0
        assert sym["total"]["missing_minutes"] == 81  # 2023-03-24, registered downtime
        assert sym["bars_1m"] == minutes - 81 - sym["warmup"]["missing_minutes"]


def test_15m_is_recorded_as_infeasible_under_n_min() -> None:
    doc = _load("15m")
    assert _candidates(doc) == {
        "spot:BTCUSDT": 8, "spot:ETHUSDT": 13, "spot:DOGEUSDT": 94, "spot:XRPUSDT": 52,
    }  # fmt: skip
    plan = _plan("15m")
    assert plan["oos_candidates"] == pytest.approx(42.7, abs=0.05)
    tw = plan["training_windows"]
    assert [f["candidates_ceiling"] for f in tw["folds"]] == [70, 76, 78, 78, 82, 108, 109]
    assert tw["folds_below_n_min"] == 7  # even with February 2022 at the busiest month's rate
    assert tw["selection_possible_in_every_fold"] is False


def test_1h_measurements_as_recorded() -> None:
    doc = _load("1h")
    assert sum(_candidates(doc).values()) == 351
    plan = _plan("1h")
    assert plan["oos_candidates"] == pytest.approx(146.1, abs=0.05)
    tw = plan["training_windows"]
    assert [f["candidates_ceiling"] for f in tw["folds"]] == [153, 189, 197, 200, 220, 265, 289]
    assert [f["reaches_n_min"] for f in tw["folds"]] == [False] * 6 + [None]
