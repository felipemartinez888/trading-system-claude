"""Deterministic synthesis table and the advise-only invariant (property-style sweep)."""

from __future__ import annotations

import itertools
import json
import random

import pytest
from agent_helpers import PACKET_CFG, analyst_payload, make_inputs, skeptic_payload

from tsclaude.agents.packet import build_packet
from tsclaude.agents.schema import validate_analyst, validate_skeptic
from tsclaude.agents.synthesis import advisory_status, book_admission, synthesize
from tsclaude.contracts.signals import ALLOWED_TRANSITIONS, AgentVerdict, SignalStatus

PACKET = build_packet(make_inputs(), PACKET_CFG)


def _synth(scores: tuple[int, int, int, int], stance: str, severities: list[str]) -> AgentVerdict:
    a = validate_analyst(json.dumps(analyst_payload(scores, stance)), PACKET)
    s = validate_skeptic(json.dumps(skeptic_payload(severities)), PACKET)
    return synthesize(a, s).verdict


@pytest.mark.parametrize(
    ("scores", "stance", "severities", "verdict"),
    [
        ((2, 2, 2, 2), "support", ["fatal"], AgentVerdict.VETOED_AGENT),  # rule 1
        ((-1, -1, -1, 0), "oppose", [], AgentVerdict.VETOED_AGENT),  # rule 2
        ((0, 0, -1, 0), "oppose", [], AgentVerdict.DOWNGRADED),  # rule 3
        ((1, 1, -2, 1), "support", [], AgentVerdict.DOWNGRADED),  # rule 4 (a -2)
        ((0, 0, -1, 0), "neutral", [], AgentVerdict.DOWNGRADED),  # rule 4 (total < 0)
        ((1, 1, 0, 1), "support", ["material", "material"], AgentVerdict.DOWNGRADED),  # rule 5
        ((1, 1, 0, 1), "support", ["material", "minor"], AgentVerdict.APPROVED),  # rule 6
        ((0, 0, 0, 0), "neutral", [], AgentVerdict.APPROVED),
    ],
)
def test_rule_table(
    scores: tuple[int, int, int, int], stance: str, severities: list[str], verdict: AgentVerdict
) -> None:
    assert _synth(scores, stance, severities) is verdict


def test_synthesis_is_deterministic() -> None:
    rng = random.Random(7)
    for _ in range(200):
        scores = tuple(rng.randint(-2, 2) for _ in range(4))
        stance = rng.choice(["support", "neutral", "oppose"])
        sev = [rng.choice(["minor", "material", "fatal"]) for _ in range(rng.randint(0, 4))]
        assert _synth(scores, stance, sev) is _synth(scores, stance, sev)  # type: ignore[arg-type]


# Higher = further along toward a position. Agents may only move CONFIRMED -> VETOED.
def test_property_advisory_status_never_raises_a_status() -> None:
    verdicts: list[AgentVerdict | None] = [None, *AgentVerdict]
    for status, verdict in itertools.product(SignalStatus, verdicts):
        out = advisory_status(status, verdict)
        if status is SignalStatus.CONFIRMED and verdict is AgentVerdict.VETOED_AGENT:
            assert out is SignalStatus.VETOED
            assert SignalStatus.VETOED in ALLOWED_TRANSITIONS[status]
        else:
            assert out is status  # nothing else changes: no promotion, no revival


def test_property_book_admission_is_monotone() -> None:
    for risk_passed, verdict in itertools.product([True, False], [None, *AgentVerdict]):
        adm = book_admission(risk_passed, verdict)
        assert adm.baseline is risk_passed  # agents never touch the deterministic book
        assert not (adm.agent_gated and not adm.baseline)  # gated is a subset of baseline
        assert adm.agent_gated is (risk_passed and verdict is AgentVerdict.APPROVED)


def test_property_full_sweep_of_valid_outputs() -> None:
    """Every valid (analyst, skeptic) combination on a grid: the verdict is one of three, and
    the resulting status is CONFIRMED or VETOED, never anything else."""
    stances = ["support", "neutral", "oppose"]
    sevs = [[], ["minor"], ["material"], ["material", "material"], ["fatal"]]
    seen = set()
    for scores in itertools.product([-2, 0, 2], repeat=4):
        for stance, sev in itertools.product(stances, sevs):
            v = _synth(scores, stance, sev)  # type: ignore[arg-type]
            seen.add(v)
            assert advisory_status(SignalStatus.CONFIRMED, v) in {
                SignalStatus.CONFIRMED,
                SignalStatus.VETOED,
            }
    assert seen == set(AgentVerdict)
