"""The agent gate: one entry point, `AgentGate.review`, for one signal.

Order (SIGNALS_AND_AGENTS §3-§4):

1. mode ``off`` -> ``NOT_REQUESTED`` immediately: no packet, no prompt, no ledger, no client.
2. only a CONFIRMED signal that passed the deterministic risk gate is reviewed; anything else is
   ``NOT_REQUESTED`` (agents can never approve a risk-blocked signal). Mode ``research`` reviews
   REPLAY/BACKTEST signals only.
3. packet build + masking audit; failure -> ``AI_UNAVAILABLE``.
4. cache hit -> the stored review, zero calls.
5. circuit breaker open -> ``AI_UNAVAILABLE``.
6. budget: both calls' worst case must fit under the daily cap, and every attempt is re-checked
   -> ``BUDGET_EXHAUSTED``.
7. Analyst, then Skeptic (sees packet + analyst output). Per call: deadline enforced here, one
   bounded retry on timeout / API down; invalid output is not retried -> ``AGENT_OUTPUT_INVALID``;
   transport failure after the retry -> ``AI_UNAVAILABLE``.
8. deterministic synthesis -> REVIEWED with APPROVED / DOWNGRADED / VETOED_AGENT; cached.

Any unexpected exception anywhere is caught and becomes ``AI_UNAVAILABLE``. No failure path ever
produces a verdict (`AgentOutcome` enforces it), so failures behave exactly like mode ``off`` for
the baseline book and keep the signal out of the ``agent_gated`` book.
"""

from __future__ import annotations

import functools
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

from tsclaude.contracts.signals import AgentReviewState, RunMode, SignalRecord, SignalStatus
from tsclaude.contracts.timeutil import WallClock

from .breaker import BreakerState, CircuitBreaker
from .budget import (
    MS_PER_DAY,
    CallOutcome,
    CostBasis,
    CostEntry,
    CostLedger,
    DailyBudget,
    EntryKind,
    LedgerUnreadable,
    cost_of,
    q,
)
from .cache import ReviewCache, review_key
from .client import (
    AgentClient,
    ClientError,
    ClientTimeout,
    ClientUnavailable,
    CompletionRequest,
    CompletionResponse,
    Usage,
)
from .config import AgentMode, LoadedConfig, Pricing
from .packet import EvidencePacket, PacketError, PacketInputs, build_packet
from .prompt_registry import PROMPTS_DIR, Prompt, Role, load_prompt
from .review import AgentOutcome, AgentReview
from .schema import AnalystOutput, OutputInvalid, SkepticOutput, validate_analyst, validate_skeptic
from .synthesis import SYNTHESIS_VERSION, synthesize

log = logging.getLogger(__name__)

_T = TypeVar("_T", AnalystOutput, SkepticOutput)

LEDGER_FILE = "cost_ledger.jsonl"
# Upper bound of the re-serialized AnalystOutput: 4 x (8 refs x 64 + 4 concerns x 240) + summary
# 400 + JSON overhead; only used for the up-front reservation (each call is re-checked exactly).
MAX_ANALYST_JSON_CHARS = 16_000  # doubled for worst-case escaping
CACHE_FILE = "review_cache.jsonl"


def worst_case_cost(text: str, max_output_tokens: int, pricing: Pricing) -> Decimal:
    """Reservation for one call: one token per input character (an upper bound: a token is never
    shorter than one character) and the full output allowance (the API never exceeds
    ``max_tokens``). With measured usage within these bounds, the daily cap cannot be exceeded."""
    return cost_of(Usage(len(text) + 1, max_output_tokens), pricing)


class _Stop(Exception):
    def __init__(self, state: AgentReviewState, reason: str) -> None:
        super().__init__(reason)
        self.state = state
        self.reason = reason


@dataclass
class _Run:
    signal_id: str
    key: str
    calls: int = 0
    cost: Decimal = Decimal(0)


