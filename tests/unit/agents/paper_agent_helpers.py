"""Test kit for the agent_gated paper wiring (M5-3): a minute-by-minute driver, scripted mock
reviews and a reviewer built on the real `AgentGate` with the `MockClient`.

Everything is synthetic: no network, no API key, no real client (`MockClient` only).
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_helpers import dim, make_gate, skeptic_payload

from tsclaude.agents.client import MockClient, MockStep
from tsclaude.agents.worker import ReviewWorker, packet_inputs_from_bars
from tsclaude.contracts.market import Bar, Market
from tsclaude.contracts.signals import SignalRecord
from tsclaude.contracts.timeutil import MS_PER_MINUTE, ManualClock
from tsclaude.marketdata.bars import BarClosed
from tsclaude.sim.journal import Batch, MemoryJournal
from tsclaude.sim.paper import PaperConfig, PaperTradingService
from tsclaude.sim.testing import minute

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sim"))
from paper_helpers import (
    BTC,
    ETH,
    ScriptedStrategy,
    config,
    record,
    sid,
    two_symbol_bars,
)
from risk_helpers import snapshot

Bars = Mapping[tuple[Market, str], Sequence[Bar]]

#: packet fields every synthetic paper packet has (1m bars, market entry, bracket)
REFS = {
    "regime": ["signal.side"],
    "technical": ["bars.ohlc_atr[-1]"],
    "volatility_liquidity": ["costs.est_cost_bps"],
    "risk": ["costs.rr_net", "geometry.stop_atr"],
}


def analyst(
    scores: tuple[int, int, int, int] = (1, 1, 0, 1), stance: str = "support"
) -> dict[str, Any]:
    return {
        "dimensions": {k: dim(s, REFS[k]) for k, s in zip(REFS, scores, strict=True)},
        "stance": stance,
        "summary": "Acceptable net reward to risk in a quiet range.",
    }


def approve() -> list[tuple[MockStep, MockStep]]:
    return [(MockStep.valid(analyst()), MockStep.valid(skeptic_payload(("minor",))))]


def downgrade() -> list[tuple[MockStep, MockStep]]:
    return [
        (
            MockStep.valid(analyst((0, 0, 0, -1), "oppose")),
            MockStep.valid(skeptic_payload(("minor",))),
        )
    ]


def veto() -> list[tuple[MockStep, MockStep]]:
    return [(MockStep.valid(analyst()), MockStep.valid(skeptic_payload(("fatal",))))]


def mock_client(reviews: Iterable[tuple[MockStep, MockStep]]) -> MockClient:
    pairs = list(reviews)
    return MockClient.scripted(analyst=[a for a, _ in pairs], skeptic=[k for _, k in pairs])


def inputs_from(bars: Bars) -> Callable[[SignalRecord], Any]:
    def build(signal: SignalRecord) -> Any:
        return packet_inputs_from_bars(signal, bars[(signal.market, signal.symbol)])

    return build


def worker(
    client: MockClient | None,
    bars: Bars,
    *,
    data_dir: Path | None = None,
    inputs: Callable[[SignalRecord], Any] | None = None,
    **gate_overrides: Any,
) -> ReviewWorker:
    gate = make_gate(client, clock=ManualClock(minute(0)), data_dir=data_dir, **gate_overrides)
    return ReviewWorker(gate, inputs or inputs_from(bars))


def gated_config(**kw: object) -> PaperConfig:
    cfg: PaperConfig = config(agent_gated=True, **kw)
    return cfg


def step_minute(
    svc: PaperTradingService,
    bars: Bars,
    i: int,
    *,
    not_before: int = 0,
    missing: frozenset[tuple[int, str]] = frozenset(),
) -> None:
    """One minute the way the live subscription delivers it (as `paper_helpers.drive`)."""
    close = minute(i) + MS_PER_MINUTE - 1
    svc.on_snapshot(snapshot(close - 200, symbols=(BTC, ETH)))
    for key in (ETH, BTC):
        if (i, key[1]) not in missing:
            svc.on_update(BarClosed(bars[key][i]))
    svc.poll(max(close + 1_500, not_before))


def run(
    svc: PaperTradingService,
    bars: Bars,
    minutes: Sequence[int],
    *,
    wait: ReviewWorker | None = None,
    not_before: int = 0,
    missing: frozenset[tuple[int, str]] = frozenset(),
) -> None:
    """Drive `minutes` exactly as `paper_helpers.drive` (same poll times); after each one wait
    until the reviews submitted so far finished, so every outcome becomes available at the next
    poll (deterministic timing)."""
    for i in minutes:
        step_minute(svc, bars, i, not_before=not_before, missing=missing)
        if wait is not None:
            assert wait.wait_idle(10.0), "review did not finish"
    if minutes:
        last_close = minute(minutes[-1]) + MS_PER_MINUTE - 1
        svc.poll(max(last_close + 1_500, not_before))


def entries(batches: Sequence[Batch], kind: str, **match: Any) -> list[tuple[int, Any]]:
    """(batch index, entry) of every journal entry of `kind` whose payload matches."""
    out = []
    for i, batch in enumerate(batches):
        for e in batch:
            if e.type == kind and all(e.payload.get(k) == v for k, v in match.items()):
                out.append((i, e))
    return out


def new_service(
    cfg: PaperConfig,
    strategy: ScriptedStrategy,
    reviewer: ReviewWorker | None = None,
    journal: MemoryJournal | None = None,
) -> tuple[PaperTradingService, MemoryJournal]:
    j = journal or MemoryJournal()
    return PaperTradingService(cfg, j, strategy=strategy, reviewer=reviewer), j


__all__ = [
    "BTC",
    "ETH",
    "ScriptedStrategy",
    "analyst",
    "approve",
    "config",
    "downgrade",
    "entries",
    "gated_config",
    "inputs_from",
    "mock_client",
    "new_service",
    "record",
    "run",
    "sid",
    "step_minute",
    "two_symbol_bars",
    "veto",
    "worker",
]
