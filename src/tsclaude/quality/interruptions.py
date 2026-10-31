"""Registry of exchange-wide interruptions (ADR-0013; owner decisions D-09, D-10, D-09A, D-10A).

A registered interval is a period in which the exchange did not trade (for example the Binance
spot halt of 2023-03-24). It is historical knowledge used by backtests:
- the simulator fills nothing inside the whole interval `[from, to)` and closes positions exposed
  to it at the first traded open afterwards (aligned with L-11);
- only its documented absence period `[absent_from, to)` is eligible registered downtime: a
  missing minute there is reported as exchange downtime instead of missing data. Before
  `absent_from` the exchange still published candles (zero-trade fillers, D-10A), so a minute
  missing there is genuine missing data and grades the run X like any other unregistered gap
  (`max_missing_share` stays 0.0).
Live and paper never use it: a real interruption there is handled by the fail-closed live rules.

The import QA also reads it, for data-quality classification only (ADR-0013 section 6; M2-E2): it
tells observed zero-trade candles apart from registered downtime and from unexpected missing
minutes. That use extends no exemption and changes no stored bar and no order execution.

Governance (D-09A): every entry carries its approval date, and an interval longer than 24 hours is
refused unless a documented exceptional override names the approver, approval date, reason and
reference. Unknown fields are refused too, so a mistyped policy field can never be ignored.

The file is versioned and its SHA-256 goes into every run manifest, together with every policy
field of every entry. Any schema problem fails closed (`InterruptionRegistryError`): no run starts
with an unreadable or incomplete registry.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import yaml

from tsclaude.contracts.market import Market
from tsclaude.contracts.timeutil import MS_PER_MINUTE

#: schema 2 (E-1b): required `absent_from` and `approved_at`, the 24-hour limit, no unknown fields
SCHEMA_VERSION = 2
DEFAULT_PATH = Path("configs/exchange_interruptions.yaml")
MAX_INTERVAL_MS = 24 * 60 * MS_PER_MINUTE  # longer intervals need an exceptional override

_ENTRY_FIELDS = frozenset(
    {
        "id",
        "market",
        "from",
        "to",
        "absent_from",
        "scope",
        "signature",
        "external_sources",
        "approved_by",
        "approved_at",
        "exceptional_override",
    }
)
_OVERRIDE_FIELDS = ("approved_by", "approved_at", "reason", "reference")


class InterruptionRegistryError(ValueError):
    """The registry file is missing, malformed or inconsistent."""


@dataclass(frozen=True, slots=True)
class ExchangeInterruption:
    id: str
    market: Market
    from_ms: int  # inclusive, minute-aligned: start of the non-trading interval
    to_ms: int  # exclusive, minute-aligned
    #: inclusive, minute-aligned, from_ms <= absent_from_ms < to_ms: start of the documented
    #: absence period; candles before it were published (observed zero-trade fillers)
    absent_from_ms: int
    approved_at: str  # ISO date of the owner approval (D-09A)
    override: tuple[tuple[str, str], ...] = ()  # exceptional override (> 24 h), sorted fields

    def contains(self, open_ms: int) -> bool:
        return self.from_ms <= open_ms < self.to_ms

    def as_tuple(self) -> tuple[Market, int, int, str]:
        """The plain form handed to the simulator (which does not import this module). The
        simulator freezes the whole interval, observed fillers included."""
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
        """Minutes of the missing range [lo, hi): (inside a documented absence period, outside,
        ids). Only `[absent_from, to)` is eligible; a missing minute in `[from, absent_from)`,
        where candles were published, counts as outside (genuine missing data)."""
        inside = 0
        ids: list[str] = []
        for e in self.for_market(market):
            a, b = max(lo, e.absent_from_ms), min(hi, e.to_ms)
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
        """What a run manifest records about the registry it used: every policy field of every
        entry, so the manifest hash changes with any of them even without a file hash."""
        return {
            "schema": SCHEMA_VERSION,
            "source": self.source,
            "sha256": self.sha256,
            "entries": [
                {
                    "id": e.id,
                    "market": str(e.market),
                    "from_ms": e.from_ms,
                    "to_ms": e.to_ms,
                    "absent_from_ms": e.absent_from_ms,
                    "approved_at": e.approved_at,
                    "exceptional_override": dict(e.override) or None,
                }
                for e in self.entries
            ],
        }


EMPTY_REGISTRY = InterruptionRegistry((), None, None)


_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate mapping keys (PyYAML silently keeps the last one, so a
    reviewer reading the file could see a different value than the one used)."""