class ClientCrashed(ClientError):
    """The client raised something that is not an `Exception` (e.g. SystemExit in its thread)."""


def call_with_deadline(
    client: AgentClient, request: CompletionRequest, timeout_s: float
) -> CompletionResponse:
    """Run the call in a daemon thread; give up after `timeout_s` (the thread is abandoned: a
    client that ignores its own timeout can delay nothing but itself). A non-`Exception` raised
    inside the worker becomes `ClientCrashed`, so it is charged and handled like any client bug
    and never escapes `AgentGate.review`."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["ok"] = client.complete(request)
        except Exception as exc:
            box["err"] = exc
        except BaseException as exc:  # never re-raise a worker's SystemExit/KeyboardInterrupt
            box["err"] = ClientCrashed(type(exc).__name__)

    worker = threading.Thread(target=run, name=f"agent-{request.role}", daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        raise ClientTimeout(f"no reply within {timeout_s:g} s")
    if "err" in box:
        err: Exception = box["err"]
        raise err
    if "ok" not in box:
        raise ClientCrashed("worker ended without a result")
    result: CompletionResponse = box["ok"]
    return result


class AgentGate:
    def __init__(
        self,
        loaded: LoadedConfig,
        *,
        client: AgentClient | None,
        clock: WallClock,
        data_dir: Path | None = None,
        timer: Callable[[], float] = time.monotonic,
        prompts_dir: Path = PROMPTS_DIR,
    ) -> None:
        self.loaded = loaded
        self.config = loaded.config
        self.mode = self.config.mode
        self.clock = clock
        self.timer = timer
        self.client = client if self.mode is not AgentMode.OFF else None
        self._lock = threading.Lock()  # one review at a time (budget and breaker stay exact)
        self.counters: dict[str, int] = {}
        if self.mode is AgentMode.OFF:
            return  # nothing else is built: mode off runs no agent code
        self.prompts = {role: load_prompt(role, prompts_dir) for role in Role}
        self.ledger = CostLedger(data_dir / LEDGER_FILE if data_dir else None)
        self.budget = DailyBudget(self.config.daily_cap_usd, self.ledger, clock)
        self.cache = ReviewCache(data_dir / CACHE_FILE if data_dir else None)
        self.breaker = CircuitBreaker(
            self.config.breaker_failure_threshold,
            int(self.config.breaker_cooldown_s * 1000),
            clock,
        )

    # ------------------------------------------------------------------ public
    def review(
        self,
        signal: SignalRecord,
        *,
        risk_passed: bool,
        inputs: Callable[[], PacketInputs],
        status: SignalStatus | None = None,
        reevaluate: bool = False,
    ) -> AgentOutcome:
        """Review one signal. Never raises; never returns a verdict on a failure path.

        `inputs` is a thunk so that mode ``off`` (and every not-requested case) does not even
        assemble the packet inputs.
        """
        if self.mode is AgentMode.OFF:
            return self._count(
                AgentOutcome(AgentReviewState.NOT_REQUESTED, "mode off", risk_passed)
            )
        start = self.timer()
        try:
            with self._lock:
                outcome = self._review(signal, risk_passed, inputs, status, reevaluate, start)
        except Exception as exc:  # fail safe: a bug here is "no review", never an approval
            log.exception("agent review failed unexpectedly")
            outcome = AgentOutcome(
                AgentReviewState.AI_UNAVAILABLE,
                f"internal error: {type(exc).__name__}",
                risk_passed,
                latency_ms=self._ms_since(start),
            )
        return self._count(outcome)

    def snapshot(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "mode": str(self.mode),
            "config_version": self.config.version,
            "config_errors": list(self.loaded.errors),
            "counters": dict(self.counters),
        }
        if self.mode is AgentMode.OFF:
            return base
        spend: dict[str, Any]
        try:
            spend = {
                "spent_today_usd": str(self.budget.spent_today()),
                "remaining_today_usd": str(self.budget.remaining()),
                "calls_today": self.ledger.calls_on(self.budget.today()),
                "ledger_error": self.ledger.error,
                "over_reservation_calls": self.ledger.over_reservations(),
            }
        except LedgerUnreadable as exc:  # e.g. the lock timed out: unknown spend, never a crash
            spend = {
                "spent_today_usd": None,
                "remaining_today_usd": None,
                "calls_today": None,
                "ledger_error": f"cost ledger unreadable ({exc})",
                "over_reservation_calls": None,
            }
        return (
            base
            | spend
            | {
                "model": self.config.model,
                "prompts": {str(r): p.ref for r, p in self.prompts.items()},
                "synthesis_version": SYNTHESIS_VERSION,
                "daily_cap_usd": str(self.budget.cap),
                "breaker": str(self.breaker.state),
                "consecutive_failures": self.breaker.consecutive_failures,
                "cached_reviews": len(self.cache),
            }
        )

    # ------------------------------------------------------------------ internals
    def _count(self, outcome: AgentOutcome) -> AgentOutcome:
        key = str(outcome.state) + (f":{outcome.verdict}" if outcome.verdict else "")
        self.counters[key] = self.counters.get(key, 0) + 1
        return outcome

    def _ms_since(self, start: float) -> int:
        return max(0, round((self.timer() - start) * 1000))

    def _review(
        self,
        signal: SignalRecord,
        risk_passed: bool,
        inputs: Callable[[], PacketInputs],
        status: SignalStatus | None,
        reevaluate: bool,
        start: float,
    ) -> AgentOutcome:
        def done(state: AgentReviewState, reason: str, run: _Run | None = None) -> AgentOutcome:
            return AgentOutcome(
                state,
                reason,
                risk_passed,
                calls=run.calls if run else 0,
                latency_ms=self._ms_since(start),
            )

        current = status if status is not None else signal.stage
        if current is not SignalStatus.CONFIRMED:
            return done(AgentReviewState.NOT_REQUESTED, "only CONFIRMED signals are reviewed")
        if not risk_passed:
            return done(AgentReviewState.NOT_REQUESTED, "risk-blocked signals are never reviewed")
        if self.mode is AgentMode.RESEARCH and signal.run_mode is RunMode.LIVE:
            return done(AgentReviewState.NOT_REQUESTED, "research mode reviews non-LIVE only")
        if self.client is None:
            return done(AgentReviewState.AI_UNAVAILABLE, "no client configured")

        try:
            packet = build_packet(inputs(), self.config.packet)
        except PacketError as exc:
            return done(AgentReviewState.AI_UNAVAILABLE, f"packet refused: {exc}")

        analyst_p, skeptic_p = self.prompts[Role.ANALYST], self.prompts[Role.SKEPTIC]
        # LIVE and indicative (REPLAY/BACKTEST) reviews never share a cache entry: the
        # `indicative` label and the agent_gated eligibility depend on the class
        run_class = "live" if signal.run_mode is RunMode.LIVE else "indicative"
        key = review_key(
            packet.packet_hash,
            analyst_p.ref,
            skeptic_p.ref,
            self.config.model,
            SYNTHESIS_VERSION,
            run_class,
        )
        if not reevaluate and (cached := self.cache.get(key)) is not None:
            return AgentOutcome(
                AgentReviewState.REVIEWED,
                "cached review",
                risk_passed,
                verdict=cached.verdict,
                review=cached,
                cache_hit=True,
                latency_ms=self._ms_since(start),
            )

        if not self.breaker.allow():
            return done(AgentReviewState.AI_UNAVAILABLE, "circuit breaker open")
        settled = False
        try:
            outcome, success = self._reviewed(signal, risk_passed, packet, key, start, done)
            if success is None:
                self.breaker.release_trial()  # budget refusal: not an API failure
            elif success:
                self.breaker.record_success()
            else:
                self.breaker.record_failure()
            settled = True
            return outcome
        finally:
            if not settled:  # an unexpected exception: never leave a half-open trial in flight
                self.breaker.record_failure()

    def _reviewed(
        self,
        signal: SignalRecord,
        risk_passed: bool,
        packet: EvidencePacket,
        key: str,
        start: float,
        done: Callable[..., AgentOutcome],
    ) -> tuple[AgentOutcome, bool | None]:
        """The paid part. Returns the outcome and True (success) / False (failure) / None
        (budget refusal) for the breaker."""
        analyst_p, skeptic_p = self.prompts[Role.ANALYST], self.prompts[Role.SKEPTIC]
        run = _Run(signal.signal_id, key)
        analyst_user = _analyst_user(packet)
        # both calls must fit before the first is made (no half-paid review)
        cfg = self.config
        reserve = worst_case_cost(
            analyst_p.text + analyst_user, cfg.max_output_tokens, cfg.pricing
        ) + worst_case_cost(
            # the skeptic also reads the analyst's validated reply, bounded by its schema
            skeptic_p.text + analyst_user + "x" * MAX_ANALYST_JSON_CHARS,
            cfg.max_output_tokens,
            cfg.pricing,
        )
        ok, why = self.budget.can_afford(reserve)
        if not ok:
            return done(AgentReviewState.BUDGET_EXHAUSTED, why), None

        try:
            analyst: AnalystOutput = self._call(
                run, analyst_p, analyst_user, lambda t: validate_analyst(t, packet)
            )
            skeptic: SkepticOutput = self._call(
                run,
                skeptic_p,
                _skeptic_user(packet, analyst),
                lambda t: validate_skeptic(t, packet),
            )
        except _Stop as stop:
            budget = stop.state is AgentReviewState.BUDGET_EXHAUSTED
            return done(stop.state, stop.reason, run), (None if budget else False)

        synthesis = synthesize(analyst, skeptic)
        now = self.clock.now_ms()
        review = AgentReview(
            review_id=f"rev-{key[:16]}-{now}",
            review_key=key,
            signal_id=signal.signal_id,
            packet_hash=packet.packet_hash,
            packet_json=packet.to_json(),
            analyst_prompt=analyst_p.ref,
            skeptic_prompt=skeptic_p.ref,
            model=self.config.model,
            synthesis_version=SYNTHESIS_VERSION,
            analyst=analyst,
            skeptic=skeptic,
            verdict=synthesis.verdict,
            rule=synthesis.rule,
            explanation=synthesis.explanation,
            created_at_ms=now,
            cost_usd=q(run.cost),
            latency_ms=self._ms_since(start),
            indicative=signal.run_mode is not RunMode.LIVE,
        )
        self.cache.put(review)
        outcome = AgentOutcome(
            AgentReviewState.REVIEWED,
            synthesis.explanation,
            risk_passed,
            verdict=synthesis.verdict,
            review=review,
            calls=run.calls,
            latency_ms=review.latency_ms,
        )
        return outcome, True

    def _call(
        self,
        run: _Run,
        prompt: Prompt,
        user: str,
        validate: Callable[[str], _T],
    ) -> _T:
        client = self.client
        if client is None:
            raise _Stop(AgentReviewState.AI_UNAVAILABLE, "no client configured")
        cfg = self.config
        request = CompletionRequest(
            role=str(prompt.role),
            model=cfg.model,
            system=prompt.text,
            user=user,
            max_output_tokens=cfg.max_output_tokens,
            timeout_s=cfg.per_call_timeout_s,
        )
        worst = worst_case_cost(prompt.text + user, cfg.max_output_tokens, cfg.pricing)
        last_reason = ""
        for attempt in range(1 + cfg.max_retries):
            t0 = self.timer()
            at = self.clock.now_ms()
            call_id = f"{run.key[:16]}-{prompt.role}-{attempt}-{uuid.uuid4().hex[:12]}"
            # reserve the worst case BEFORE the call (atomic check-and-append); a crash during
            # the call leaves it on the books
            ok, why = self.budget.try_reserve(
                self._entry(
                    EntryKind.RESERVATION,
                    call_id,
                    at,
                    run,
                    prompt,
                    attempt,
                    None,
                    worst,
                    CostBasis.RESERVED,
                    0,
                    CallOutcome.PENDING,
                )
            )
            if not ok:
                raise _Stop(AgentReviewState.BUDGET_EXHAUSTED, why)
            run.calls += 1

            settle = functools.partial(self._settle, run, prompt, call_id, at, attempt, t0)

            try:
                response = call_with_deadline(client, request, cfg.per_call_timeout_s)
            except ClientTimeout:
                settle(None, worst, CostBasis.WORST_CASE, CallOutcome.TIMEOUT)
                last_reason = f"{prompt.role} call timed out"
                continue
            except ClientUnavailable:
                settle(None, Decimal(0), CostBasis.NOT_BILLED, CallOutcome.API_DOWN)
                last_reason = f"{prompt.role} API unavailable"
                continue
            except Exception as exc:  # unexpected client error (incl. ClientCrashed): no retry
                settle(None, worst, CostBasis.WORST_CASE, CallOutcome.ERROR)
                raise _Stop(
                    AgentReviewState.AI_UNAVAILABLE,
                    f"{prompt.role} client error: {type(exc).__name__}",
                ) from None
            cost = cost_of(response.usage, cfg.pricing)
            if cost > worst:
                log.warning("agent call cost above its reservation (over-reservation alert)")
            try:
                output = validate(response.text)
            except OutputInvalid as exc:
                settle(response.usage, cost, CostBasis.MEASURED, CallOutcome.OUTPUT_INVALID)
                raise _Stop(
                    AgentReviewState.AGENT_OUTPUT_INVALID, f"{prompt.role}: {exc}"
                ) from None
            settle(response.usage, cost, CostBasis.MEASURED, CallOutcome.OK)
            return output
        raise _Stop(AgentReviewState.AI_UNAVAILABLE, last_reason)

    def _settle(
        self,
        run: _Run,
        prompt: Prompt,
        call_id: str,
        at: int,
        attempt: int,
        t0: float,
        usage: Usage | None,
        cost: Decimal,
        basis: CostBasis,
        outcome: CallOutcome,
    ) -> None:
        run.cost += cost
        self.ledger.settle(
            self._entry(
                EntryKind.SETTLEMENT,
                call_id,
                at,
                run,
                prompt,
                attempt,
                usage,
                cost,
                basis,
                self._ms_since(t0),
                outcome,
            )
        )

    def _entry(
        self,
        kind: EntryKind,
        call_id: str,
        at: int,
        run: _Run,
        prompt: Prompt,
        attempt: int,
        usage: Usage | None,
        cost: Decimal,
        basis: CostBasis,
        latency_ms: int,
        outcome: CallOutcome,
    ) -> CostEntry:
        u = usage or Usage(0, 0, 0)
        return CostEntry(
            kind=kind,
            call_id=call_id,
            at_ms=at,
            day=at // MS_PER_DAY,
            review_key=run.key,
            signal_id=run.signal_id,
            role=str(prompt.role),
            attempt=attempt,
            model=self.config.model,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cached_input_tokens=u.cached_input_tokens,
            cost_usd=q(cost),
            cost_basis=basis,
            latency_ms=latency_ms,
            outcome=outcome,
        )

    @property
    def breaker_state(self) -> BreakerState | None:
        return None if self.mode is AgentMode.OFF else self.breaker.state


def _analyst_user(packet: EvidencePacket) -> str:
    return "EVIDENCE PACKET:\n" + packet.to_json()


def _skeptic_user(packet: EvidencePacket, analyst: AnalystOutput) -> str:
    return (
        "EVIDENCE PACKET:\n"
        + packet.to_json()
        + "\n\nANALYST ASSESSMENT:\n"
        + json.dumps(
            analyst.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    )
