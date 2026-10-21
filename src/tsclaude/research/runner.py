"""Backtest runner: guarded lake data -> strategy -> simulator -> certify -> graded report.

One call (`run_backtest`) produces ``runs/<run_id>/`` with ``manifest.json``, ``trades.parquet``,
``equity.parquet`` and ``metrics.json`` (SIMULATION §8). Steps:

1. **Data.** Bars are read only through `LakeReader.read_bars(purpose="research")`, month by
   month, from ``from - warm-up`` to ``to``. The holdout guard decides: a range touching the
   sealed holdout raises `HoldoutLocked` unless the run carries the preregistration id and the
   strategy hash of a recorded unlock. Dataset ids actually used are recorded (and can be pinned
   to rerun a manifest exactly). Missing minutes inside ``[from, to)`` and conflicts are counted.
2. **Trial registry.** The configuration is recorded before results exist; ``n_trials`` for
   `certify` is the registry's count for the hypothesis (preregistration id, else strategy id).
3. **Simulation.** One `Simulator` with three books on the same signals: ``baseline`` (cost
   model) and ``stress_x1.5`` / ``stress_x2`` (`CostModel.scaled`). Risk hook: the simulator's
   stub (the M3 risk engine is not wired yet). A second run is the **random-entry control**.
4. **max_loss_r** (L-10, ADR-0012 Amendment 5): the strategy is simulated on the development
   segment for the same symbols; the worst stop-loss outcome in R (gap-throughs included), as a
   loss magnitude, times ``max_loss_safety_factor`` (1.5), and never below the planned stop loss
   times that factor. Without any development stop-loss trade the fallback is
   ``factor x planned stop loss`` and the grade is capped at C. ``max_win_r`` for brackets is the
   take-profit net of fees (maximum over trades, never below the best observed outcome).
5. **Certification and report** (`research.report`), integrity checks, artifacts.

Reproducibility: ``manifest_hash`` covers every input that can change a result (strategy id,
version, params, symbols, range, dataset ids, configs, cost model, seed, n_trials, code commit is
recorded but not hashed). Same inputs -> identical ``metrics.json`` bytes; the metrics SHA-256 is
returned and stored in the manifest's ``outputs`` (outside the hashed part).
"""

from __future__ import annotations

import hashlib
import heapq
import json
import shutil
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from tsclaude.contracts.events import canonical_json
from tsclaude.contracts.market import Bar, Market
from tsclaude.contracts.signals import SignalStatus
from tsclaude.contracts.timeutil import MS_PER_MINUTE, from_iso, to_iso
from tsclaude.features.core import FEATURES_VERSION
from tsclaude.research import report
from tsclaude.research.parquet import write_table
from tsclaude.research.source import StrategySource, SymbolKey
from tsclaude.research.stats import Certification, certify
from tsclaude.research.trials import TrialRegistry
from tsclaude.sim.book import Book, BookConfig, SimEventType, TradeRecord, allow_all
from tsclaude.sim.clock import HistoricalClock, order_key
from tsclaude.sim.costs import CostModel, Liquidity
from tsclaude.sim.engine import SIM_ENGINE_VERSION, SimResult, Simulator
from tsclaude.sim.fills import ExitReason
from tsclaude.sim.orders import InstrumentSpec
from tsclaude.store.holdout import HOLDOUT_END_MS, HOLDOUT_START_MS, Purpose
from tsclaude.store.lake_reader import LakeReader
from tsclaude.strategies.base import PlanningCosts, Strategy

RUNNER_VERSION = "runner-v1"
MANIFEST_SCHEMA = 1
DEV_START_MS = from_iso("2022-01-01T00:00:00Z")
DEV_END_MS = from_iso("2025-01-01T00:00:00Z")
VAL_START_MS = DEV_END_MS
VAL_END_MS = HOLDOUT_START_MS
BASELINE = "baseline"
CONTROL = "control"
REQUIRED_MANIFEST_FIELDS = (
    "schema",
    "run_id",
    "runner_version",
    "strategy",
    "hypothesis_id",
    "prereg_id",
    "symbols",
    "range",
    "segment",
    "datasets",
    "config",
    "config_hash",
    "cost_model",
    "features_version",
    "sim_engine_version",
    "code",
    "seed",
    "timezone",
    "trials",
    "max_loss",
    "manifest_hash",
)


