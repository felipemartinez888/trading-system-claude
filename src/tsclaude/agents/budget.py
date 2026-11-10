"""Cost ledger and hard daily budget (REQ-AGT-06; D-03: $1 per UTC day).

- Every client call attempt appends one `CostEntry` (model, tokens, cost, latency, outcome) to an
  append-only JSONL file (or memory only, for tests). The file is read back on start, so a restart
  does not reset the day's spend.
- Money is `Decimal` quantized to 1e-8 USD (CLAUDE.md).
- Fail closed on cost: a call whose usage is unknown but may have been billed (timeout, unexpected
  error) is charged its *worst case* (estimated input + max output tokens). A call refused before
  generation (`ClientUnavailable`) is charged zero.
- `can_afford(worst_case)` is checked before every call against ``spent today + worst case <= cap``.
- An unreadable ledger means the day's spend is unknown: the budget refuses every call.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from tsclaude.contracts.timeutil import WallClock

from .client import Usage
from .config import Pricing

QUANTUM = Decimal("0.00000001")
_MTOK = Decimal(1_000_000)
MS_PER_DAY = 86_400_000


def q(x: Decimal) -> Decimal:
    return x.quantize(QUANTUM)


def cost_of(usage: Usage, pricing: Pricing) -> Decimal:
    fresh = max(0, usage.input_tokens - usage.cached_input_tokens)
    return q(
        (
            Decimal(fresh) * pricing.input_per_mtok
            + Decimal(usage.cached_input_tokens) * pricing.cached_input_per_mtok
            + Decimal(usage.output_tokens) * pricing.output_per_mtok
        )
        / _MTOK
    )


class CostBasis(StrEnum):
    MEASURED = "measured"  # usage reported by the API
    WORST_CASE = "worst_case"  # usage unknown, possibly billed: charged the reservation
    NOT_BILLED = "not_billed"  # refused before generation


class CallOutcome(StrEnum):
    OK = "ok"
    OUTPUT_INVALID = "output_invalid"
    TIMEOUT = "timeout"
    API_DOWN = "api_down"
    ERROR = "error"


@dataclass(frozen=True)
class CostEntry:
    at_ms: int
    day: int  # UTC day index (at_ms // MS_PER_DAY)
    review_key: str
    signal_id: str
    role: str
    attempt: int
    model: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cost_usd: Decimal
    cost_basis: CostBasis
    latency_ms: int
    outcome: CallOutcome

    def to_json(self) -> str:
        d: dict[str, Any] = asdict(self)
        d["cost_usd"] = str(self.cost_usd)
        d["cost_basis"] = str(self.cost_basis)
        d["outcome"] = str(self.outcome)
        return json.dumps(d, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> CostEntry:
        d = json.loads(line)
        d["cost_usd"] = q(Decimal(d["cost_usd"]))
        d["cost_basis"] = CostBasis(d["cost_basis"])
        d["outcome"] = CallOutcome(d["outcome"])
        return cls(**d)


class LedgerUnreadable(Exception):
    pass


def read_ledger(path: Path) -> list[CostEntry]:
    if not path.exists():
        return []
    entries: list[CostEntry] = []
    try:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if line.strip():
                try:
                    entries.append(CostEntry.from_json(line))
                except (ValueError, KeyError, TypeError) as exc:
                    raise LedgerUnreadable(f"line {n}: {type(exc).__name__}") from None
    except OSError as exc:
        raise LedgerUnreadable(type(exc).__name__) from None
    return entries


class CostLedger:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.error: str | None = None
        self.entries: list[CostEntry] = []
        if path is not None:
            try:
                self.entries = read_ledger(path)
            except LedgerUnreadable as exc:
                self.error = f"cost ledger unreadable ({exc})"

    def append(self, entry: CostEntry) -> None:
        with self._lock:
            self.entries.append(entry)
            if self.path is not None:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with self.path.open("a", encoding="utf-8") as fh:
                        fh.write(entry.to_json() + "\n")
                except OSError as exc:  # spend can no longer be proven: stop spending
                    self.error = f"cost ledger not writable ({type(exc).__name__})"

    def spent_on(self, day: int) -> Decimal:
        with self._lock:
            return q(sum((e.cost_usd for e in self.entries if e.day == day), Decimal(0)))

    def calls_on(self, day: int) -> int:
        with self._lock:
            return sum(1 for e in self.entries if e.day == day)


class DailyBudget:
    def __init__(self, cap_usd: Decimal, ledger: CostLedger, clock: WallClock) -> None:
        self.cap = q(cap_usd)
        self.ledger = ledger
        self.clock = clock

    def today(self) -> int:
        return self.clock.now_ms() // MS_PER_DAY

    def spent_today(self) -> Decimal:
        return self.ledger.spent_on(self.today())

    def remaining(self) -> Decimal:
        return max(Decimal(0), q(self.cap - self.spent_today()))

    def can_afford(self, worst_case: Decimal) -> tuple[bool, str]:
        if self.ledger.error is not None:
            return False, self.ledger.error
        if self.spent_today() + worst_case > self.cap:
            return False, "daily budget exhausted"
        return True, ""