def _construct_mapping(loader: _StrictLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    keys: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node)
        if key in keys:
            raise InterruptionRegistryError(f"duplicate key {key!r} in the registry file")
        keys.add(key)
    return loader.construct_mapping(node)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _text(value: Any, field: str, entry_id: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InterruptionRegistryError(f"{entry_id}: '{field}' must be a non-blank string")
    return value.strip()


def _ms(value: Any, field: str, entry_id: str) -> int:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise InterruptionRegistryError(
            f"{entry_id}: {field} must be an ISO-8601 UTC timestamp "
            "YYYY-MM-DDTHH:MM[:SS] ending in Z"
        )
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InterruptionRegistryError(f"{entry_id}: {field} is not a valid timestamp") from exc
    ms = int(dt.astimezone(UTC).timestamp() * 1000)
    if ms % MS_PER_MINUTE:
        raise InterruptionRegistryError(f"{entry_id}: {field} must be minute-aligned")
    return ms


def _approval_date(value: Any, field: str, entry_id: str) -> str:
    """An ISO calendar date `YYYY-MM-DD` (YAML may already have parsed it into a date)."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and _DATE.fullmatch(value):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    raise InterruptionRegistryError(f"{entry_id}: {field} must be an ISO date YYYY-MM-DD")


def _override(value: Any, entry_id: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict):
        raise InterruptionRegistryError(f"{entry_id}: exceptional_override must be a mapping")
    unknown = sorted(set(value) - set(_OVERRIDE_FIELDS))
    if unknown:
        raise InterruptionRegistryError(
            f"{entry_id}: unknown exceptional_override field(s) {unknown}"
        )
    out: dict[str, str] = {}
    for f in _OVERRIDE_FIELDS:
        v = value.get(f)
        if f == "approved_at":
            out[f] = _approval_date(v, "exceptional_override.approved_at", entry_id)
        elif isinstance(v, str) and v.strip():
            out[f] = v.strip()
        else:
            raise InterruptionRegistryError(
                f"{entry_id}: exceptional_override.{f} is required (D-09A)"
            )
    return tuple(sorted(out.items()))


def parse_registry(data: Any, *, sha256: str | None, source: str | None) -> InterruptionRegistry:
    if (
        not isinstance(data, dict)
        or type(data.get("schema")) is not int  # 2.0 or True must not pass as 2
        or data.get("schema") != SCHEMA_VERSION
    ):
        raise InterruptionRegistryError(f"registry schema must be the integer {SCHEMA_VERSION}")
    unknown_top = sorted(set(data) - {"schema", "interruptions"})
    if unknown_top:
        raise InterruptionRegistryError(f"unknown registry field(s) {unknown_top}")
    raw = data.get("interruptions")
    if not isinstance(raw, list):
        raise InterruptionRegistryError("'interruptions' must be a list")
    entries: list[ExchangeInterruption] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise InterruptionRegistryError("every interruption must be a mapping")
        entry_id = item.get("id")
        if not isinstance(entry_id, str) or not entry_id.strip() or entry_id != entry_id.strip():
            raise InterruptionRegistryError("every interruption needs a non-blank id")
        if entry_id in seen:
            raise InterruptionRegistryError(f"duplicate interruption id {entry_id}")
        seen.add(entry_id)
        unknown = sorted(set(item) - _ENTRY_FIELDS)
        if unknown:
            raise InterruptionRegistryError(f"{entry_id}: unknown field(s) {unknown}")
        try:
            market = Market(str(item.get("market")))
        except ValueError as exc:
            raise InterruptionRegistryError(f"{entry_id}: unknown market") from exc
        if item.get("scope") != "exchange-wide":
            raise InterruptionRegistryError(f"{entry_id}: scope must be 'exchange-wide'")
        # evidence fields (ADR-0013): typed, never just "truthy"
        signature = item.get("signature")
        if not isinstance(signature, dict) or not signature:
            raise InterruptionRegistryError(
                f"{entry_id}: 'signature' is required as a non-empty mapping (ADR-0013)"
            )
        sources = item.get("external_sources")
        if (
            not isinstance(sources, list)
            or not sources
            or not all(isinstance(s, str) and s.strip() for s in sources)
        ):
            raise InterruptionRegistryError(
                f"{entry_id}: 'external_sources' is required as a non-empty list of "
                "non-blank strings (ADR-0013)"
            )
        _text(item.get("approved_by"), "approved_by", entry_id)
        approved_at = _approval_date(item.get("approved_at"), "approved_at", entry_id)
        lo, hi = _ms(item.get("from"), "from", entry_id), _ms(item.get("to"), "to", entry_id)
        if hi <= lo:
            raise InterruptionRegistryError(f"{entry_id}: 'to' must be after 'from'")
        absent = _ms(item.get("absent_from"), "absent_from", entry_id)
        if not lo <= absent < hi:
            raise InterruptionRegistryError(
                f"{entry_id}: 'absent_from' must lie in [from, to) (D-10A)"
            )
        override: tuple[tuple[str, str], ...] = ()
        if "exceptional_override" in item:
            override = _override(item["exceptional_override"], entry_id)
        if hi - lo > MAX_INTERVAL_MS and not override:
            raise InterruptionRegistryError(
                f"{entry_id}: interval longer than 24 hours needs a documented "
                "exceptional_override (D-09A)"
            )
        if override and hi - lo <= MAX_INTERVAL_MS:
            raise InterruptionRegistryError(
                f"{entry_id}: exceptional_override is only for intervals longer than 24 hours"
            )
        entries.append(
            ExchangeInterruption(entry_id, market, lo, hi, absent, approved_at, override)
        )
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
        data = yaml.load(raw.decode("utf-8"), Loader=_StrictLoader)  # noqa: S506 (safe subclass)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise InterruptionRegistryError(f"{p} is not valid YAML") from exc
    return parse_registry(data, sha256=hashlib.sha256(raw).hexdigest(), source=p.name)