# ---------------------------------------------------------------- configuration
@dataclass(frozen=True)
class BacktestConfig:
    version: str = "backtest-config-v1"
    starting_equity: Decimal = Decimal("10000")
    risk_per_trade: Decimal = Decimal("0.005")
    decision_latency_ms: int = 0
    alpha: float = 0.05
    min_power: float = 0.80
    n_resamples: int = 10_000
    max_missing_share: float = 0.0
    flagged_share_cap: float = 0.05
    max_loss_safety_factor: float = 1.5
    stress_factors: tuple[str, ...] = ("1.5", "2")
    flat_margin_bars: int = 1
    instruments: Mapping[str, Mapping[str, Mapping[str, str]]] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> BacktestConfig:
        known = set(cls.__dataclass_fields__)
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ValueError(f"unknown backtest config keys: {unknown}")
        kw: dict[str, Any] = dict(raw)
        for k in ("starting_equity", "risk_per_trade"):
            if k in kw:
                kw[k] = Decimal(str(kw[k]))
        if "stress_factors" in kw:
            kw["stress_factors"] = tuple(str(x) for x in kw["stress_factors"])
        cfg = cls(**kw)
        if cfg.max_loss_safety_factor < 1.5:
            raise ValueError("max_loss_safety_factor may not be below 1.5 (L-10)")
        if not 0.0 <= cfg.max_missing_share < 1.0:
            raise ValueError("max_missing_share must be in [0, 1)")
        return cfg

    @classmethod
    def load(cls, path: Path | str) -> BacktestConfig:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.from_mapping(raw)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "starting_equity": str(self.starting_equity),
            "risk_per_trade": str(self.risk_per_trade),
            "decision_latency_ms": self.decision_latency_ms,
            "alpha": self.alpha,
            "min_power": self.min_power,
            "n_resamples": self.n_resamples,
            "max_missing_share": self.max_missing_share,
            "flagged_share_cap": self.flagged_share_cap,
            "max_loss_safety_factor": self.max_loss_safety_factor,
            "stress_factors": list(self.stress_factors),
            "flat_margin_bars": self.flat_margin_bars,
            "instruments": {
                m: {s: dict(v) for s, v in sorted(syms.items())}
                for m, syms in sorted(self.instruments.items())
            },
        }

    @property
    def config_hash(self) -> str:
        return _sha(self.to_dict())

    def instrument(self, market: Market, symbol: str) -> InstrumentSpec:
        """Instrument spec from the config (symbol entry, else the market's ``default``)."""
        by_market = self.instruments.get(str(market), {})
        spec = by_market.get(symbol) or by_market.get("default")
        if spec is None:
            raise ValueError(f"no instrument spec for {market}:{symbol} (fail closed)")
        return InstrumentSpec(
            market,
            symbol,
            Decimal(str(spec["tick_size"])),
            Decimal(str(spec["lot_size"])),
            Decimal(str(spec.get("min_notional", "0"))),
        )


@dataclass
class BacktestRequest:
    strategy: Strategy[Any]
    symbols: Sequence[SymbolKey]
    from_ms: int
    to_ms: int
    seed: int = 0
    prereg_id: str | None = None
    hypothesis_id: str | None = None
    dataset_ids: Mapping[str, Sequence[str]] | None = None  # pin: "main" / "max_loss"
    max_loss_r_override: float | None = None  # a preregistration may only raise the default

    @property
    def hypothesis(self) -> str:
        return self.hypothesis_id or self.prereg_id or self.strategy.strategy_id


@dataclass
class RunEnv:
    reader: LakeReader
    trials: TrialRegistry
    runs_dir: Path
    now_ms: Callable[[], int]
    cost_model: CostModel = field(default_factory=CostModel)
    code_version: Callable[[], dict[str, Any]] = field(default_factory=lambda: git_version)


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    run_dir: Path
    manifest: dict[str, Any]
    metrics: dict[str, Any]
    metrics_hash: str


