"""Deterministic synthesis of the two opinions, and the advise-only application of a verdict.

Rule table ``synthesis-v1`` (first matching row wins):

| # | Condition | Verdict |
|---|---|---|
| 1 | Skeptic raised at least one ``fatal`` objection | VETOED_AGENT |
| 2 | Analyst stance ``oppose`` and total score <= -3 | VETOED_AGENT |
| 3 | Analyst stance ``oppose`` | DOWNGRADED |
| 4 | Total score < 0, or any dimension scored -2 | DOWNGRADED |
| 5 | Skeptic raised >= 2 ``material`` objections | DOWNGRADED |
| 6 | otherwise | APPROVED |

Advise-only (ADR-0007, CLAUDE.md rule 7): a verdict maps onto a signal status only through
`advisory_status`, which can keep a CONFIRMED signal CONFIRMED or move it to VETOED, and leaves
every other status untouched. It cannot revive a vetoed/expired signal, cannot promote WATCH, and
no verdict carries a price, size, stop or take-profit. Book admission (`BookAdmission`): the
deterministic ``baseline`` book is never affected by agents (it is the comparison baseline);
the ``agent_gated`` book admits only APPROVED reviews (SIGNALS_AND_AGENTS §3-§4).
"""

from __future__ import annotations

from dataclasses import dataclass

from tsclaude.contracts.signals import AgentVerdict, SignalStatus

from .schema import AnalystOutput, Severity, SkepticOutput

SYNTHESIS_VERSION = "synthesis-v1"


@dataclass(frozen=True)
class Synthesis:
    verdict: AgentVerdict
    rule: int
    explanation: str


def synthesize(analyst: AnalystOutput, skeptic: SkepticOutput) -> Synthesis:
    total = analyst.total_score
    if skeptic.count(Severity.FATAL) >= 1:
        return Synthesis(AgentVerdict.VETOED_AGENT, 1, "skeptic raised a fatal objection")
    if analyst.stance == "oppose" and total <= -3:
        return Synthesis(
            AgentVerdict.VETOED_AGENT, 2, "analyst opposes with a strongly negative score"
        )
    if analyst.stance == "oppose":
        return Synthesis(AgentVerdict.DOWNGRADED, 3, "analyst opposes")
    if total < 0 or any(d.score == -2 for d in analyst.dimensions.all()):
        return Synthesis(AgentVerdict.DOWNGRADED, 4, "negative analyst assessment")
    if skeptic.count(Severity.MATERIAL) >= 2:
        return Synthesis(AgentVerdict.DOWNGRADED, 5, "two or more material objections")
    return Synthesis(AgentVerdict.APPROVED, 6, "no rule against the signal")


def advisory_status(current: SignalStatus, verdict: AgentVerdict | None) -> SignalStatus:
    """The status an agent verdict implies. Never raises a status; only CONFIRMED can change."""
    if current is SignalStatus.CONFIRMED and verdict is AgentVerdict.VETOED_AGENT:
        return SignalStatus.VETOED
    return current


@dataclass(frozen=True)
class BookAdmission:
    baseline: bool  # unchanged by agents: whatever the deterministic risk engine decided
    agent_gated: bool  # only an APPROVED review of a risk-passed CONFIRMED signal


def book_admission(risk_passed: bool, verdict: AgentVerdict | None) -> BookAdmission:
    return BookAdmission(
        baseline=risk_passed,
        agent_gated=risk_passed and verdict is AgentVerdict.APPROVED,
    )
