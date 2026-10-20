"""Rule-based trading universe (decision D-04 as refined by L-08, DATA_ARCHITECTURE §6).

Two modes:

- **research** (L-08): the universe for backtests and research is frozen at S = 2025-01-01, the
  validation start. Snapshots with S after the holdout start are rejected in code, and every lake
  read goes through the holdout guard. Today's exchange *status* is not used (a delisting inside a
  segment is an event, not an exclusion); instead the symbol must have data through S on both
  markets.
- **live** (`--live`): the current snapshot for the live/forward universe only. It uses today's
  `TRADING` status and fetches its volumes over REST, without reading the lake.

Rule (parameters in `configs/universe_rule.yaml`), evaluated at snapshot date S (00:00 UTC):

1. listed on Binance Spot **and** as a USD-M perpetual with the same symbol name (contract
   multiplier symbols such as `1000PEPEUSDT` do not match -- conservative);
2. quote asset USDT on both; (live mode only) status `TRADING` on both;
3. base asset is not a stablecoin (list in the rule file) and not a leveraged token (base ending in
   UP/DOWN/BULL/BEAR whose remainder is itself a listed base asset, e.g. BTCUP);
4. listed for >= 24 months before S on both: USD-M by `onboardDate` <= S - 24 months; spot by the
   existence of native daily klines in the 7 days before S - 24 months;
5. data through S: a daily kline for the day before S on both markets;
6. ranked by the 90-day median daily quote volume over [S - 90 d, S); `rank_by` decides how the
   two markets combine (default `min`: liquid on both); a symbol needs >= `min_volume_days` days
   with data (missing days are never counted as zero volume);
7. top N (default 10).

Inputs are stored as evidence: the raw exchangeInfo payloads (with SHA-256) and the dataset ids of
every lake dataset read. `select_universe` is pure.

Known limitation: candidates come from today's exchangeInfo, so symbols Binance has removed from it
entirely cannot be candidates (survivorship).
"""

from __future__ import annotations

import hashlib
import json
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

from tsclaude.contracts.market import Market
from tsclaude.contracts.timeutil import MS_PER_DAY, to_iso
from tsclaude.marketdata.archive.paths import day_ms
from tsclaude.marketdata.types import ExchangeInfo
from tsclaude.store.holdout import HOLDOUT_START_MS

type RankBy = Literal["spot", "usdm", "min", "sum"]
type Mode = Literal["research", "live"]

RESEARCH_SNAPSHOT = date(2025, 1, 1)  # decision L-08


class UniverseError(ValueError):
    pass


def check_research_snapshot(snapshot: date) -> None:
    """L-08: research snapshots after the holdout start would rank on holdout information."""
    if day_ms(snapshot) > HOLDOUT_START_MS:
        raise UniverseError(
            f"research universe snapshot {snapshot} is after the holdout start; "
            f"use {RESEARCH_SNAPSHOT} (L-08) or --live for the forward universe"
        )


@dataclass(frozen=True)
class UniverseRule:
    quote_asset: str = "USDT"
    min_listing_months: int = 24
    listing_probe_days: int = 7
    volume_window_days: int = 90
    min_volume_days: int = 85
    top_n: int = 10
    rank_by: RankBy = "min"
    stablecoin_bases: frozenset[str] = frozenset()
    leveraged_suffixes: tuple[str, ...] = ("UP", "DOWN", "BULL", "BEAR")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> UniverseRule:
        allowed = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - allowed - {"version", "description"}
        if unknown:
            raise ValueError(f"unknown universe rule keys: {sorted(unknown)}")
        kwargs = {k: v for k, v in data.items() if k in allowed}
        if "stablecoin_bases" in kwargs:
            kwargs["stablecoin_bases"] = frozenset(
                str(s).upper() for s in kwargs["stablecoin_bases"]
            )
        if "leveraged_suffixes" in kwargs:
            kwargs["leveraged_suffixes"] = tuple(kwargs["leveraged_suffixes"])
        rule = cls(**kwargs)
        if rule.rank_by not in ("spot", "usdm", "min", "sum"):
            raise ValueError(f"rank_by must be spot|usdm|min|sum, not {rule.rank_by!r}")
        if rule.top_n < 1 or rule.min_volume_days > rule.volume_window_days:
            raise ValueError("invalid top_n or min_volume_days")
        return rule

    def to_dict(self) -> dict[str, Any]:
        return {
            "quote_asset": self.quote_asset,
            "min_listing_months": self.min_listing_months,
            "listing_probe_days": self.listing_probe_days,
            "volume_window_days": self.volume_window_days,
            "min_volume_days": self.min_volume_days,
            "top_n": self.top_n,
            "rank_by": self.rank_by,
            "stablecoin_bases": sorted(self.stablecoin_bases),
            "leveraged_suffixes": list(self.leveraged_suffixes),
        }


