"""Risk engine as a `sim.book.RiskHook`, with recording for replay parity.

`EngineRiskHook` asks a *view provider* for the market view at the check's cutoff, evaluates the
engine and records ``(stage, book, request_id, ts_ms, view, decision)``. Live sessions use
`tracker_provider` (real-time `MarketStateTracker`); replays use `RecordedViews`, which hands back
the recorded views in order, so the replayed decisions are re-*computed* by the same pure engine
on the same inputs -- not copied. A provider error yields no view, which blocks (fail closed).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from tsclaude.risk.engine import RiskEngine
from tsclaude.risk.market import MarketStateTracker, MarketView, cutoff_ms
from tsclaude.sim.book import RiskCheck, RiskDecision

type ViewProvider = Callable[[RiskCheck], MarketView | None]
type RiskRecord = dict[str, Any]


class ReplayMismatch(RuntimeError):
    """A replayed risk check does not match the recorded one (inputs diverged)."""


def check_key(check: RiskCheck) -> dict[str, Any]:
    return {
        "stage": check.stage,
        "book": check.book,
        "request_id": check.request.request_id,
        "ts_ms": check.ts_ms,
    }


def tracker_provider(tracker: MarketStateTracker) -> ViewProvider:
    def provide(check: RiskCheck) -> MarketView | None:
        key = (check.request.market, check.request.symbol)
        return tracker.view_at(cutoff_ms(check.stage, check.ts_ms), [key])

    return provide


class RecordedViews:
    """Replay provider: returns recorded views in order, checking each matches its check."""

    def __init__(self, records: Iterable[Mapping[str, Any]] = ()) -> None:
        self._queue: deque[Mapping[str, Any]] = deque(records)

    def extend(self, records: Iterable[Mapping[str, Any]]) -> None:
        self._queue.extend(records)

    @property
    def remaining(self) -> int:
        return len(self._queue)

    def __call__(self, check: RiskCheck) -> MarketView | None:
        if not self._queue:
            raise ReplayMismatch(f"no recorded risk check left for {check_key(check)}")
        rec = self._queue.popleft()
        expected = {k: rec[k] for k in ("stage", "book", "request_id", "ts_ms")}
        if expected != check_key(check):
            raise ReplayMismatch(f"recorded {expected} != replayed {check_key(check)}")
        view = rec["view"]
        return None if view is None else MarketView.from_payload(view)


class EngineRiskHook:
    def __init__(self, engine: RiskEngine, provider: ViewProvider) -> None:
        self.engine = engine
        self.provider = provider
        self._records: list[RiskRecord] = []

    def __call__(self, check: RiskCheck) -> RiskDecision:
        error: str | None = None
        try:
            view = self.provider(check)
        except Exception as exc:  # fail closed: no view -> the market-view rule blocks
            view, error = None, type(exc).__name__
        verdict = self.engine.evaluate(check, view)
        decision = verdict.decision()
        if error is not None and decision.allow:  # defensive: never allow without a view
            decision = RiskDecision(False, f"RISK_BLOCKED:provider_error:{error}")
        self._records.append(
            {
                **check_key(check),
                "view": None if view is None else view.to_payload(),
                "provider_error": error,
                "allow": decision.allow,
                "reason": decision.reason,
            }
        )
        return decision

    def drain(self) -> list[RiskRecord]:
        out, self._records = self._records, []
        return out
