"""Cost ledger and daily budget (REQ-AGT-06; D-03: $1 per UTC day).

Reserve, then settle. Every call attempt writes two kinds of entry to an append-only JSONL ledger:

1. a ``reservation`` entry *before* the call, carrying the call's worst-case cost. It is written
   only if ``spent today + worst case <= cap``. The check and the append happen atomically under
   a thread lock and an OS file lock, after re-reading the file, so several gates or processes
   sharing one ledger file share one cap;
2. a ``settlement`` entry after the call, carrying the real cost (measured usage; worst case
   for a timeout or an unexpected error; zero for `ClientUnavailable`).

Spend for a call is its settlement cost if one exists, otherwise its reservation. A crash during
a call therefore leaves the worst case on the books after a restart.

The cap holds as long as every measured cost is at most its reservation. The reservation
assumes one token per input character plus the full ``max_tokens`` output. A measured cost
above the reservation is recorded as it is, never clamped down (the real spend is what counts),
and is counted as an over-reservation alert in the status.

Fail closed: any ledger line that is malformed fails validation (types, enums, finite cost of
at least 0, ``day == at_ms // MS_PER_DAY``, a settlement without its reservation, a duplicate).
The same holds for an unreadable or unwritable file. In all of these cases the day's spend is
unknown, and the budget refuses every reservation.

Money is `Decimal` quantized to 1e-8 USD (CLAUDE.md).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

from tsclaude.contracts.timeutil import WallClock

from .client import Usage
from .config import Pricing

QUANTUM = Decimal("0.00000001")
_MTOK = Decimal(1_000_000)
MS_PER_DAY = 86_400_000
LEDGER_SCHEMA = 2


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


class EntryKind(StrEnum):
    RESERVATION = "reservation"
    SETTLEMENT = "settlement"


class CostBasis(StrEnum):
    RESERVED = "reserved"  # worst case held before the call
    MEASURED = "measured"  # usage reported by the API
    WORST_CASE = "worst_case"  # usage unknown, possibly billed: charged the reservation
    NOT_BILLED = "not_billed"  # refused before generation


class CallOutcome(StrEnum):
    PENDING = "pending"  # reservation only
    OK = "ok"
    OUTPUT_INVALID = "output_invalid"
    TIMEOUT = "timeout"
    API_DOWN = "api_down"
    ERROR = "error"


_STR_FIELDS = ("call_id", "review_key", "signal_id", "role", "model")
_INT_FIELDS = (
    "schema",
    "at_ms",
    "day",
    "attempt",
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "latency_ms",
)


@dataclass(frozen=True)
class CostEntry:
    kind: EntryKind
    call_id: str
    at_ms: int  # the call's start (both entries): spend belongs to the day it was reserved
    day: int  # UTC day index == at_ms // MS_PER_DAY
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
    schema: int = LEDGER_SCHEMA

    def to_json(self) -> str:
        d: dict[str, Any] = asdict(self)
        for k in ("kind", "cost_basis", "outcome"):
            d[k] = str(d[k])
        d["cost_usd"] = str(self.cost_usd)
        return json.dumps(d, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> CostEntry:
        """Strict: any deviation raises ValueError (the caller fails closed)."""
        d = json.loads(line)
        if not isinstance(d, dict) or set(d) != set(cls.__dataclass_fields__):
            raise ValueError("unexpected or missing fields")
        for k in _STR_FIELDS:
            if not isinstance(d[k], str) or not d[k]:
                raise ValueError(f"{k} must be a non-empty string")
        for k in _INT_FIELDS:
            if type(d[k]) is not int or d[k] < 0:  # no bool, no float, no string
                raise ValueError(f"{k} must be a non-negative integer")
        if d["schema"] != LEDGER_SCHEMA:
            raise ValueError("unknown ledger schema")
        if d["day"] != d["at_ms"] // MS_PER_DAY:
            raise ValueError("day does not match at_ms")
        if not isinstance(d["cost_usd"], str):
            raise ValueError("cost_usd must be a decimal string")
        try:
            cost = Decimal(d["cost_usd"])
        except InvalidOperation:
            raise ValueError("cost_usd is not a decimal") from None
        if not cost.is_finite() or cost < 0:
            raise ValueError("cost_usd must be finite and >= 0")
        entry = cls(
            kind=EntryKind(d["kind"]),
            call_id=d["call_id"],
            at_ms=d["at_ms"],
            day=d["day"],
            review_key=d["review_key"],
            signal_id=d["signal_id"],
            role=d["role"],
            attempt=d["attempt"],
            model=d["model"],
            input_tokens=d["input_tokens"],
            output_tokens=d["output_tokens"],
            cached_input_tokens=d["cached_input_tokens"],
            cost_usd=q(cost),
            cost_basis=CostBasis(d["cost_basis"]),
            latency_ms=d["latency_ms"],
            outcome=CallOutcome(d["outcome"]),
        )
        reserved = entry.kind is EntryKind.RESERVATION
        if reserved != (entry.cost_basis is CostBasis.RESERVED) or reserved != (
            entry.outcome is CallOutcome.PENDING
        ):
            raise ValueError("kind, basis and outcome are inconsistent")
        return entry


class LedgerUnreadable(Exception):
    pass


@dataclass(frozen=True)
class CallSpend:
    day: int
    reserved: Decimal
    settled: Decimal | None

    @property
    def spend(self) -> Decimal:
        return self.reserved if self.settled is None else self.settled


def parse_ledger(lines: list[str]) -> list[CostEntry]:
    entries: list[CostEntry] = []
    for n, line in enumerate(lines, start=1):
        if line.strip():
            try:
                entries.append(CostEntry.from_json(line))
            except (ValueError, KeyError, TypeError) as exc:
                raise LedgerUnreadable(f"line {n}: {exc}") from None
    calls(entries)  # validates the reservation/settlement pairing
    return entries


def calls(entries: list[CostEntry]) -> dict[str, CallSpend]:
    out: dict[str, CallSpend] = {}
    for e in entries:
        prev = out.get(e.call_id)
        if e.kind is EntryKind.RESERVATION:
            if prev is not None:
                raise LedgerUnreadable(f"duplicate reservation {e.call_id}")
            out[e.call_id] = CallSpend(e.day, e.cost_usd, None)
        else:
            if prev is None or prev.settled is not None or prev.day != e.day:
                raise LedgerUnreadable(f"settlement without its reservation {e.call_id}")
            out[e.call_id] = CallSpend(prev.day, prev.reserved, e.cost_usd)
    return out


def read_ledger(path: Path) -> list[CostEntry]:
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LedgerUnreadable(type(exc).__name__) from None
    return parse_ledger(text.splitlines())


def spent_on(entries: list[CostEntry], day: int) -> Decimal:
    return q(sum((c.spend for c in calls(entries).values() if c.day == day), Decimal(0)))


def over_reservations(entries: list[CostEntry]) -> int:
    return sum(
        1 for c in calls(entries).values() if c.settled is not None and c.settled > c.reserved
    )


@contextlib.contextmanager
def _os_file_lock(path: Path) -> Iterator[None]:
    """Exclusive advisory lock on a sidecar file (POSIX: flock; Windows: msvcrt.locking)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # retries ~10 s, then raises
                    break
                except OSError:
                    continue
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class CostLedger:
    """JSONL ledger (or memory only, path None). File-backed ledgers re-read the file under the
    lock before every reservation, so the file, not this object, is the source of truth."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.error: str | None = None
        self._entries: list[CostEntry] = []
        if path is not None:
            with self._locked():
                self._refresh()

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        with self._lock:
            if self.path is None:
                yield
                return
            with _os_file_lock(self.path.with_name(self.path.name + ".lock")):
                yield

    def _refresh(self) -> None:
        if self.path is None:
            return
        try:
            self._entries = read_ledger(self.path)
        except LedgerUnreadable as exc:
            self.error = f"cost ledger unreadable ({exc})"

    def _append(self, entry: CostEntry) -> bool:
        if self.path is None:
            self._entries.append(entry)
            return True
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(entry.to_json() + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:  # spend can no longer be proven: stop spending
            self.error = f"cost ledger not writable ({type(exc).__name__})"
            return False
        self._entries.append(entry)
        return True

    def entries(self) -> list[CostEntry]:
        with self._locked():
            self._refresh()
            return list(self._entries)

    def try_reserve(self, entry: CostEntry, cap: Decimal) -> tuple[bool, str]:
        """Atomically: re-read, check ``spent + entry.cost <= cap``, append the reservation."""
        with self._locked():
            self._refresh()
            if self.error is not None:
                return False, self.error
            if spent_on(self._entries, entry.day) + entry.cost_usd > cap:
                return False, "daily budget exhausted"
            if not self._append(entry):
                return False, self.error or "cost ledger not writable"
            return True, ""

    def preview(self, day: int, amount: Decimal, cap: Decimal) -> tuple[bool, str]:
        with self._locked():
            self._refresh()
            if self.error is not None:
                return False, self.error
            if spent_on(self._entries, day) + amount > cap:
                return False, "daily budget exhausted"
            return True, ""

    def settle(self, entry: CostEntry) -> None:
        with self._locked():
            self._append(entry)  # a failed write leaves the reservation (worst case) in force

    def spent_on(self, day: int) -> Decimal:
        with self._locked():
            self._refresh()
            return spent_on(self._entries, day)

    def calls_on(self, day: int) -> int:
        with self._locked():
            self._refresh()
            return sum(1 for c in calls(self._entries).values() if c.day == day)

    def over_reservations(self) -> int:
        with self._locked():
            self._refresh()
            return over_reservations(self._entries)


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

    def can_afford(self, amount: Decimal) -> tuple[bool, str]:
        """Non-reserving check (used for the up-front two-call check)."""
        return self.ledger.preview(self.today(), amount, self.cap)

    def try_reserve(self, entry: CostEntry) -> tuple[bool, str]:
        return self.ledger.try_reserve(entry, self.cap)