def load_rule(path: Path | str) -> UniverseRule:
    import yaml

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return UniverseRule.from_mapping(data)


def months_before(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + (d.month - 1) - months, 12)
    m += 1
    day = d.day
    while True:
        try:
            return date(y, m, day)
        except ValueError:
            day -= 1


@dataclass(frozen=True)
class Windows:
    snapshot_ms: int
    listing_cutoff_ms: int
    listing_probe: tuple[int, int]
    volume: tuple[int, int]


def windows(snapshot: date, rule: UniverseRule) -> Windows:
    s = day_ms(snapshot)
    cutoff = day_ms(months_before(snapshot, rule.min_listing_months))
    return Windows(
        snapshot_ms=s,
        listing_cutoff_ms=cutoff,
        listing_probe=(cutoff - rule.listing_probe_days * MS_PER_DAY, cutoff),
        volume=(s - rule.volume_window_days * MS_PER_DAY, s),
    )


@dataclass
class SymbolData:
    """Lake facts for one candidate (from native daily klines)."""

    spot_listed_by_cutoff: bool
    spot_daily_quote_volume: dict[int, float] = field(default_factory=dict)
    usdm_daily_quote_volume: dict[int, float] = field(default_factory=dict)


def _is_leveraged(base: str, all_bases: set[str], suffixes: Sequence[str]) -> bool:
    for suffix in suffixes:
        if base.endswith(suffix) and len(base) > len(suffix) and base[: -len(suffix)] in all_bases:
            return True
    return False


def static_candidates(
    spot: ExchangeInfo,
    usdm: ExchangeInfo,
    rule: UniverseRule,
    w: Windows,
    *,
    status_filter: bool,
) -> tuple[list[str], list[dict[str, str]]]:
    """Apply the exchangeInfo-only parts of the rule. Returns (candidates, exclusions).

    `status_filter` (live mode only) requires `TRADING` today on both markets.
    """
    excluded: list[dict[str, str]] = []
    candidates: list[str] = []
    bases = {s.base_asset for s in spot.symbols.values()}
    for symbol in sorted(spot.symbols):
        s = spot.symbols[symbol]
        if s.quote_asset != rule.quote_asset:
            continue  # not part of the candidate population at all
        f = usdm.symbols.get(symbol)
        reason = None
        if f is None:
            reason = "not listed on USD-M with the same symbol"
        elif f.contract_type != "PERPETUAL":
            reason = f"USD-M contract type {f.contract_type}"
        elif f.quote_asset != rule.quote_asset:
            reason = f"USD-M quote asset {f.quote_asset}"
        elif status_filter and (s.status != "TRADING" or f.status != "TRADING"):
            reason = f"status spot={s.status} usdm={f.status}"
        elif s.base_asset.upper() in rule.stablecoin_bases:
            reason = "stablecoin base"
        elif _is_leveraged(s.base_asset, bases, rule.leveraged_suffixes):
            reason = "leveraged token"
        elif f.onboard_date_ms is None:
            reason = "USD-M onboardDate unknown (fail closed)"
        elif f.onboard_date_ms > w.listing_cutoff_ms:
            reason = f"USD-M listed {to_iso(f.onboard_date_ms)}, after the 24-month cutoff"
        if reason is None:
            candidates.append(symbol)
        else:
            excluded.append({"symbol": symbol, "reason": reason})
    return candidates, excluded


def _median(vols: Mapping[int, float], window: tuple[int, int]) -> tuple[float | None, int]:
    values = [v for d, v in vols.items() if window[0] <= d < window[1]]
    return (statistics.median(values) if values else None), len(values)


