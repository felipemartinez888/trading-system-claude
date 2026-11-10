"""P&L-free feasibility count (audit finding H1-1): how many decision bars pass each entry stage.

Before a hypothesis is frozen, the owner needs to know whether a strategy's entry filters leave
enough candidate bars at all, in particular before and after the cost filter. This module counts
them on **development data only** and evaluates **no outcome**: no trade is simulated, there is no
exit, no R, no win/loss and no equity. It therefore registers no trial.

Pipeline (the same data path as a backtest, `research.runner` / `research.source`):

1. The range ``[from, to)`` must lie inside the development segment 2022-01-01 -> 2025-01-01 and
   be aligned to the strategy interval; anything else is refused before any read (a range that
   touches the sealed holdout raises `FeasibilityHoldoutRefused`). A strategy whose `diagnose`
   returns None is refused before any read too.
2. Closed 1m bars are read through the guarded ``LakeReader.read_bars(purpose=RESEARCH)`` month by
   month from ``from - warmup_ms_of(strategy)`` to ``to`` (the warm-up before ``from`` is the
   strategy's trailing window, exactly as a backtest reads it).
3. Per symbol they are aggregated with `BarAggregator` (complete buckets only). An incomplete
   bucket resets the strategy-bar history (counted in ``history_resets``), so no feature window
   joins bars across a gap.
4. For every derived strategy bar closing inside ``[from, to)`` (a *decision bar*) the strategy's
   `diagnose` is called on the trailing window of ``strategy.window_bars`` bars ending at that bar,
   with the planning costs a backtest would use (`planning_costs_for`, = ``runner._planning``).

Output per ``market:symbol`` and per UTC year of the decision bar's close: ``decision_bars``, the
**cumulative** counts in stage order (bars passing stages 1..k), ``missing_minutes`` (inside
``[from, to)``, by the year of the missing minute) and ``history_resets`` (by the year of the 1m bar
that revealed the gap; resets while reading the warm-up are reported under ``warmup``).

Interpretation limits: the count after the last stage is an **upper bound** on entries (position
overlap, validity windows and exits are not simulated) and is not evidence of an edge.

Diagnostics (feasibility-v2, all P&L-free, all from the same stage flags; mathematical audit F1, F2,
`docs/research/MATHEMATICAL_STRATEGY_FOUNDATIONS.md`), under ``diagnostics``:

- ``by_utc_hour``: decision bars and the cumulative stage counts by the UTC hour of the decision
  bar's close (overall), and decision bars / last-stage candidates by hour per symbol, with the
  share of candidates in the 6 busiest hours (a time-of-day confound check, audit A12);
- ``stage_filter``: per stage, the cumulative count, the pass rate from the previous stage, and the
  **marginal** count (bars passing that stage when every *structural* stage passes, whatever the
  other conditions say) and the **leave-one-out** count (bars passing every stage except that
  one): how binding each condition is. For a stage whose failure makes ``diagnose`` mark every
  later stage False (H1: warm-up, defined features, valid stop) leave-one-out is degenerate (it
  equals the candidate count);
- ``candidates_by_utc_month``: last-stage candidates (all symbols) by the ``YYYY-MM`` of the
  decision bar's close, so a power plan can count candidates inside the test windows instead of
  assuming they are spread evenly (`research.power_plan`);
- ``ablation_ladder`` (only for a strategy with a declared ladder, `ABLATION_LADDERS`): candidate
  counts, overall and by UTC hour, for each rung of nested condition sets, e.g. H1 trend →
  + breakout → + compression → + volume. These are the candidate pools a conditional random
  control would draw from. They are counts, not controls: no outcome is evaluated and nothing here
  enters selection.

Structural stages (`STRUCTURAL_STAGES`) are the ones that make a bar evaluable at all (warm-up,
defined features, a valid stop) and the cost filter, which every rung keeps.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from tsclaude.contracts.events import canonical_json
from tsclaude.contracts.market import Bar, Market
from tsclaude.contracts.timeutil import MS_PER_MINUTE, to_iso
from tsclaude.features.bars import BarAggregator
from tsclaude.research.runner import _months, segment_of, warmup_ms_of
from tsclaude.research.source import SymbolKey, _History
from tsclaude.sim.costs import CostModel
from tsclaude.store.holdout import Purpose, touches_holdout
from tsclaude.store.lake_reader import ReadResult
from tsclaude.strategies.base import PlanningCosts, Strategy

FEASIBILITY_VERSION = "feasibility-v2"  # v2: diagnostics (hour of day, stage filter, ladder)
KIND = "feasibility_count"

type CostsFor = Callable[[Market, str], PlanningCosts]

HOURS = 24
BUSIEST_HOURS = 6
#: stages that make a decision bar evaluable, plus the cost filter: kept in every marginal count
#: and every ablation rung (a condition is never "ablated" into an untradable or unevaluable bar)
STRUCTURAL_STAGES = frozenset({"warmup_ready", "features_defined", "stop_valid", "cost_ok"})
#: nested condition sets per strategy id, from the least to the most conditioned (audit F1, R-6).
#: The structural stages are added to every rung.
ABLATION_LADDERS: Mapping[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "h1_compression_breakout": (
        ("trend", ("trend_1h",)),
        ("trend+breakout", ("trend_1h", "breakout")),
        ("trend+breakout+compression", ("trend_1h", "breakout", "compressed")),
        ("trend+breakout+compression+volume", ("trend_1h", "breakout", "compressed", "volume_ok")),
    ),
}


class FeasibilityRefused(ValueError):
    """The request is not a development-segment feasibility count (nothing was read)."""


class FeasibilityHoldoutRefused(FeasibilityRefused):
    """The requested range touches the sealed holdout (nothing was read)."""


class BarReader(Protocol):
    """The part of `LakeReader` used here (tests wrap it to record calls)."""

    def read_bars(
        self,
        market: Market,
        symbol: str,
        start_ms: int,
        end_ms: int,
        *,
        purpose: Purpose | str,
        prereg_id: str | None = None,
        strategy_hash: str | None = None,
        dataset_ids: Collection[str] | None = None,
    ) -> ReadResult[Bar]: ...


def planning_costs_for(cost_model: CostModel) -> CostsFor:
    """The planning costs a backtest gives the strategy (identical to ``runner._planning``)."""

    def costs_for(market: Market, symbol: str) -> PlanningCosts:
        return PlanningCosts.from_cost_model(cost_model, market, symbol)

    return costs_for


def utc_year(ms: int) -> int:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).year


def _year_start_ms(year: int) -> int:
    return int(datetime(year, 1, 1, tzinfo=UTC).timestamp() * 1000)


def _minutes_by_year(a: int, b: int) -> Iterator[tuple[int, int]]:
    """Minutes of the missing range [a, b) split at UTC year boundaries."""
    cur = a
    while cur < b:
        year = utc_year(cur)
        nxt = min(b, _year_start_ms(year + 1))
        yield year, (nxt - cur) // MS_PER_MINUTE
        cur = nxt


def stage_names(strategy: Strategy[Any]) -> tuple[str, ...]:
    """Stage names declared by `diagnose` (probed on an empty window); refused if None."""
    probe = strategy.diagnose(_History().window(strategy.window_bars), _PROBE_COSTS)
    if probe is None:
        raise FeasibilityRefused(
            f"strategy {strategy.strategy_id!r} declares no entry stages (diagnose returns None)"
        )
    names = tuple(probe)
    if not names or len(set(names)) != len(names):
        raise FeasibilityRefused("diagnose must return at least one uniquely named stage")
    return names


_PROBE_COSTS = PlanningCosts(0.0, "probe (stage names only)")


def check_request(strategy: Strategy[Any], from_ms: int, to_ms: int) -> None:
    """Refuse anything but an interval-aligned range inside the development segment."""
    if to_ms <= from_ms:
        raise FeasibilityRefused("to must be after from")
    read_from = from_ms - warmup_ms_of(strategy)
    if touches_holdout(read_from, to_ms):
        raise FeasibilityHoldoutRefused(
            f"[{to_iso(from_ms)}, {to_iso(to_ms)}) touches the sealed holdout; feasibility counts "
            "run on the development segment only (2022-01-01 to 2025-01-01)"
        )
    segment = segment_of(from_ms, to_ms)
    if segment != "development":
        raise FeasibilityRefused(
            f"[{to_iso(from_ms)}, {to_iso(to_ms)}) is {segment}, not development; feasibility "
            "counts run on the development segment only (2022-01-01 to 2025-01-01)"
        )
    step = strategy.interval.ms
    if from_ms % step or to_ms % step:
        raise FeasibilityRefused(f"from/to must be aligned to the strategy interval {step} ms")


@dataclass(frozen=True, slots=True)
class DecisionFlags:
    """Stage flags of one decision bar (diagnostic, never stored in the count's output)."""

    market: Market
    symbol: str
    decision_ms: int  # close time of the decision (strategy) bar
    stages: Mapping[str, bool]


@dataclass
class _Year:
    decision_bars: int = 0
    passed: list[int] = field(default_factory=list)
    missing_minutes: int = 0
    history_resets: int = 0


@dataclass
class _SymbolLog:
    years: dict[int, _Year] = field(default_factory=dict)
    hour_decisions: list[int] = field(default_factory=lambda: [0] * HOURS)
    hour_candidates: list[int] = field(default_factory=lambda: [0] * HOURS)
    warmup_missing_minutes: int = 0
    warmup_history_resets: int = 0
    conflicts: int = 0
    bars_1m: int = 0

    def year(self, y: int, n_stages: int) -> _Year:
        out = self.years.get(y)
        if out is None:
            out = self.years[y] = _Year(passed=[0] * n_stages)
        return out


def iter_decisions(
    reader: BarReader,
    strategy: Strategy[Any],
    symbols: Sequence[SymbolKey],
    from_ms: int,
    to_ms: int,
    *,
    costs_for: CostsFor,
    dataset_ids: Collection[str] | None = None,
    logs: dict[SymbolKey, _SymbolLog] | None = None,
    datasets: set[str] | None = None,
) -> Iterator[DecisionFlags]:
    """Stage flags of every decision bar closing in [from, to), per symbol in order.

    Validates the request first (nothing is read when it is refused)."""
    check_request(strategy, from_ms, to_ms)
    names = stage_names(strategy)
    if not symbols:
        raise FeasibilityRefused("at least one symbol is required")
    costs = {key: costs_for(*key) for key in symbols}  # unknown instrument: raises (fail closed)
    read_from = from_ms - warmup_ms_of(strategy)
    logs = {} if logs is None else logs
    datasets = set() if datasets is None else datasets
    pinned = None if dataset_ids is None else list(dataset_ids)
    # guard check over the whole read range with nothing selected (as `run_backtest` does)
    for market, symbol in symbols:
        reader.read_bars(
            market,
            symbol,
            read_from,
            to_ms,
            purpose=Purpose.RESEARCH,
            strategy_hash=strategy.strategy_hash,
            dataset_ids=[],
        )
    for key in symbols:
        market, symbol = key
        log = logs.setdefault(key, _SymbolLog())
        agg = BarAggregator(strategy.interval)
        hist = _History()
        for lo, hi in _months(read_from, to_ms):
            res = reader.read_bars(
                market,
                symbol,
                lo,
                hi,
                purpose=Purpose.RESEARCH,
                strategy_hash=strategy.strategy_hash,
                dataset_ids=pinned,
            )
            datasets.update(res.dataset_ids)
            log.conflicts += len(res.conflicts)
            log.bars_1m += len(res.items)
            for a, b in res.missing:
                wa, wb = a, min(b, from_ms)
                if wb > wa:
                    log.warmup_missing_minutes += (wb - wa) // MS_PER_MINUTE
                ia, ib = max(a, from_ms), min(b, to_ms)
                for year, minutes in _minutes_by_year(ia, ib) if ib > ia else ():
                    log.year(year, len(names)).missing_minutes += minutes
            for bar in res.items:
                before = agg.incomplete
                derived = agg.push(bar)
                if agg.incomplete != before:
                    # same rule as `StrategySource`: a dropped bucket breaks the history
                    hist = _History()
                    if bar.close_time_ms < from_ms:
                        log.warmup_history_resets += 1
                    else:
                        log.year(utc_year(bar.close_time_ms), len(names)).history_resets += 1
                if derived is None:
                    continue
                hist.append(derived)
                decision = derived.close_time_ms
                if not from_ms <= decision < to_ms:
                    continue
                flags = strategy.diagnose(hist.window(strategy.window_bars), costs[key])
                if flags is None or tuple(flags) != names:
                    raise ValueError(
                        f"diagnose returned stages {None if flags is None else list(flags)}, "
                        f"declared {list(names)}"
                    )
                yield DecisionFlags(market, symbol, decision, dict(flags))


def count_feasibility(
    reader: BarReader,
    strategy: Strategy[Any],
    symbols: Sequence[SymbolKey],
    from_ms: int,
    to_ms: int,
    *,
    costs_for: CostsFor,
    dataset_ids: Collection[str] | None = None,
) -> dict[str, Any]:
    """Cumulative stage-pass counts per symbol and UTC year (see the module docstring)."""
    check_request(strategy, from_ms, to_ms)
    names = stage_names(strategy)
    logs: dict[SymbolKey, _SymbolLog] = {}
    datasets: set[str] = set()
    diag = _Diagnostics(names, ABLATION_LADDERS.get(strategy.strategy_id, ()))
    for d in iter_decisions(
        reader,
        strategy,
        symbols,
        from_ms,
        to_ms,
        costs_for=costs_for,
        dataset_ids=dataset_ids,
        logs=logs,
        datasets=datasets,
    ):
        log = logs[(d.market, d.symbol)]
        yr = log.year(utc_year(d.decision_ms), len(names))
        yr.decision_bars += 1
        for k, name in enumerate(names):
            if not d.stages[name]:
                break
            yr.passed[k] += 1
        hour = utc_hour(d.decision_ms)
        log.hour_decisions[hour] += 1
        if all(d.stages[n] for n in names):
            log.hour_candidates[hour] += 1
        diag.add(d.stages, hour, utc_month(d.decision_ms))

    years = range(utc_year(from_ms), utc_year(to_ms - 1) + 1)
    per_symbol: dict[str, Any] = {}
    for key in symbols:
        log = logs.get(key, _SymbolLog())
        by_year = {str(y): _year_out(log.year(y, len(names)), names) for y in years}
        total = _Year(passed=[0] * len(names))
        for y in years:
            src = log.year(y, len(names))
            total.decision_bars += src.decision_bars
            total.missing_minutes += src.missing_minutes
            total.history_resets += src.history_resets
            total.passed = [a + b for a, b in zip(total.passed, src.passed, strict=True)]
        per_symbol[f"{key[0]}:{key[1]}"] = {
            "years": by_year,
            "total": _year_out(total, names),
            "warmup": {
                "missing_minutes": log.warmup_missing_minutes,
                "history_resets": log.warmup_history_resets,
            },
            "conflicts": log.conflicts,
            "bars_1m": log.bars_1m,
            "by_utc_hour": {
                "decision_bars": list(log.hour_decisions),
                "candidates": list(log.hour_candidates),
                "busiest_hours_candidate_share": busiest_share(log.hour_candidates),
            },
        }
    costs = {f"{m}:{s}": costs_for(m, s) for m, s in symbols}
    read_from = from_ms - warmup_ms_of(strategy)
    result: dict[str, Any] = {
        "kind": KIND,
        "version": FEASIBILITY_VERSION,
        "provenance": "BACKTEST",
        "pnl_free": True,
        "segment": "development",
        "strategy": strategy.describe(),
        "range": {
            "from_ms": from_ms,
            "to_ms": to_ms,
            "from": to_iso(from_ms),
            "to": to_iso(to_ms),
            "read_from_ms": read_from,
            "warmup_ms": warmup_ms_of(strategy),
        },
        "stages": list(names),
        "counting": (
            "cumulative in stage order: cumulative[k] counts decision bars passing stages 1..k; "
            "the last stage is an upper bound on entries (no overlap, validity or exit simulated)"
        ),
        "planning_costs": {
            k: {"round_trip_frac": c.round_trip_frac, "basis": c.basis}
            for k, c in sorted(costs.items())
        },
        "symbols": per_symbol,
        "diagnostics": diag.out(),
        "dataset_ids": sorted(datasets),
    }
    result["content_hash"] = content_hash(result)
    return result


def utc_hour(ms: int) -> int:
    """UTC hour of a decision bar's close time. Close times end at ``hh:59:59.999`` for bars of
    up to one hour, so this is the hour the bar opened in."""
    return (ms // 3_600_000) % HOURS


def utc_month(ms: int) -> str:
    """``YYYY-MM`` of a decision bar's close time, UTC."""
    t = datetime.fromtimestamp(ms / 1000, tz=UTC)
    return f"{t.year:04d}-{t.month:02d}"


def busiest_share(by_hour: Sequence[int], k: int = BUSIEST_HOURS) -> float | None:
    """Share of the counts in the ``k`` busiest hours (uniform: k / 24 = 0.25 for k = 6)."""
    total = sum(by_hour)
    return None if total == 0 else sum(sorted(by_hour, reverse=True)[:k]) / total


class _Diagnostics:
    """P&L-free statistics over the stage flags of every decision bar (see the module docs)."""

    def __init__(self, names: Sequence[str], ladder: Sequence[tuple[str, Sequence[str]]]) -> None:
        self.names = tuple(names)
        self.structural = tuple(n for n in self.names if n in STRUCTURAL_STAGES)
        unknown = sorted({c for _, rung in ladder for c in rung} - set(self.names))
        if unknown:
            raise ValueError(f"ablation ladder names unknown stages {unknown}")
        self.ladder = tuple((label, tuple(rung)) for label, rung in ladder)
        self.decisions = 0
        self.cumulative = [0] * len(self.names)
        self.marginal = [0] * len(self.names)
        self.leave_one_out = [0] * len(self.names)
        self.rungs = [0] * len(self.ladder)
        self.rung_hours = [[0] * HOURS for _ in self.ladder]
        self.hour_decisions = [0] * HOURS
        self.hour_cumulative = [[0] * HOURS for _ in self.names]
        self.month_candidates: dict[str, int] = {}

    def add(self, stages: Mapping[str, bool], hour: int, month: str) -> None:
        self.decisions += 1
        self.hour_decisions[hour] += 1
        for k, name in enumerate(self.names):
            if not stages[name]:
                break
            self.cumulative[k] += 1
            self.hour_cumulative[k][hour] += 1
        if self.names and all(stages[n] for n in self.names):
            self.month_candidates[month] = self.month_candidates.get(month, 0) + 1
        base = all(stages[n] for n in self.structural)
        failed = [n for n in self.names if not stages[n]]
        for k, name in enumerate(self.names):
            if base and stages[name]:
                self.marginal[k] += 1
            if not failed or failed == [name]:
                self.leave_one_out[k] += 1
        for r, (_, rung) in enumerate(self.ladder):
            if base and all(stages[c] for c in rung):
                self.rungs[r] += 1
                self.rung_hours[r][hour] += 1

    def out(self) -> dict[str, Any]:
        rows = []
        prev = self.decisions
        for k, name in enumerate(self.names):
            rows.append({
                "stage": name,
                "structural": name in STRUCTURAL_STAGES,
                "cumulative": self.cumulative[k],
                "pass_rate_from_previous": None if prev == 0 else self.cumulative[k] / prev,
                "marginal": self.marginal[k],
                "leave_one_out": self.leave_one_out[k],
            })  # fmt: skip
            prev = self.cumulative[k]
        out: dict[str, Any] = {
            "decision_bars": self.decisions,
            "stage_filter": rows,
            "by_utc_hour": {
                "decision_bars": list(self.hour_decisions),
                "cumulative": {
                    n: list(h) for n, h in zip(self.names, self.hour_cumulative, strict=True)
                },
                "busiest_hours": BUSIEST_HOURS,
                "busiest_hours_candidate_share": busiest_share(self.hour_cumulative[-1])
                if self.names
                else None,
                "busiest_hours_decision_share": busiest_share(self.hour_decisions),
            },
            "candidates_by_utc_month": dict(sorted(self.month_candidates.items())),
            "structural_stages": list(self.structural),
            "notes": (
                "P&L-free counts from the stage flags; marginal = structural stages and that "
                "stage pass; leave_one_out = every stage except that one passes. A stage whose "
                "failure makes the strategy mark every later stage False (H1: warmup_ready, "
                "features_defined, stop_valid) has leave_one_out equal to the candidate count, "
                "which carries no information. by_utc_hour.cumulative is per stage in stage "
                "order; ablation_ladder[].by_utc_hour is per rung. Not used by selection or "
                "certification"
            ),
        }
        if self.ladder:
            out["ablation_ladder"] = [
                {
                    "rung": label,
                    "conditions": list(rung),
                    "candidates": n,
                    "by_utc_hour": list(hours),
                    "busiest_hours_share": busiest_share(hours),
                }
                for (label, rung), n, hours in zip(
                    self.ladder, self.rungs, self.rung_hours, strict=True
                )
            ]
        return out


def content_hash(result: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical JSON of the result without its ``content_hash``."""
    body = {k: v for k, v in result.items() if k != "content_hash"}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def _year_out(y: _Year, names: Sequence[str]) -> dict[str, Any]:
    return {
        "decision_bars": y.decision_bars,
        "cumulative": [{"stage": n, "bars": c} for n, c in zip(names, y.passed, strict=True)],
        "missing_minutes": y.missing_minutes,
        "history_resets": y.history_resets,
    }
