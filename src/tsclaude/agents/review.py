"""Review record and gate outcome.

`AgentReview` is the stored record of one completed review (packet, prompt refs, model, both
validated outputs, verdict). It has no price, size, stop or take-profit field and rejects unknown
keys, so no review can carry one. `AgentOutcome` is what the gate returns for a signal: a review
state (`AgentReviewState`), the verdict if any, and the book admission derived from it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from tsclaude.contracts.signals import AgentReviewState, AgentVerdict, SignalStatus

from .schema import AnalystOutput, SkepticOutput
from .synthesis import BookAdmission, advisory_status, book_admission


class AgentReview(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    review_id: str
    review_key: str
    signal_id: str  # the signal that triggered this record (a cache hit may serve another one)
    packet_hash: str
    packet_json: str
    analyst_prompt: str  # "<version>@<sha256 prefix>"
    skeptic_prompt: str
    model: str
    synthesis_version: str
    analyst: AnalystOutput
    skeptic: SkepticOutput
    verdict: AgentVerdict
    rule: int
    explanation: str
    created_at_ms: int
    cost_usd: Decimal
    latency_ms: int
    indicative: bool  # True unless a LIVE signal (contamination guard, §3)


@dataclass(frozen=True)
class AgentOutcome:
    state: AgentReviewState
    reason: str
    risk_passed: bool
    verdict: AgentVerdict | None = None
    review: AgentReview | None = None
    cache_hit: bool = False
    calls: int = 0
    latency_ms: int = 0

    def __post_init__(self) -> None:
        # a verdict exists only for a completed review (never on any failure path)
        if (self.verdict is not None) != (self.state is AgentReviewState.REVIEWED):
            raise ValueError("a verdict requires state REVIEWED and vice versa")

    @property
    def admission(self) -> BookAdmission:
        return book_admission(self.risk_passed, self.verdict)

    def advisory_status(self, current: SignalStatus) -> SignalStatus:
        return advisory_status(current, self.verdict)
