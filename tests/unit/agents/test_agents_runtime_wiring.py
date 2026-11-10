"""M5-3, runtime side: agents mode -> paper configuration, the paper runtime's reviewer, the
status counts (provenance), packet inputs without look-ahead, the worker's fail-closed paths, and
no secret anywhere. MockClient only; the fake key below is not a key; no network."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any

import pytest
from agent_helpers import make_gate
from paper_agent_helpers import (
    ScriptedStrategy,
    approve,
    config,
    entries,
    gated_config,
    inputs_from,
    mock_client,
    new_service,
    record,
    run,
    sid,
    two_symbol_bars,
    worker,
)

from tsclaude.agents.config import load_config
from tsclaude.agents.packet import PacketError, build_packet
from tsclaude.agents.status import agent_status, runtime_counts
from tsclaude.agents.wiring import AGENT_GATED
from tsclaude.agents.worker import (
    ReviewWorker,
    agent_gated_enabled,
    packet_inputs_from_bars,
    reviewer_factory,
)
from tsclaude.contracts.market import Interval
from tsclaude.contracts.timeutil import ManualClock
from tsclaude.runtime.paper_runtime import (
    AUDIT_FILE,
    PaperRuntime,
    PaperSettings,
    PaperState,
    build_paper_config,
)
from tsclaude.sim.testing import minute, series
from tsclaude.store.eventstore import EventStore

REPO = Path(__file__).resolve().parents[3]
FAKE = "sk-test-not-a-real-key-000"


def _configs(tmp: Path, mode: str | None) -> Path:
    base = tmp / "configs"
    shutil.copytree(REPO / "configs", base)
    agents = base / "agents.yaml"
    if mode is None:
        agents.unlink()
    elif mode == "invalid":
        agents.write_text('mode: "confirmed"\nunknown_key: 1\n', encoding="utf-8")
    else:
        text = agents.read_text(encoding="utf-8").replace('mode: "off"', f'mode: "{mode}"')
        agents.write_text(text, encoding="utf-8")
    return base


@pytest.mark.parametrize(
    ("mode", "gated"),
    [("off", False), ("confirmed", True), ("research", False), ("invalid", False), (None, False)],
)
def test_paper_config_gets_agent_gated_only_in_mode_confirmed(
    tmp_path: Path, mode: str | None, gated: bool
) -> None:
    base = _configs(tmp_path, mode)
    cfg = build_paper_config(PaperSettings(), base)
    assert cfg.agent_gated is gated
    assert (AGENT_GATED in cfg.describe()["books"]) is gated
    assert ("agent_gated" in cfg.describe()) is gated
    if mode != "invalid" and mode is not None:
        assert agent_gated_enabled(load_config(base / "agents.yaml")) is gated


def test_repo_default_config_builds_the_agent_free_session() -> None:
    cfg = build_paper_config(PaperSettings(), REPO / "configs")
    assert cfg.agent_gated is False
    assert AGENT_GATED not in cfg.describe()["books"]


# ------------------------------------------------------------------ the paper runtime
def _runtime(tmp: Path, store: EventStore, factory: Any) -> PaperRuntime:
    return PaperRuntime(
        PaperSettings(enabled=True),
        store,
        ManualClock(minute(0)),
        config_factory=gated_config,
        audit_path=tmp / AUDIT_FILE,
        reviewer_factory=factory,
    )


def test_runtime_open_builds_a_reviewer_publishes_counts_and_closes_it(tmp_path: Path) -> None:
    bars = two_symbol_bars(4)
    made: list[ReviewWorker] = []

    def factory() -> ReviewWorker:
        w = worker(mock_client(approve()), bars)
        made.append(w)
        return w

    with EventStore(tmp_path / "state.db") as store:
        rt = _runtime(tmp_path, store, factory)
        assert rt._open()
        assert rt.state is PaperState.RUNNING
        assert rt.service is not None
        assert rt.service.reviewer is made[0]
        snap, at = rt.published()
        assert snap["agents"]["book"] == AGENT_GATED
        assert snap["agents"]["session_id"] == "paper"
        assert AGENT_GATED in snap["books"]
        counts = runtime_counts((snap, at))
        assert counts["provenance"] == "SYSTEM"
        assert counts["as_of_ms"] == at
        assert counts["value"]["coverage"]["requested"] == 0
        asyncio.run(rt.stop())
        assert made[0].closed
        assert rt._reviewer is None


def test_runtime_without_agent_gated_session_never_calls_the_factory(tmp_path: Path) -> None:
    calls: list[int] = []

    def factory() -> None:
        calls.append(1)

    with EventStore(tmp_path / "state.db") as store:
        rt = PaperRuntime(
            PaperSettings(enabled=True),
            store,
            ManualClock(minute(0)),
            config_factory=config,  # mode off: no agent_gated book
            audit_path=tmp_path / AUDIT_FILE,
            reviewer_factory=factory,
        )
        assert rt._open()
        assert calls == []
        snap, at = rt.published()
        assert "agents" not in snap
        assert runtime_counts((snap, at))["provenance"] == "UNAVAILABLE"
        asyncio.run(rt.stop())


def test_a_failing_reviewer_factory_leaves_paper_running(tmp_path: Path) -> None:
    def factory() -> None:
        raise RuntimeError("no gate today")

    problems: list[tuple[str, str]] = []
    with EventStore(tmp_path / "state.db") as store:
        rt = _runtime(tmp_path, store, factory)
        rt.on_problem = lambda t, d: problems.append((t, d))
        assert rt._open()
        assert rt.state is PaperState.RUNNING
        assert rt.service is not None
        assert rt.service.reviewer is None
        assert problems
        assert problems[0][0] == "Agent reviews unavailable"
        asyncio.run(rt.stop())


def test_status_counts_are_labelled() -> None:
    assert runtime_counts(None)["provenance"] == "UNAVAILABLE"
    assert "no paper session" in runtime_counts(({"service": None}, 5))["reason"]
    off = runtime_counts(({"service": {}, "state": "RUNNING"}, 5))
    assert off["provenance"] == "UNAVAILABLE"
    assert "agent_gated" in off["reason"]
    on = runtime_counts(({"service": {}, "state": "RUNNING", "agents": {"coverage": {}}}, 5))
    assert (on["provenance"], on["as_of_ms"], on["value"]["paper_state"]) == (
        "SYSTEM",
        5,
        "RUNNING",
    )


def test_agent_status_reports_wired_and_the_session_counts(tmp_path: Path) -> None:
    snap = ({"service": {}, "state": "RUNNING", "agents": {"coverage": {"requested": 2}}}, 7)
    st = agent_status(REPO / "configs" / "agents.yaml", tmp_path, minute(0), snap)
    assert st["runtime_wired"] is True
    assert st["runtime"]["provenance"] == "SYSTEM"
    assert st["runtime"]["value"]["coverage"] == {"requested": 2}


# ------------------------------------------------------------------ production factory, secrets
def _agents_yaml(tmp: Path, mode: str) -> Path:
    text = (REPO / "configs" / "agents.yaml").read_text(encoding="utf-8")
    path = tmp / "agents.yaml"
    path.write_text(text.replace('mode: "off"', f'mode: "{mode}"'), encoding="utf-8")
    return path


def test_production_factory_is_off_by_default(tmp_path: Path) -> None:
    make = reviewer_factory(
        REPO / "configs" / "agents.yaml",
        tmp_path / "agents",
        ManualClock(0),
        inputs_from(two_symbol_bars(2)),
        env={"ANTHROPIC_API_KEY": FAKE},
        dotenv=tmp_path / ".env",
    )
    assert make() is None
    assert not (tmp_path / "agents").exists()  # mode off writes nothing


def test_production_stub_client_is_unavailable_and_the_key_never_appears(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    bars = two_symbol_bars(8)
    make = reviewer_factory(
        _agents_yaml(tmp_path, "confirmed"),
        tmp_path / "agents",
        ManualClock(minute(0)),
        inputs_from(bars),
        env={"ANTHROPIC_API_KEY": FAKE},  # a fake value: presence only, never read out
        dotenv=tmp_path / ".env",
    )
    w = make()
    assert isinstance(w, ReviewWorker)
    second = make()
    assert second is not w
    assert isinstance(second, ReviewWorker)
    assert second.gate is w.gate  # one gate per runtime
    strat = ScriptedStrategy({(minute(1), "BTCUSDT"): [record(1, 1)]})
    svc, journal = new_service(gated_config(), strat, w)
    run(svc, bars, list(range(8)), wait=w)
    w.close()
    [(_, done)] = entries(journal.batches(), "agent_review_completed")
    assert done.payload["state"] == "AI_UNAVAILABLE"
    assert not entries(journal.batches(), "sim_order_submitted", book=AGENT_GATED)
    text = "".join(e.canonical() for b in journal.batches() for e in b)
    assert FAKE not in text
    assert FAKE not in caplog.text
    assert FAKE not in str(svc.status())
    for f in (tmp_path / "agents").glob("*"):
        assert FAKE not in f.read_text(encoding="utf-8")


# ------------------------------------------------------------------ packet inputs, worker
def test_packet_inputs_use_only_bars_closed_by_the_decision() -> None:
    raw = series([(1000, 1006 + i % 3, 994 - i % 2, 1000 + i % 5) for i in range(30)])
    rec = record(1, 10)
    inputs = packet_inputs_from_bars(rec, raw)
    assert inputs.bars[-1].close_time_ms == rec.decision_time_ms
    assert all(b.close_time_ms <= rec.decision_time_ms for b in inputs.bars)
    assert len(inputs.bars) == 11
    assert inputs.atr > 0
    build_packet(inputs, make_gate(None).config.packet)  # passes the masking audit


def test_packet_inputs_derive_strategy_bars_from_complete_buckets() -> None:
    raw = series([(1000, 1005, 995, 1000 + i % 4) for i in range(60)])
    rec = record(1, 44).model_copy(update={"timeframe": Interval.M15})  # close of bucket 2
    inputs = packet_inputs_from_bars(rec, raw)
    assert [b.close_time_ms for b in inputs.bars] == [minute(15 * k) - 1 for k in (1, 2, 3)]
    with pytest.raises(PacketError):
        packet_inputs_from_bars(record(1, 0), raw)  # one bar: no ATR


def test_worker_refuses_when_full_or_closed_without_raising() -> None:
    bars = two_symbol_bars(4)
    w = worker(mock_client([]), bars)
    w.max_queue = 0  # nothing may run
    w.submit(record(1, 1))
    w.close()
    w.submit(record(2, 1))
    done = w.completed()
    assert [(s, p["state"]) for s, p in done] == [
        (sid(1), "AI_UNAVAILABLE"),
        (sid(2), "AI_UNAVAILABLE"),
    ]
    assert done[0][1]["reason"] == "review queue full"
    assert done[1][1]["reason"] == "review worker closed"
    assert w.refused == 2
    assert w.submitted == 0
