"""Registry of exchange-wide interruptions (ADR-0013; owner decisions D-09, D-10).

A registered interval is a period in which the exchange did not trade (for example the Binance
spot halt of 2023-03-24). It is historical knowledge used by backtests only:
- minutes inside it are reported as exchange downtime instead of missing data, so an approved,
  source-proven, market-wide halt does not grade every run X, while any other missing minute
  still does (`max_missing_share` stays 0.0);
- the simulator fills nothing inside it and closes positions exposed to it at the first traded
  open afterwards (aligned with L-11).
Live and paper never use it: a real interruption there is handled by the fail-closed live rules.

The file is versioned and its SHA-256 goes into every run manifest. Any schema problem fails closed
(`InterruptionRegistryError`): no run starts with an unreadable registry.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import yaml

from tsclaude.contracts.market import Market
from tsclaude.contracts.timeutil import MS_PER_MINUTE

SCHEMA_VERSION = 1
DEFAULT_PATH = Path("configs/exchange_interruptions.yaml")


class InterruptionRegistryError(ValueError):
    """The registry file is missing, malformed or inconsistent."""


@dataclass(frozen=True, slots=True)
class ExchangeInterruption:
    id: str
    market: Market
    from_ms: int  # inclusive, minute-aligned
    to_ms: int  # exclusive, minute-aligned

    def contains(self, open_ms: int) -> bool:
        return self.from_ms <= open_ms < self.to_ms

    def as_tuple(self) -> tuple[Market, int, int, str]:
        """The plain form handed to the simulator (which does not import this module)."""
        return (self.market, self.from_ms, self.to_ms, self.id)


@dataclass(frozen=True, slots=True)
class InterruptionRegistry:
    entries: tuple[ExchangeInterruption, ...]
    sha256: str | None  # of the file bytes; None for the built-in empty registry
    #: the file name only: the content is identified by `sha256`, and a run manifest must not
    #: depend on how the path was typed (relative or absolute) nor leak local directories
    source: str | None

    def for_market(self, market: Market) -> tuple[ExchangeInterruption, ...]:
        return tuple(e for e in self.entries if e.market is market)

    def split_missing(self, market: Market, lo: int, hi: int) -> tuple[int, int, list[str]]:
        """Minutes of the missing range [lo, hi): (inside registered intervals, outside, ids)."""
        inside = 0
        ids: list[str] = []
        for e in self.for_market(market):
            a, b = max(lo, e.from_ms), min(hi, e.to_ms)
            if b > a:
                inside += (b - a) // MS_PER_MINUTE
                ids.append(e.id)
        total = max(0, hi - lo) // MS_PER_MINUTE
        return inside, total - inside, ids

    def ids_overlapping(self, markets: Iterable[Market], lo: int, hi: int) -> list[str]:
        """Ids of the entries of these markets that intersect [lo, hi), whether their minutes
        are absent or published as zero-trade fillers."""
        wanted = set(markets)
        return sorted(
            e.id for e in self.entries if e.market in wanted and e.from_ms < hi and e.to_ms > lo
        )

    def simulator_intervals(self) -> tuple[tuple[Market, int, int, str], ...]:
        return tuple(e.as_tuple() for e in self.entries)

    def manifest(self) -> dict[str, Any]:
        """What a run manifest records about the registry it used."""
        return {
            "schema": SCHEMA_VERSION,
            "source": self.source,
            "sha256": self.sha256,
            "entries": [
                {"id": e.id, "market": str(e.market), "from_ms": e.from_ms, "to_ms": e.to_ms}
                for e in self.entries
            ],
        }


EMPTY_REGISTRY = InterruptionRegistry((), None, None)


def _ms(value: Any, field: str, entry_id: str) -> int:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise InterruptionRegistryError(
            f"{entry_id}: {field} must be an ISO-8601 UTC string ending in Z"
        )
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InterruptionRegistryError(f"{entry_id}: {field} is not a valid timestamp") from exc
    ms = int(dt.astimezone(UTC).timestamp() * 1000)
    if ms % MS_PER_MINUTE:
        raise InterruptionRegistryError(f"{entry_id}: {field} must be minute-aligned")
    return ms


def parse_registry(data: Any, *, sha256: str | None, source: str | None) -> InterruptionRegistry:
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
        raise InterruptionRegistryError(f"registry schema must be {SCHEMA_VERSION}")
    raw = data.get("interruptions")
    if not isinstance(raw, list):
        raise InterruptionRegistryError("'interruptions' must be a list")
    entries: list[ExchangeInterruption] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise InterruptionRegistryError("every interruption must be a mapping")
        entry_id = item.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            raise InterruptionRegistryError("every interruption needs an id")
        if entry_id in seen:
            raise InterruptionRegistryError(f"duplicate interruption id {entry_id}")
        seen.add(entry_id)
        try:
            market = Market(str(item.get("market")))
        except ValueError as exc:
            raise InterruptionRegistryError(f"{entry_id}: unknown market") from exc
        if item.get("scope") != "exchange-wide":
            raise InterruptionRegistryError(f"{entry_id}: scope must be 'exchange-wide'")
        for required in ("signature", "external_sources", "approved_by"):
            if not item.get(required):
                raise InterruptionRegistryError(f"{entry_id}: '{required}' is required (ADR-0013)")
        lo, hi = _ms(item.get("from"), "from", entry_id), _ms(item.get("to"), "to", entry_id)
        if hi <= lo:
            raise InterruptionRegistryError(f"{entry_id}: 'to' must be after 'from'")
        entries.append(ExchangeInterruption(entry_id, market, lo, hi))
    entries.sort(key=lambda e: (str(e.market), e.from_ms))
    for a, b in pairwise(entries):
        if a.market is b.market and b.from_ms < a.to_ms:
            raise InterruptionRegistryError(f"{a.id} and {b.id} overlap")
    return InterruptionRegistry(tuple(entries), sha256, source)


def load_registry(path: Path | str = DEFAULT_PATH) -> InterruptionRegistry:
    """Read and validate the registry file; any problem fails closed."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise InterruptionRegistryError(f"cannot read {p}: {exc}") from exc
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise InterruptionRegistryError(f"{p} is not valid YAML") from exc
    return parse_registry(data, sha256=hashlib.sha256(raw).hexdigest(), source=p.name)
