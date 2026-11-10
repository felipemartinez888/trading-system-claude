"""The advisory agent gate in paper trading: the ``agent_gated`` book (M5-3; ADR-0007, L-12).

This module is the deterministic half. `AgentGatedState` lives inside a `PaperSession` that was
started with ``PaperConfig.agent_gated`` (agents mode ``confirmed`` at session start) and is driven
the same way live, on restore and on replay. It never calls a client: review outcomes reach it
only as journaled inputs. The live half (running `AgentGate.review` off the event loop) is
`agents.worker`.

Rules (SIGNALS_AND_AGENTS §4, ADR-0007, decision L-12):

1. **Request.** Every CONFIRMED signal the ``baseline`` book *accepted* at submit (its order was
   submitted, i.e. it passed the deterministic risk check at submit) is queued for review, in
   order. The request is journaled (``AGENT_REVIEW_REQUESTED``) in the same batch as the baseline
   submission. The baseline book has already acted: a review can never delay or change it (L-12).
2. **Outcome.** A finished review is journaled as its own batch (input kind ``agent_review``,
   event ``AGENT_REVIEW_COMPLETED``) **before** anything uses it. Outcomes must arrive in request
   order; anything else (unknown signal, wrong order, malformed outcome, a verdict on a failure
   state) raises `AgentReplayMismatch`: replay and restore fail closed.
3. **Admission.** Only state ``REVIEWED`` with verdict ``APPROVED`` admits the signal to
   ``agent_gated`` (`synthesis.book_admission`). ``DOWNGRADED`` and ``VETOED_AGENT`` block it;
   ``AI_UNAVAILABLE``, ``BUDGET_EXHAUSTED``, ``AGENT_OUTPUT_INVALID`` and ``NOT_REQUESTED`` keep it
   out (counted in the coverage statistics). A downgrade can only block: no verdict carries or
   changes a price, size, stop, take-profit or validity. The verdict also gives the signal's
   *advisory* status (VETOED for ``VETOED_AGENT``), journaled in the outcome event only; the
   lifecycle status, which follows the baseline book, is never changed by an agent.
4. **Execution (latency mapping).** An admitted signal is submitted to ``agent_gated`` on the
   first *observed* bar step **of its own instrument** whose release time is at or after the time
   the outcome became available (``available_at_ms``: the poll that journaled it), so its risk
   check at submit sees that instrument's bar of the decision minute, as the baseline's did. The
   request is the baseline's own request (same entry rule, stop, take-profit, validity)
   restricted to ``agent_gated``; that book has its own risk check at submit and at fill, with
   the same risk hook and configuration as ``baseline``, and sizes from its own equity. Its
   earliest execution is the next 1m open after that bar's close and after its release, so
   execution is never earlier than the first 1m open after the review result existed. If the
   signal's validity ends before that, the book rejects it (``validity_ends_before_execution``):
   agent_gated does not take it. A signal invalidated by the strategy before admission is
   skipped; one invalidated later exits ``agent_gated`` like every other book that holds it.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from typing import Any

from tsclaude.contracts.events import AggregateType, EventType, NewEvent
from tsclaude.contracts.market import Bar
from tsclaude.contracts.signals import (
    AgentReviewState,
    AgentVerdict,
    SignalRecord,
    SignalStatus,
)
from tsclaude.signals.lifecycle import PAPER_STATUS_BOOK, SignalLifecycle
from tsclaude.sim.orders import EntryRequest

from .review import AgentOutcome
from .synthesis import SYNTHESIS_VERSION, advisory_status, book_admission

AGENT_GATED = "agent_gated"
#: version of the admission + execution rules above; part of the session configuration, so a
#: session recorded under other rules does not restore (fail closed)
AGENT_GATED_VERSION = "agent-gated-v1"
#: journal input kind of a review outcome batch (`sim.journal`, head ``kind``)
REVIEW_KIND = "agent_review"
#: cost ledger file of the gate (`agents.gate.LEDGER_FILE`, under ``<data_dir>/agents/``); the
#: outcome cites it with the signal id and review key, which every ledger entry carries
COST_LEDGER_REF = "agents/cost_ledger.jsonl"
MAX_REASON = 300
INTERRUPTED = "review interrupted: the paper service restarted while it was in flight"

#: every key of a journaled review outcome, no more and no less
OUTCOME_KEYS = frozenset(
    {
        "signal_id",
        "state",
        "verdict",
        "reason",
        "risk_passed",
        "latency_ms",
        "calls",
        "cache_hit",
        "review_id",
        "review_key",
        "model",
        "synthesis_version",
        "synthesis_rule",
        "review_cost_usd",
        "indicative",
        "cost_ledger",
    }
)


class AgentReplayMismatch(RuntimeError):
    """A review outcome does not match the review requests (missing, out of order, unknown or
    malformed). Replay and restore fail closed on it, as `risk.hook.ReplayMismatch` does."""


def describe() -> dict[str, Any]:
    """The ``agent_gated`` part of the session configuration (`PaperConfig.describe`)."""
    return {
        "version": AGENT_GATED_VERSION,
        "book": AGENT_GATED,
        "admits": "REVIEWED and APPROVED only",
        "baseline": "never affected by agents (L-12)",
        "execution": "first observed bar of the instrument released at or after the outcome; "
        "next 1m open",
    }


def _clip(text: str) -> str:
    return text if len(text) <= MAX_REASON else text[: MAX_REASON - 3] + "..."


def outcome_payload(signal_id: str, outcome: AgentOutcome) -> dict[str, Any]:
    """The journaled form of a gate outcome (JSON scalars only; no secret is ever part of it)."""
    review = outcome.review
    return {
        "signal_id": signal_id,
        "state": str(outcome.state),
        "verdict": None if outcome.verdict is None else str(outcome.verdict),
        "reason": _clip(str(outcome.reason)),
        "risk_passed": bool(outcome.risk_passed),
        "latency_ms": int(outcome.latency_ms),
        "calls": int(outcome.calls),
        "cache_hit": bool(outcome.cache_hit),
        "review_id": None if review is None else review.review_id,
        "review_key": None if review is None else review.review_key,
        "model": None if review is None else review.model,
        "synthesis_version": None if review is None else review.synthesis_version,
        "synthesis_rule": None if review is None else int(review.rule),
        "review_cost_usd": None if review is None else str(review.cost_usd),
        "indicative": None if review is None else bool(review.indicative),
        "cost_ledger": COST_LEDGER_REF,
    }


def unavailable_payload(signal_id: str, reason: str) -> dict[str, Any]:
    """An ``AI_UNAVAILABLE`` outcome produced without the gate (restart, no reviewer, worker
    failure): fail closed, never an approval."""
    return outcome_payload(signal_id, AgentOutcome(AgentReviewState.AI_UNAVAILABLE, reason, True))


def parse_outcome(payload: Mapping[str, Any]) -> tuple[AgentReviewState, AgentVerdict | None]:
    """Validate a journaled outcome strictly; raise `AgentReplayMismatch` on any defect."""
    if not isinstance(payload, Mapping):
        raise AgentReplayMismatch("review outcome is not an object")
    keys = set(payload)
    if keys != OUTCOME_KEYS:
        extra, missing = sorted(keys - OUTCOME_KEYS), sorted(OUTCOME_KEYS - keys)
        raise AgentReplayMismatch(f"review outcome keys differ (extra {extra}, missing {missing})")
    try:
        state = AgentReviewState(payload["state"])
        raw = payload["verdict"]
        verdict = None if raw is None else AgentVerdict(raw)
        if state is AgentReviewState.PENDING:
            raise ValueError("a PENDING review is not an outcome")
        if payload["risk_passed"] is not True:
            raise ValueError("only risk-passed signals are reviewed")
        for name in ("latency_ms", "calls"):
            v = payload[name]
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise ValueError(f"{name} must be an integer >= 0")
        if not isinstance(payload["reason"], str) or not isinstance(payload["signal_id"], str):
            raise ValueError("signal_id and reason must be strings")
        AgentOutcome(state, payload["reason"], True, verdict=verdict)  # verdict iff REVIEWED
    except (ValueError, TypeError) as exc:
        raise AgentReplayMismatch(f"malformed review outcome: {exc}") from None
    return state, verdict


def _event(kind: EventType, signal_id: str, ts_ms: int, payload: dict[str, Any]) -> NewEvent:
    return NewEvent(
        type=kind,
        aggregate_type=AggregateType.AGENT,
        aggregate_id=signal_id,
        ts_ms=ts_ms,
        payload=payload,
    )


class AgentGatedState:
    """Deterministic agent state of one paper session (see the module docstring)."""

    def __init__(self) -> None:
        #: (signal_id, requested_at_ms) in request order, outcome not journaled yet
        self._awaiting: deque[tuple[str, int]] = deque()
        #: (signal_id, available_at_ms) of admitted signals not submitted to agent_gated yet
        self._admissions: deque[tuple[str, int]] = deque()
        self._invalidated: set[str] = set()
        self.counts: dict[str, int] = {
            "requested": 0,
            "completed": 0,
            "admitted": 0,
            "submitted": 0,
            "skipped": 0,
            "accepted_by_book": 0,
            "rejected_by_book": 0,
            "entries_filled": 0,
            "positions_closed": 0,
        }
        self.by_state: dict[str, int] = {}
        self.by_verdict: dict[str, int] = {}
        self.rejections: dict[str, int] = {}
        self.skips: dict[str, int] = {}

    # ------------------------------------------------------------ views
    def awaiting(self) -> list[str]:
        return [sid for sid, _ in self._awaiting]

    def head(self) -> str | None:
        return self._awaiting[0][0] if self._awaiting else None

    @property
    def pending_admissions(self) -> int:
        return len(self._admissions)

    def coverage(self) -> dict[str, Any]:
        return {
            **self.counts,
            "awaiting_review": len(self._awaiting),
            "pending_admissions": len(self._admissions),
            "by_state": dict(sorted(self.by_state.items())),
            "by_verdict": dict(sorted(self.by_verdict.items())),
            "book_rejections": dict(sorted(self.rejections.items())),
            "skip_reasons": dict(sorted(self.skips.items())),
        }

    # ------------------------------------------------------------ requests (from sim events)
    def on_sim_events(self, events: Iterable[Mapping[str, Any]]) -> list[NewEvent]:
        """Review requests for the baseline book's accepted submissions of this step, and the
        ``agent_gated`` book's own results (statistics only)."""
        out: list[NewEvent] = []
        for e in events:
            kind, p = e["type"], e["payload"]
            book = p.get("book")
            if book == PAPER_STATUS_BOOK and kind == "sim_order_submitted":
                request = p.get("request") or {}
                sid = request.get("signal_id")
                if sid is None:
                    continue
                ts = int(e["ts_ms"])
                self._awaiting.append((str(sid), ts))
                self.counts["requested"] += 1
                out.append(
                    _event(
                        EventType.AGENT_REVIEW_REQUESTED,
                        str(sid),
                        ts,
                        {"signal_id": str(sid), "requested_at_ms": ts, "book": PAPER_STATUS_BOOK},
                    )
                )
            elif book == AGENT_GATED:
                if kind == "sim_order_submitted":
                    self.counts["accepted_by_book"] += 1
                elif kind == "sim_request_rejected":
                    self.counts["rejected_by_book"] += 1
                    reason = str(p.get("reason")).split(":", 2)
                    key = ":".join(reason[:2]) if reason[0] == "risk_blocked" else reason[0]
                    self.rejections[key] = self.rejections.get(key, 0) + 1
                elif kind == "sim_order_filled" and p.get("role") == "entry":
                    self.counts["entries_filled"] += 1
                elif kind == "sim_position_closed":
                    self.counts["positions_closed"] += 1
        return out

    def invalidate(self, signal_id: str) -> None:
        """The strategy invalidated the signal: a pending admission is skipped."""
        self._invalidated.add(signal_id)

    # ------------------------------------------------------------ outcomes (journaled inputs)
    def check(self, payload: Mapping[str, Any]) -> tuple[AgentReviewState, AgentVerdict | None]:
        """Validate an outcome against the next expected request without changing anything."""
        state, verdict = parse_outcome(payload)
        expected = self.head()
        if expected is None:
            raise AgentReplayMismatch(
                f"review outcome for {payload['signal_id']} but no review was requested"
            )
        if payload["signal_id"] != expected:
            raise AgentReplayMismatch(
                f"review outcome for {payload['signal_id']}, expected {expected} (request order)"
            )
        return state, verdict

    def apply(self, payload: Mapping[str, Any], available_at_ms: int, ts_ms: int) -> NewEvent:
        """Apply one journaled outcome: the ``AGENT_REVIEW_COMPLETED`` event of its batch."""
        if isinstance(available_at_ms, bool) or not isinstance(available_at_ms, int):
            raise AgentReplayMismatch("available_at_ms must be an integer")
        state, verdict = self.check(payload)
        sid, requested_at = self._awaiting[0]
        if available_at_ms < requested_at:
            raise AgentReplayMismatch(f"review outcome for {sid} available before its request")
        self._awaiting.popleft()
        admitted = book_admission(True, verdict).agent_gated
        self.counts["completed"] += 1
        self.by_state[str(state)] = self.by_state.get(str(state), 0) + 1
        if verdict is not None:
            self.by_verdict[str(verdict)] = self.by_verdict.get(str(verdict), 0) + 1
        if admitted:
            self.counts["admitted"] += 1
            self._admissions.append((sid, available_at_ms))
        return _event(
            EventType.AGENT_REVIEW_COMPLETED,
            sid,
            ts_ms,
            {
                **dict(payload),
                "requested_at_ms": requested_at,
                "available_at_ms": available_at_ms,
                "agent_gated_admitted": admitted,
                "advisory_status": str(advisory_status(SignalStatus.CONFIRMED, verdict)),
                "baseline_affected": False,
                "rules": AGENT_GATED_VERSION,
                "synthesis": SYNTHESIS_VERSION,
            },
        )

    # ------------------------------------------------------------ admissions (bar steps)
    def due(
        self, bar: Bar, released_at_ms: int | None, lifecycle: SignalLifecycle
    ) -> tuple[list[EntryRequest], list[NewEvent]]:
        """Requests for ``agent_gated`` whose outcome was available by this step's release, for
        signals of this bar's instrument (called for observed bars only). A step without a
        release time admits nothing."""
        if released_at_ms is None or not self._admissions:
            return [], []
        requests: list[EntryRequest] = []
        events: list[NewEvent] = []
        keep: deque[tuple[str, int]] = deque()
        ts = bar.close_time_ms
        for sid, available_at in self._admissions:
            state = lifecycle.get(sid)
            other = state is not None and (state.market, state.symbol) != (
                str(bar.market),
                bar.symbol,
            )
            if available_at > released_at_ms or other:
                keep.append((sid, available_at))
                continue
            reason: str | None = None
            if state is None:
                reason = "unknown_signal"
            elif sid in self._invalidated or state.status is SignalStatus.INVALIDATED:
                reason = "invalidated_before_admission"
            if reason is None and state is not None:
                record = SignalRecord.model_validate(state.record)
                requests.append(EntryRequest.from_signal(record, books=(AGENT_GATED,)))
                self.counts["submitted"] += 1
                action = "submitted"
            else:
                self.counts["skipped"] += 1
                key = str(reason)
                self.skips[key] = self.skips.get(key, 0) + 1
                action = "skipped"
            events.append(
                _event(
                    EventType.AGENT_GATED_ADMISSION,
                    sid,
                    ts,
                    {
                        "signal_id": sid,
                        "book": AGENT_GATED,
                        "action": action,
                        "reason": reason,
                        "decision_ms": ts,
                        "released_at_ms": released_at_ms,
                        "available_at_ms": available_at,
                    },
                )
            )
        self._admissions = keep
        return requests, events


__all__ = [
    "AGENT_GATED",
    "AGENT_GATED_VERSION",
    "COST_LEDGER_REF",
    "INTERRUPTED",
    "OUTCOME_KEYS",
    "REVIEW_KIND",
    "AgentGatedState",
    "AgentReplayMismatch",
    "describe",
    "outcome_payload",
    "parse_outcome",
    "unavailable_payload",
]