# ---------------------------------------------------------------- helpers
def _sha(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def git_version() -> dict[str, Any]:
    """Code commit of the working tree (recorded, not hashed); unknown if git is unavailable."""
    git = shutil.which("git")
    if git is None:
        return {"commit": "unknown", "dirty": None}
    head = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [git, "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    status = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [git, "status", "--porcelain", "--untracked-files=no"],
        capture_output=True,
        text=True,
        check=False,
    )
    if head.returncode != 0:
        return {"commit": "unknown", "dirty": None}
    return {"commit": head.stdout.strip(), "dirty": bool(status.stdout.strip())}


def segment_of(from_ms: int, to_ms: int) -> str:
    for name, lo, hi in (
        ("development", DEV_START_MS, DEV_END_MS),
        ("validation", VAL_START_MS, VAL_END_MS),
        ("holdout", HOLDOUT_START_MS, HOLDOUT_END_MS),
    ):
        if lo <= from_ms and to_ms <= hi:
            return name
    if from_ms < DEV_START_MS and to_ms <= DEV_START_MS:
        return "outside"
    return "mixed"


def _months(start_ms: int, end_ms: int) -> Iterator[tuple[int, int]]:
    """[start, end) split at UTC month boundaries."""
    cur = start_ms
    while cur < end_ms:
        d = datetime.fromtimestamp(cur / 1000, tz=UTC)
        y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        nxt = int(datetime(y, m, 1, tzinfo=UTC).timestamp() * 1000)
        yield cur, min(nxt, end_ms)
        cur = nxt


@dataclass
class _DataLog:
    dataset_ids: set[str] = field(default_factory=set)
    conflicts: int = 0
    missing_minutes: int = 0  # inside the evaluated range only
    bars: int = 0


def _stream(
    env: RunEnv,
    req: BacktestRequest,
    start_ms: int,
    end_ms: int,
    count_from_ms: int,
    log: _DataLog,
    pinned: Sequence[str] | None,
) -> Iterator[Bar]:
    """Guarded month-by-month read of every symbol, merged into global clock order."""
    for lo, hi in _months(start_ms, end_ms):
        chunks: list[list[Bar]] = []
        for market, symbol in req.symbols:
            res = env.reader.read_bars(
                market,
                symbol,
                lo,
                hi,
                purpose=Purpose.RESEARCH,
                prereg_id=req.prereg_id,
                strategy_hash=req.strategy.strategy_hash,
                dataset_ids=pinned,
            )
            log.dataset_ids.update(res.dataset_ids)
            log.conflicts += len(res.conflicts)
            for a, b in res.missing:
                a, b = max(a, count_from_ms), min(b, end_ms)
                if b > a:
                    log.missing_minutes += (b - a) // MS_PER_MINUTE
            log.bars += len(res.items)
            chunks.append(res.items)
        yield from heapq.merge(*chunks, key=order_key)


def _books(
    env: RunEnv, req: BacktestRequest, cfg: BacktestConfig, names: Sequence[tuple[str, CostModel]]
) -> list[Book]:
    specs = [cfg.instrument(m, s) for m, s in req.symbols]
    book_cfg = BookConfig(risk_per_trade=cfg.risk_per_trade)
    return [
        Book(name, cfg.starting_equity, cm, allow_all, instruments=specs, config=book_cfg)
        for name, cm in names
    ]


def _simulate(
    env: RunEnv,
    req: BacktestRequest,
    cfg: BacktestConfig,
    books: list[Book],
    source: StrategySource,
    read_from_ms: int,
    end_ms: int,
    count_from_ms: int,
    pinned: Sequence[str] | None,
) -> tuple[SimResult, _DataLog]:
    log = _DataLog()
    sim = Simulator(HistoricalClock([]), books, source, decision_latency_ms=cfg.decision_latency_ms)
    for bar in _stream(env, req, read_from_ms, end_ms, count_from_ms, log, pinned):
        sim.step(bar)
    return sim.finish(), log


def _planning(env: RunEnv, req: BacktestRequest) -> dict[SymbolKey, PlanningCosts]:
    return {(m, s): PlanningCosts.from_cost_model(env.cost_model, m, s) for m, s in req.symbols}


def _source(
    req: BacktestRequest,
    planning: Mapping[SymbolKey, PlanningCosts],
    trade_from_ms: int,
    end_ms: int,
    cfg: BacktestConfig,
    *,
    schedule: Mapping[SymbolKey, frozenset[int]] | None = None,
    prefix: str = "s",
) -> StrategySource:
    margin = cfg.flat_margin_bars * req.strategy.interval.ms
    return StrategySource(
        req.strategy,
        planning,
        trade_from_ms=trade_from_ms,
        flat_from_ms=end_ms - margin,
        schedule=schedule,
        request_prefix=prefix,
    )


def _control_schedule(
    main: StrategySource, trades: Sequence[TradeRecord], seed: int
) -> dict[SymbolKey, frozenset[int]]:
    """Same number of entries per (symbol, UTC hour) as the strategy, at random decision bars."""
    decision_of = {r.request_id: r.decision_ms for r in main.records if r.request_id}
    wanted: dict[tuple[SymbolKey, int], int] = {}
    for t in trades:
        d = decision_of.get(t.request_id)
        if d is None:
            continue
        hk = ((t.market, t.symbol), ((d + 1) // 3_600_000) % 24)
        wanted[hk] = wanted.get(hk, 0) + 1
    rng = np.random.default_rng(seed)
    out: dict[SymbolKey, set[int]] = {}
    for (key, hour), k in sorted(wanted.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        bars = main.decision_bars.get(key, [])
        pool = sorted(d for d in bars if ((d + 1) // 3_600_000) % 24 == hour)
        if not pool:
            continue
        idx = rng.choice(len(pool), size=min(k, len(pool)), replace=False)
        out.setdefault(key, set()).update(pool[int(i)] for i in sorted(idx))
    return {k: frozenset(v) for k, v in out.items()}


def _max_win_r(trades: Sequence[TradeRecord], cost_model: CostModel) -> tuple[float | None, str]:
    """Largest take-profit outcome net of fees over the trades (bracket designs)."""
    best: float | None = None
    for t in trades:
        if t.take_profit_price is None:
            continue
        sign = Decimal(1) if str(t.side) == "long" else Decimal(-1)
        taker = cost_model.fee_rate(t.market, Liquidity.TAKER)
        maker = cost_model.fee_rate(t.market, Liquidity.MAKER)
        fee_entry = (taker if t.entry_liquidity is Liquidity.TAKER else maker) * t.entry_price
        fee_exit = (taker if cost_model.tp_as_taker else maker) * t.take_profit_price
        net = (sign * (t.take_profit_price - t.entry_price) - fee_entry - fee_exit) * t.qty
        value = float(net / t.risk_amount)
        best = value if best is None else max(best, value)
    if best is None:
        return None, "observed"
    return best, "take-profit net of fees (max over trades)"


def measure_max_loss(env: RunEnv, req: BacktestRequest, cfg: BacktestConfig) -> dict[str, Any]:
    """L-10 default: worst development-segment stop-loss outcome (R) x safety factor."""
    planning = _planning(env, req)
    first = planning[req.symbols[0]]
    planned_stop = req.strategy.stop_loss_r(first) or 1.0
    factor = cfg.max_loss_safety_factor
    source = _source(req, planning, DEV_START_MS, DEV_END_MS, cfg, prefix="ml")
    books = _books(env, req, cfg, [(BASELINE, env.cost_model)])
    pinned = None if req.dataset_ids is None else req.dataset_ids.get("max_loss")
    _, log = _simulate(env, req, cfg, books, source, DEV_START_MS, DEV_END_MS, DEV_START_MS, pinned)
    stops = [t for t in books[0].trades if t.exit_reason is ExitReason.STOP_LOSS]
    worst = min((float(t.r_multiple) for t in stops), default=None)
    out: dict[str, Any] = {
        "dataset_ids": sorted(log.dataset_ids),
        "dev_range": [to_iso(DEV_START_MS), to_iso(DEV_END_MS)],
        "dev_trades": len(books[0].trades),
        "dev_stop_loss_trades": len(stops),
        "dev_gap_fill_stops": sum(1 for t in stops if t.gap_fill),
        "worst_stop_loss_r": worst,
        "planned_stop_loss_r": planned_stop,
        "safety_factor": factor,
    }
    if worst is None:
        value = factor * planned_stop
        out.update(
            measured=False,
            value=value,
            source=(
                f"fallback: no development-segment stop-loss trades; {factor} x planned stop "
                f"loss {planned_stop:.6f}R"
            ),
        )
    else:
        value = factor * max(-worst, planned_stop)
        out.update(
            measured=True,
            value=value,
            source=(
                f"{factor} x worst development-segment stop-loss outcome "
                f"{worst:.6f}R over {len(stops)} stop-loss trades (simulator, {SIM_ENGINE_VERSION})"
            ),
        )
    if req.max_loss_r_override is not None:
        if req.max_loss_r_override < value:
            raise ValueError("max_loss_r_override may only raise the measured default (L-10)")
        out.update(
            value=req.max_loss_r_override,
            source=f"preregistration override (raised from {value:.6f}R)",
        )
    return out


def _certify(
    r: Sequence[float],
    *,
    p_star: float | None,
    stop_loss_r: float | None,
    cfg: BacktestConfig,
    n_trials: int,
    seed: int,
    max_loss: Mapping[str, Any],
    max_win_r: float | None,
) -> Certification | None:
    if len(r) < 2:
        return None
    mw = None if max_win_r is None else max(max_win_r, max(r))
    return certify(
        r,
        p_star=p_star,
        alpha=cfg.alpha,
        n_trials=n_trials,
        min_power=cfg.min_power,
        mean_block=None,
        seed=seed,
        max_loss_r=float(max_loss["value"]),
        max_loss_source=str(max_loss["source"]),
        max_win_r=mw,
        stop_loss_r=stop_loss_r,
        n_resamples=cfg.n_resamples,
    )


def _timing_problems(source: StrategySource, books: Sequence[Book], latency_ms: int) -> list[str]:
    """Causality checks on the realized run: no decision before its bar closed, no fill before
    the next 1m open after the decision."""
    problems: list[str] = []
    decided = {r.request_id: r for r in source.records if r.request_id}
    for r in source.records:
        if r.delivered_ms < r.decision_ms:
            problems.append(f"proposal at {r.decision_ms} delivered before its bar closed")
    for book in books:
        for t in book.trades:
            rec = decided.get(t.request_id)
            if rec is None:
                problems.append(f"{book.name}:{t.trade_id} has no recorded decision")
            elif t.entry_bar_open_ms < rec.decision_ms + 1 + latency_ms:
                problems.append(f"{book.name}:{t.trade_id} filled before its decision time")
    return problems


def _trade_rows(book: Book) -> list[dict[str, Any]]:
    rows = []
    for t in report.order_trades(book.trades):
        p = t.to_payload()
        p["r"] = float(t.r_multiple)
        rows.append(p)
    return rows


TRADE_SCHEMA: list[tuple[str, str]] = [
    ("trade_id", "str"),
    ("book", "str"),
    ("market", "str"),
    ("symbol", "str"),
    ("side", "str"),
    ("request_id", "str"),
    ("signal_id", "str?"),
    ("entry_bar_open_ms", "int"),
    ("entry_at_open", "bool"),
    ("entry_price", "str"),
    ("entry_liquidity", "str"),
    ("qty", "str"),
    ("stop_price", "str"),
    ("take_profit_price", "str?"),
    ("exit_bar_open_ms", "int"),
    ("exit_at_open", "bool"),
    ("exit_price", "str"),
    ("exit_liquidity", "str"),
    ("exit_reason", "str"),
    ("gap_fill", "bool"),
    ("ambiguous_bar", "bool"),
    ("spans_gap", "bool"),
    ("gross_pnl", "str"),
    ("fees", "str"),
    ("funding", "str"),
    ("slippage_cost", "str"),
    ("net_pnl", "str"),
    ("risk_amount", "str"),
    ("r_multiple", "str"),
    ("mae_r", "str"),
    ("mfe_r", "str"),
    ("alt_r_if_tp_first", "str?"),
    ("r", "float"),
]
EQUITY_SCHEMA: list[tuple[str, str]] = [
    ("book", "str"),
    ("ts_ms", "int"),
    ("equity", "str"),
    ("equity_f", "float"),
]


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- the run
def run_backtest(
    req: BacktestRequest, env: RunEnv, cfg: BacktestConfig | None = None
) -> RunOutcome:
    cfg = cfg or BacktestConfig()
    if not req.symbols:
        raise ValueError("at least one symbol is required")
    if req.to_ms <= req.from_ms:
        raise ValueError("to must be after from")
    strat = req.strategy
    interval_ms = strat.interval.ms
    if req.from_ms % interval_ms or req.to_ms % interval_ms:
        raise ValueError(f"from/to must be aligned to the strategy interval {strat.interval}")
    if req.to_ms - req.from_ms <= cfg.flat_margin_bars * interval_ms:
        raise ValueError("range is shorter than the end-of-run flat margin")
    warmup_ms = strat.window_bars * interval_ms
    read_from = req.from_ms - warmup_ms
    segment = segment_of(req.from_ms, req.to_ms)
    created = env.now_ms()
    planning = _planning(env, req)

    # 1. guard check over the whole read range (no parts selected): a run touching the sealed
    #    holdout without a recorded unlock is refused before anything is recorded
    for market, symbol in req.symbols:
        env.reader.read_bars(
            market,
            symbol,
            read_from,
            req.to_ms,
            purpose=Purpose.RESEARCH,
            prereg_id=req.prereg_id,
            strategy_hash=strat.strategy_hash,
            dataset_ids=[],
        )

    # 2. trial registry (before results exist)
    stamp = datetime.fromtimestamp(created / 1000, tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    trial_config = {
        "symbols": [f"{m}:{s}" for m, s in req.symbols],
        "from_ms": req.from_ms,
        "to_ms": req.to_ms,
        "segment": segment,
        "config_hash": cfg.config_hash,
        "cost_model": env.cost_model.describe(),
    }
    provisional_id = f"bt-{stamp}-{_sha([strat.params_hash, trial_config, req.seed])[:8]}"
    trial = env.trials.record(
        hypothesis_id=req.hypothesis,
        strategy_id=strat.strategy_id,
        strategy_version=strat.version,
        params_hash=strat.params_hash,
        config=trial_config,
        run_id=provisional_id,
        now_ms=created,
    )

    # 3. main run: baseline + cost stress books on the same signals
    stress = [(f"stress_x{f}", env.cost_model.scaled(Decimal(f))) for f in cfg.stress_factors]
    books = _books(env, req, cfg, [(BASELINE, env.cost_model), *stress])
    main = _source(req, planning, req.from_ms, req.to_ms, cfg, prefix="s")
    pinned = None if req.dataset_ids is None else req.dataset_ids.get("main")
    result, log = _simulate(env, req, cfg, books, main, read_from, req.to_ms, req.from_ms, pinned)
    by_name = {b.name: b for b in books}
    base = by_name[BASELINE]
    base_trades = report.order_trades(base.trades)

    # random-entry control: same exits, same entries per (symbol, UTC hour), seeded
    schedule = _control_schedule(main, base_trades, req.seed)
    control_books = _books(env, req, cfg, [(CONTROL, env.cost_model)])
    control_src = _source(req, planning, req.from_ms, req.to_ms, cfg, schedule=schedule, prefix="c")
    control_result, _ = _simulate(
        env, req, cfg, control_books, control_src, read_from, req.to_ms, req.from_ms, pinned
    )
    control_trades = report.order_trades(control_books[0].trades)

    # 4. max_loss_r on the development segment (L-10)
    max_loss = measure_max_loss(env, req, cfg)
    first = planning[req.symbols[0]]
    p_star = strat.design_p_star(first)
    stop_loss_r = strat.stop_loss_r(first)
    max_win, max_win_source = _max_win_r(base_trades, env.cost_model)

    # 5. certification
    r = report.outcomes(base_trades)
    cert = _certify(
        r,
        p_star=p_star,
        stop_loss_r=stop_loss_r,
        cfg=cfg,
        n_trials=trial.n_trials,
        seed=req.seed,
        max_loss=max_loss,
        max_win_r=max_win,
    )
    stress_certs: dict[str, Certification | None] = {}
    stress_stats: dict[str, Any] = {}
    stress_means: dict[str, float | None] = {}
    for name, cm in stress:
        label = name.removeprefix("stress_")
        st = report.order_trades(by_name[name].trades)
        sr = report.outcomes(st)
        sw, _ = _max_win_r(st, cm)
        c = _certify(
            sr,
            p_star=None,
            stop_loss_r=stop_loss_r,
            cfg=cfg,
            n_trials=trial.n_trials,
            seed=req.seed,
            max_loss=max_loss,
            max_win_r=sw,
        )
        stress_certs[label] = c
        stress_means[label] = report._mean(sr)
        stress_stats[label] = {
            "cost_model": cm.version,
            **report.trade_stats(st, cfg.alpha),
            "certified": None if c is None else c.certified,
            "margin_lcb": None if c is None else c.margin_lcb,
            "expectancy_lcb": None if c is None else c.expectancy_lcb,
            "reasons": [] if c is None else list(c.reasons),
        }
    alpha_adj = cfg.alpha / trial.n_trials
    control = report.control_comparison(r, report.outcomes(control_trades), alpha_adj)
    control["scheduled_entries"] = sum(len(v) for v in schedule.values())
    control["source_errors"] = _count(control_result, SimEventType.SOURCE_ERROR)

    # integrity
    evaluated_minutes = (req.to_ms - req.from_ms) // MS_PER_MINUTE * len(req.symbols)
    missing_share = log.missing_minutes / evaluated_minutes if evaluated_minutes else 0.0
    problems: list[str] = []
    if missing_share > cfg.max_missing_share:
        problems.append(
            f"data gaps: {log.missing_minutes} missing 1m bars in range "
            f"(share {missing_share:.6f} > tolerance {cfg.max_missing_share})"
        )
    if log.bars == 0:
        problems.append("no bars in range")
    problems += _timing_problems(main, books, cfg.decision_latency_ms)
    problems += _timing_problems(control_src, control_books, cfg.decision_latency_ms)
    for b in [*books, *control_books]:
        if not b.ledger.reconcile().ok:
            problems.append(f"ledger of {b.name} does not reconcile")
    errors = _count(result, SimEventType.SOURCE_ERROR)
    if errors:
        problems.append(f"{errors} signal-source error(s) during the run")

    # manifest (hashed part first)
    hashed: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "runner_version": RUNNER_VERSION,
        "strategy": {**strat.describe(), "strategy_hash": strat.strategy_hash},
        "hypothesis_id": req.hypothesis,
        "prereg_id": req.prereg_id,
        "symbols": [f"{m}:{s}" for m, s in req.symbols],
        "range": {
            "from": to_iso(req.from_ms),
            "to": to_iso(req.to_ms),
            "from_ms": req.from_ms,
            "to_ms": req.to_ms,
            "warmup_from_ms": read_from,
        },
        "segment": segment,
        "datasets": {
            "main": sorted(log.dataset_ids),
            "max_loss": list(max_loss["dataset_ids"]),
        },
        "config": cfg.to_dict(),
        "config_hash": cfg.config_hash,
        "cost_model": {**env.cost_model.describe(), "planning": first.basis},
        "features_version": FEATURES_VERSION,
        "sim_engine_version": SIM_ENGINE_VERSION,
        "report_version": report.REPORT_VERSION,
        "seed": req.seed,
        "timezone": "UTC",
        "trials": {
            "trial_key": trial.trial_key,
            "n_trials": trial.n_trials,
            "counted_by": "research_trials registry",
        },
        "max_loss": {"value": max_loss["value"], "source": max_loss["source"]},
        "risk_hook": "sim.book.allow_all (stub; M3 risk engine not wired)",
    }
    manifest_hash = _sha(hashed)
    run_id = f"bt-{stamp}-{manifest_hash[:10]}"
    manifest: dict[str, Any] = {
        **hashed,
        "run_id": run_id,
        "manifest_hash": manifest_hash,
        "code": env.code_version(),
        "created_at": to_iso(created),
        "provenance": "BACKTEST",
    }
    missing_fields = [f for f in REQUIRED_MANIFEST_FIELDS if manifest.get(f, "") in ("", None)]
    missing_fields = [f for f in missing_fields if f != "prereg_id"]  # None = no prereg (valid)
    if missing_fields:
        problems.append(f"manifest fields missing: {missing_fields}")

    # report
    stats = report.trade_stats(base_trades, cfg.alpha)
    grade, reasons = report.grade(
        report.GradeInputs(
            segment=segment,
            prereg_id=req.prereg_id,
            integrity_problems=problems,
            cert=cert,
            n_trades=len(r),
            flagged_share_cap=cfg.flagged_share_cap,
            ambiguous_share=stats["ambiguous_share"],
            spans_gap_share=stats["spans_gap_share"],
            max_loss_measured=bool(max_loss["measured"]),
            stress=stress_certs,
            stress_means=stress_means,
            beats_control=control["beats_control"],
        )
    )
    watch = sum(1 for x in main.records if x.proposal.stage is SignalStatus.WATCH)
    metrics = report.clean(
        {
            "report_version": report.REPORT_VERSION,
            "manifest_hash": manifest_hash,
            "provenance": "BACKTEST",
            "segment": segment,
            "evidence_grade": grade,
            "grade_reasons": reasons,
            "n_trials": trial.n_trials,
            "trades": stats,
            "certification": None if cert is None else report.certification_dict(cert),
            "certification_inputs": {
                "p_star_design": p_star,
                "stop_loss_r": stop_loss_r,
                "max_loss_r": max_loss["value"],
                "max_loss_source": max_loss["source"],
                "max_loss_measurement": {
                    k: v for k, v in max_loss.items() if k not in ("dataset_ids",)
                },
                "max_win_r": max_win,
                "max_win_source": max_win_source,
                "alpha": cfg.alpha,
                "min_power": cfg.min_power,
            },
            "equity": report.equity_stats(base.equity_curve, base.summary()),
            "activity": report.exposure_turnover(
                base_trades, cfg.starting_equity, req.to_ms - req.from_ms, len(req.symbols)
            ),
            "breakdowns": report.breakdowns(base_trades),
            "cost_stress": stress_stats,
            "random_control": control,
            "signals": {
                "watch": watch,
                "confirmed": sum(1 for x in main.records if x.request_id),
                "decision_bars": sum(len(v) for v in main.decision_bars.values()),
                "incomplete_strategy_buckets": main.incomplete_buckets,
            },
            "integrity": {
                "problems": problems,
                "bars_read": log.bars,
                "missing_minutes_in_range": log.missing_minutes,
                "missing_share": missing_share,
                "conflicts": log.conflicts,
                "bar_gap_events": _count(result, SimEventType.BAR_GAP),
                "rejections": base.summary()["rejections_by_reason"],
                "open_positions_at_end": base.summary()["open_positions"],
                "sim_fingerprint": result.fingerprint,
                "ledger_hash": result.ledger_hashes[BASELINE],
            },
        }
    )
    metrics_text = canonical_json(metrics)
    metrics_hash = hashlib.sha256(metrics_text.encode("utf-8")).hexdigest()

    # artifacts
    run_dir = Path(env.runs_dir) / run_id
    suffix = 1
    while run_dir.exists():
        suffix += 1
        run_dir = Path(env.runs_dir) / f"{run_id}-{suffix}"
    run_dir.mkdir(parents=True)
    if suffix > 1:
        manifest["run_id"] = run_dir.name
    trades_rows = [row for b in [*books, *control_books] for row in _trade_rows(b)]
    write_table(run_dir / "trades.parquet", TRADE_SCHEMA, trades_rows)
    eq_rows = [
        {"book": b.name, "ts_ms": p.ts_ms, "equity": str(p.equity), "equity_f": float(p.equity)}
        for b in books
        for p in b.equity_curve
    ]
    write_table(run_dir / "equity.parquet", EQUITY_SCHEMA, eq_rows)
    (run_dir / "metrics.json").write_text(metrics_text + "\n", encoding="utf-8")
    manifest["outputs"] = {
        "metrics_sha256": metrics_hash,
        "trades_parquet_sha256": _file_sha(run_dir / "trades.parquet"),
        "equity_parquet_sha256": _file_sha(run_dir / "equity.parquet"),
        "trades_rows": len(trades_rows),
        "equity_rows": len(eq_rows),
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return RunOutcome(manifest["run_id"], run_dir, manifest, metrics, metrics_hash)


def _count(result: SimResult, kind: SimEventType) -> int:
    return sum(1 for e in result.events if e["type"] == str(kind))


# ---------------------------------------------------------------- reading runs back
def list_runs(runs_dir: Path | str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    root = Path(runs_dir)
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir(), reverse=True):
        m = d / "manifest.json"
        if not m.is_file():
            continue
        summary = load_run(root, d.name)
        if summary is not None:
            out.append(summary["summary"])
    return out


def load_run(runs_dir: Path | str, run_id: str) -> dict[str, Any] | None:
    root = Path(runs_dir).resolve()
    d = (root / run_id).resolve()
    if d.parent != root or not (d / "manifest.json").is_file():
        return None  # never leave the runs directory
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    metrics_path = d / "metrics.json"
    metrics = (
        json.loads(metrics_path.read_text(encoding="utf-8")) if metrics_path.is_file() else None
    )
    verified = None
    if metrics_path.is_file():
        text = metrics_path.read_text(encoding="utf-8").rstrip("\n")
        verified = hashlib.sha256(text.encode("utf-8")).hexdigest() == manifest.get(
            "outputs", {}
        ).get("metrics_sha256")
    trades = (metrics or {}).get("trades", {})
    cert = (metrics or {}).get("certification") or {}
    summary = {
        "run_id": manifest.get("run_id"),
        "created_at": manifest.get("created_at"),
        "strategy_id": manifest.get("strategy", {}).get("strategy_id"),
        "strategy_version": manifest.get("strategy", {}).get("version"),
        "params_hash": manifest.get("strategy", {}).get("params_hash"),
        "symbols": manifest.get("symbols"),
        "from": manifest.get("range", {}).get("from"),
        "to": manifest.get("range", {}).get("to"),
        "segment": manifest.get("segment"),
        "evidence_grade": (metrics or {}).get("evidence_grade"),
        "n_trades": trades.get("n"),
        "win_rate": trades.get("win_rate"),
        "win_rate_ci": trades.get("win_rate_ci"),
        "p_star": cert.get("p_star"),
        "margin_lcb": cert.get("margin_lcb"),
        "n_trials": (metrics or {}).get("n_trials"),
        "metrics_verified": verified,
    }
    return {"summary": summary, "manifest": manifest, "metrics": metrics}


def request_from_manifest(manifest: Mapping[str, Any], strategy: Strategy[Any]) -> BacktestRequest:
    """Rebuild the request of a recorded run with its dataset ids pinned (exact rerun)."""
    if strategy.params_hash != manifest["strategy"]["params_hash"]:
        raise ValueError("strategy params differ from the manifest")
    symbols = [(Market(x.split(":")[0]), x.split(":")[1]) for x in manifest["symbols"]]
    return BacktestRequest(
        strategy=strategy,
        symbols=symbols,
        from_ms=int(manifest["range"]["from_ms"]),
        to_ms=int(manifest["range"]["to_ms"]),
        seed=int(manifest["seed"]),
        prereg_id=manifest.get("prereg_id"),
        hypothesis_id=manifest.get("hypothesis_id"),
        dataset_ids={k: list(v) for k, v in manifest["datasets"].items()},
    )