def select_universe(
    candidates: Sequence[str],
    data: Mapping[str, SymbolData],
    rule: UniverseRule,
    w: Windows,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Rank candidates. Returns (ranked eligible list, exclusions)."""
    excluded: list[dict[str, str]] = []
    ranked: list[dict[str, Any]] = []
    for symbol in candidates:
        d = data.get(symbol)
        if d is None:
            excluded.append({"symbol": symbol, "reason": "no lake data"})
            continue
        if not d.spot_listed_by_cutoff:
            excluded.append({"symbol": symbol, "reason": "spot not listed 24 months before"})
            continue
        last_day = w.snapshot_ms - MS_PER_DAY
        spot_last = max(d.spot_daily_quote_volume, default=None)
        usdm_last = max(d.usdm_daily_quote_volume, default=None)
        if spot_last is None or usdm_last is None or min(spot_last, usdm_last) < last_day:
            excluded.append(
                {"symbol": symbol, "reason": "no data through the snapshot on both markets"}
            )
            continue
        spot_med, spot_days = _median(d.spot_daily_quote_volume, w.volume)
        usdm_med, usdm_days = _median(d.usdm_daily_quote_volume, w.volume)
        if spot_days < rule.min_volume_days or usdm_days < rule.min_volume_days:
            excluded.append(
                {
                    "symbol": symbol,
                    "reason": f"volume days spot={spot_days} usdm={usdm_days} "
                    f"< {rule.min_volume_days}",
                }
            )
            continue
        if spot_med is None or usdm_med is None:  # pragma: no cover - days >= min_volume_days
            continue
        score = {
            "spot": spot_med,
            "usdm": usdm_med,
            "min": min(spot_med, usdm_med),
            "sum": spot_med + usdm_med,
        }[rule.rank_by]
        ranked.append(
            {
                "symbol": symbol,
                "score": score,
                "spot_median_daily_quote_volume": spot_med,
                "usdm_median_daily_quote_volume": usdm_med,
                "spot_volume_days": spot_days,
                "usdm_volume_days": usdm_days,
            }
        )
    ranked.sort(key=lambda r: (-r["score"], r["symbol"]))
    for i, r in enumerate(ranked, start=1):
        r["rank"] = i
    return ranked, excluded


def raw_sha256(payload: Any) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snapshot_document(
    snapshot: date,
    rule: UniverseRule,
    w: Windows,
    ranked: Sequence[dict[str, Any]],
    excluded: Sequence[dict[str, str]],
    inputs: Mapping[str, Any],
    generated_at_ms: int,
    mode: Mode = "research",
) -> dict[str, Any]:
    top = list(ranked[: rule.top_n])
    return {
        "version": 2,
        "purpose": "research" if mode == "research" else "live_forward",
        "snapshot_date": snapshot.isoformat(),
        "generated_at": to_iso(generated_at_ms),
        "rule": rule.to_dict(),
        "windows": {
            "listing_cutoff": to_iso(w.listing_cutoff_ms),
            "listing_probe": [to_iso(w.listing_probe[0]), to_iso(w.listing_probe[1])],
            "volume": [to_iso(w.volume[0]), to_iso(w.volume[1])],
        },
        "symbols": [r["symbol"] for r in top],
        "spot": [r["symbol"] for r in top],
        "usdm": [r["symbol"] for r in top],
        "selected": top,
        "ranked_eligible": list(ranked),
        "excluded": list(excluded),
        "inputs": dict(inputs),
        "limitations": [
            "survivorship: candidates come from today's exchangeInfo",
            "contract-multiplier USD-M symbols (e.g. 1000PEPEUSDT) do not match spot symbols",
        ]
        + (
            ["live/forward universe only: never use it for research or backtests"]
            if mode == "live"
            else ["research universe frozen at S (L-08); today's status is not used"]
        ),
    }


def write_snapshot(doc: Mapping[str, Any], configs_dir: Path) -> Path:
    import yaml

    stem = "universe_snapshot" if doc.get("purpose") == "research" else "universe_live"
    path = configs_dir / f"{stem}_{doc['snapshot_date']}.yaml"
    header = (
        "# Generated by `tsclaude data universe`. Do not edit by hand: re-run the command.\n"
        "# Rule: decision D-04 / DATA_ARCHITECTURE §6; inputs and their hashes are listed below.\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        header + yaml.safe_dump(dict(doc), sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return path


def lake_facts(reader: Any, symbol: str, w: Windows) -> SymbolData:
    """Research mode: lake facts via LakeReader's holdout-guarded volume-only reads."""
    probe = reader.first_daily_open(Market.SPOT, symbol, *w.listing_probe)
    return SymbolData(
        spot_listed_by_cutoff=probe is not None,
        spot_daily_quote_volume=reader.daily_quote_volumes(Market.SPOT, symbol, *w.volume),
        usdm_daily_quote_volume=reader.daily_quote_volumes(Market.USDM, symbol, *w.volume),
    )


def snapshot_date_ok(snapshot: date, today: date) -> None:
    if snapshot > today:
        raise ValueError("snapshot date is in the future")
    if snapshot < today - timedelta(days=3650):
        raise ValueError("snapshot date implausibly old")


async def live_facts(rest: Mapping[Market, Any], symbol: str, w: Windows) -> SymbolData:
    """Live mode: the same facts from REST daily klines (the lake is not read)."""
    probe = await rest[Market.SPOT].klines(symbol, *w.listing_probe, "1d")
    spot = await rest[Market.SPOT].klines(symbol, *w.volume, "1d")
    usdm = await rest[Market.USDM].klines(symbol, *w.volume, "1d")
    return SymbolData(
        spot_listed_by_cutoff=bool(probe),
        spot_daily_quote_volume={k.open_time_ms: k.quote_volume for k in spot},
        usdm_daily_quote_volume={k.open_time_ms: k.quote_volume for k in usdm},
    )
