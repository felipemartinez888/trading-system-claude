"""`tsclaude backtest run | show | list | trials | walkforward | feasibility` (M2-T4; MVP; M4).

``run`` reads the lake only through `LakeReader` with the holdout guard backed by the event store
(a range touching the sealed holdout is refused unless ``--prereg`` names a recorded unlock for
this strategy hash). Output is a JSON summary on stdout; artifacts go to ``<data_dir>/runs/``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tsclaude.contracts.market import Market
from tsclaude.contracts.timeutil import SystemClock, from_iso
from tsclaude.runtime.settings import Settings

if TYPE_CHECKING:
    from tsclaude.store.holdout import HoldoutGuard
    from tsclaude.store.lake_reader import LakeReader

# Heavy modules (numpy, simulator) are imported inside the commands so `tsclaude --help` and the
# other commands stay light.

DEFAULT_CONFIG = Path("configs") / "backtest.yaml"


def parse_time(text: str) -> int:
    """``YYYY-MM-DD`` (UTC midnight) or ISO-8601 with a UTC designator -> epoch ms."""
    if len(text) == 10:
        text += "T00:00:00Z"
    return from_iso(text)


def parse_symbols(text: str, default_market: str) -> list[tuple[Market, str]]:
    out = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        market, _, symbol = item.rpartition(":")
        out.append((Market(market or default_market), symbol.upper()))
    if not out:
        raise ValueError("no symbols given")
    return out


def _default_reader(data_dir: Path, guard: HoldoutGuard) -> LakeReader:
    from tsclaude.store.lake_reader import LakeReader

    return LakeReader.open(data_dir, guard)


def _cmd_run(args: argparse.Namespace) -> int:
    from tsclaude.research.max_loss_cache import MaxLossCache
    from tsclaude.research.prereg import PreregistrationRefused
    from tsclaude.research.runner import BacktestConfig, BacktestRequest, RunEnv, run_backtest
    from tsclaude.research.trials import TrialRegistry
    from tsclaude.store.eventstore import EventStore
    from tsclaude.store.holdout import EventStoreUnlocks, HoldoutError, HoldoutGuard
    from tsclaude.strategies import build_strategy

    settings = Settings.from_env()
    params: dict[str, Any] = json.loads(args.params) if args.params else {}
    strategy = build_strategy(args.strategy, params)
    cfg_path = Path(args.config)
    cfg = BacktestConfig.load(cfg_path) if cfg_path.is_file() else BacktestConfig()
    from tsclaude.quality.interruptions import (
        EMPTY_REGISTRY,
        InterruptionRegistryError,
        load_registry,
    )

    registry_path = cfg_path.parent / "exchange_interruptions.yaml"
    try:  # no file: empty registry (every missing minute grades X); a broken file refuses
        registry = load_registry(registry_path) if registry_path.is_file() else EMPTY_REGISTRY
    except InterruptionRegistryError as exc:
        print(json.dumps({"ok": False, "refused": "exchange_interruptions", "error": str(exc)}))
        return 5
    req = BacktestRequest(
        strategy=strategy,
        symbols=parse_symbols(args.symbols, args.market),
        from_ms=parse_time(args.from_),
        to_ms=parse_time(args.to),
        seed=args.seed,
        prereg_id=args.prereg,
        hypothesis_id=args.hypothesis,
    )
    clock = SystemClock()
    with EventStore(settings.state_db_path) as store:
        guard = HoldoutGuard(EventStoreUnlocks(store, Path(args.repo_root)))
        env = RunEnv(
            reader=_default_reader(settings.data_dir, guard),  # module lookup: test seam
            trials=TrialRegistry(settings.state_db_path),
            runs_dir=settings.data_dir / "runs",
            now_ms=clock.now_ms,
            repo_root=Path(args.repo_root),
            exchange_interruptions=registry,
            max_loss_cache=MaxLossCache(settings.data_dir / "cache" / "max_loss"),
        )
        try:
            out = run_backtest(req, env, cfg)
        except HoldoutError as exc:
            print(json.dumps({"ok": False, "refused": "holdout", "error": str(exc)}))
            return 3
        except PreregistrationRefused as exc:
            print(json.dumps({"ok": False, "refused": "preregistration", "error": str(exc)}))
            return 4
    print(
        json.dumps(
            {
                "ok": True,
                "run_id": out.run_id,
                "run_dir": str(out.run_dir),
                "evidence_grade": out.metrics["evidence_grade"],
                "grade_reasons": out.metrics["grade_reasons"],
                "n_trades": out.metrics["trades"]["n"],
                "n_trials": out.metrics["n_trials"],
                "metrics_sha256": out.metrics_hash,
                "provenance": "BACKTEST",
            },
            indent=2,
        )
    )
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    from tsclaude.research.runner import load_run

    settings = Settings.from_env()
    run = load_run(settings.data_dir / "runs", args.run_id)
    if run is None:
        print(json.dumps({"ok": False, "error": f"unknown run {args.run_id}"}))
        return 2
    print(json.dumps(run if args.full else run["summary"], indent=2))  # current n_trials (N1)
    return 0


def _cmd_list(_: argparse.Namespace) -> int:
    from tsclaude.research.runner import list_runs

    settings = Settings.from_env()
    print(json.dumps(list_runs(settings.data_dir / "runs"), indent=2))
    return 0


def _cmd_trials(args: argparse.Namespace) -> int:
    from tsclaude.research.trials import TrialRegistry

    settings = Settings.from_env()
    registry = TrialRegistry(settings.state_db_path)
    print(json.dumps(registry.list(args.strategy), indent=2))
    return 0


def load_grid(path: Path) -> dict[str, dict[str, Any]]:
    """A parameter grid: a JSON list of parameter objects (ids are content hashes) or a JSON
    object ``{config_id: params}``."""
    from tsclaude.research.walkforward import config_id_of

    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        grid = {config_id_of(p): dict(p) for p in raw}
        if len(grid) != len(raw):
            raise ValueError("the grid lists the same parameters twice")
        return grid
    if isinstance(raw, dict) and all(isinstance(v, dict) for v in raw.values()):
        return {str(k): dict(v) for k, v in raw.items()}
    raise ValueError("the grid must be a list of parameter objects or {id: parameters}")


def load_dataset_pins(path: Path) -> list[str]:
    """Dataset pins: a JSON list of dataset ids or ``{"dataset_ids": [...]}`` (non-empty)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    ids = raw.get("dataset_ids") if isinstance(raw, dict) else raw
    if not isinstance(ids, list) or not ids or not all(isinstance(x, str) and x for x in ids):
        raise ValueError('the dataset pin file must be a non-empty list or {"dataset_ids": [...]}')
    if len(set(ids)) != len(ids):
        raise ValueError("the dataset pin file lists an id twice")
    return sorted(ids)


def _cmd_walkforward(args: argparse.Namespace) -> int:
    """Anchored walk-forward on the development segment only (RESEARCH_PROTOCOL §3).

    Without ``--from`` the anchor is the earliest UTC month start (not before the development
    segment) whose warm-up lies inside the stored data; an explicit earlier anchor is refused
    (audit WF-2). The report and the progress ledger go to ``<data_dir>/walkforward/<plan id>/``;
    running the same command again after an interruption resumes it (verified reuse, WF-4).
    Exit codes: 2 refused, 3 holdout, 4 preregistration, 5 interruption registry, 6 resume
    refused."""
    from tsclaude.contracts.timeutil import MS_PER_MINUTE
    from tsclaude.research.max_loss_cache import MaxLossCache
    from tsclaude.research.prereg import PreregistrationRefused
    from tsclaude.research.runner import BacktestConfig, RunEnv, segment_of
    from tsclaude.research.trials import TrialRegistry
    from tsclaude.research.walkforward import (
        WalkForwardError,
        check_anchor,
        data_start_ms,
        default_anchor_ms,
        grid_warmup_ms,
        plan_folds,
        run_walk_forward,
    )
    from tsclaude.research.wf_progress import ResumeRefused
    from tsclaude.store.eventstore import EventStore
    from tsclaude.store.holdout import EventStoreUnlocks, HoldoutError, HoldoutGuard

    def refused(kind: str, exc: Exception, code: int) -> int:
        print(json.dumps({"ok": False, "refused": kind, "error": str(exc)}))
        return code

    settings = Settings.from_env()
    try:
        grid = load_grid(Path(args.grid))
        if len(grid) > args.max_configs:
            raise WalkForwardError(
                f"{len(grid)} configurations exceed the trial budget --max-configs "
                f"{args.max_configs}; shrink the grid (every evaluation is a recorded trial)"
            )
        to_ms = parse_time(args.to)
        explicit_from = None if args.from_ is None else parse_time(args.from_)
        if explicit_from is not None and segment_of(explicit_from, to_ms) != "development":
            raise WalkForwardError(
                "walk-forward selection runs on the development segment only "
                "(2022-01-01 to 2025-01-01); validation and holdout are evaluated once, later"
            )
        symbols = parse_symbols(args.symbols, args.market)
        warmup = grid_warmup_ms(args.strategy, grid)
        pins = None if args.datasets is None else load_dataset_pins(Path(args.datasets))
        if not 0.0 < args.min_oos_coverage <= 1.0:
            raise WalkForwardError("--min-oos-coverage must be in (0, 1]")
    except (ValueError, OSError) as exc:
        return refused("walkforward", exc, 2)
    cfg_path = Path(args.config)
    cfg = BacktestConfig.load(cfg_path) if cfg_path.is_file() else BacktestConfig()
    from tsclaude.quality.interruptions import (
        EMPTY_REGISTRY,
        InterruptionRegistryError,
        load_registry,
    )

    registry_path = cfg_path.parent / "exchange_interruptions.yaml"
    try:  # as `backtest run`: no file -> empty registry; a broken file refuses
        interruptions = load_registry(registry_path) if registry_path.is_file() else EMPTY_REGISTRY
    except InterruptionRegistryError as exc:
        return refused("exchange_interruptions", exc, 5)
    clock = SystemClock()
    with EventStore(settings.state_db_path) as store:
        guard = HoldoutGuard(EventStoreUnlocks(store, Path(args.repo_root)))
        env = RunEnv(
            reader=_default_reader(settings.data_dir, guard),
            trials=TrialRegistry(settings.state_db_path),
            runs_dir=settings.data_dir / "runs",
            now_ms=clock.now_ms,
            repo_root=Path(args.repo_root),
            exchange_interruptions=interruptions,
            max_loss_cache=MaxLossCache(settings.data_dir / "cache" / "max_loss"),
        )
        try:  # the anchor and the folds need the data's coverage; nothing is recorded yet
            start = data_start_ms(env.reader, symbols, pins)
            if start is None:
                raise WalkForwardError("no stored 1m bars for the requested symbols")
            from_ms = explicit_from
            if from_ms is None:
                from_ms = default_anchor_ms(start, warmup)
            check_anchor(from_ms, data_start_ms=start, warmup_ms=warmup)
            if segment_of(from_ms, to_ms) != "development":
                raise WalkForwardError(
                    "walk-forward selection runs on the development segment only "
                    "(2022-01-01 to 2025-01-01); validation and holdout are evaluated once, later"
                )
            folds = plan_folds(
                from_ms,
                to_ms,
                initial_train_months=args.initial_train_months,
                test_months=args.test_months,
                embargo_ms=args.embargo_minutes * MS_PER_MINUTE,
            )
            from tsclaude.risk.config import RiskConfigError, load_risk_config

            risk_path = cfg_path.parent / "risk.yaml"
            try:  # informational only (live compatibility); a missing file: not assessed
                risk_config = load_risk_config(risk_path) if risk_path.is_file() else None
            except RiskConfigError as exc:
                raise WalkForwardError(f"configs/risk.yaml is invalid: {exc}") from exc
            report = run_walk_forward(
                env,
                cfg,
                strategy_id=args.strategy,
                grid=grid,
                symbols=symbols,
                folds=folds,
                n_min=args.n_min,
                hypothesis_id=args.hypothesis,
                work_root=settings.data_dir / "walkforward",
                prereg_id=args.prereg,
                dataset_ids=pins,
                resume=not args.no_resume,
                max_new_trials=args.max_new_trials,
                final_selection_enabled=args.final_selection,
                min_oos_coverage=args.min_oos_coverage,
                risk_config=risk_config,
            )
        except HoldoutError as exc:
            return refused("holdout", exc, 3)
        except PreregistrationRefused as exc:
            return refused("preregistration", exc, 4)
        except ResumeRefused as exc:
            return refused("resume", exc, 6)
        except ValueError as exc:
            return refused("walkforward", exc, 2)
    print(
        json.dumps(
            {
                "ok": True,
                "report": report.get("report_path"),
                "plan_id": report["plan"]["plan_id"],
                "resumable": report["plan"]["resumable"],
                "resume": report["resume"],
                "strategy_id": report["strategy_id"],
                "folds": len(report["folds"]),
                "n_train_evaluations": report["n_train_evaluations"],
                "n_test_evaluations": report["n_test_evaluations"],
                "trial_budget": report["trial_budget"],
                "warmup": report["warmup"],
                "checks": report["checks"],
                "oos": report["oos"],
                "final_selection": report["final_selection"],
                "live_compatibility": report["live_compatibility"],
                "provenance": "BACKTEST",
            },
            indent=2,
            default=str,
        )
    )
    return 0


def register(sub: Any) -> None:
    bt = sub.add_parser("backtest", help="reproducible backtests with certified reports")
    bsub = bt.add_subparsers(dest="backtest_command", required=True)
    run = bsub.add_parser("run", help="run a backtest and write runs/<run_id>/")
    run.add_argument("--strategy", required=True)
    run.add_argument("--params", default=None, help="JSON object of strategy params")
    run.add_argument("--symbols", required=True, help="e.g. spot:BTCUSDT,spot:ETHUSDT")
    run.add_argument("--market", default="spot", help="market for symbols without a prefix")
    run.add_argument("--from", dest="from_", required=True, help="UTC date or ISO-8601 Z")
    run.add_argument("--to", required=True, help="UTC date or ISO-8601 Z (exclusive)")
    run.add_argument(
        "--seed",
        type=int,
        default=None,
        help="explicit seed (recorded as a separate trial); default: derived from the trial key",
    )
    run.add_argument("--prereg", default=None, help="preregistration id (holdout runs)")
    run.add_argument(
        "--hypothesis",
        default=None,
        help="label stored with the trial (n_trials counts every configuration of the strategy)",
    )
    run.add_argument("--config", default=str(DEFAULT_CONFIG))
    run.add_argument("--repo-root", default=".")
    run.set_defaults(func=_cmd_run)
    show = bsub.add_parser("show", help="print a recorded run")
    show.add_argument("run_id")
    show.add_argument("--full", action="store_true", help="manifest and metrics, not a summary")
    show.set_defaults(func=_cmd_show)
    ls = bsub.add_parser("list", help="list recorded runs")
    ls.set_defaults(func=_cmd_list)
    tr = bsub.add_parser("trials", help="list the trial registry")
    tr.add_argument("--strategy", default=None, help="strategy id (family)")
    tr.set_defaults(func=_cmd_trials)
    wf = bsub.add_parser(
        "walkforward",
        help="anchored walk-forward on the development segment (selection on train only)",
    )
    wf.add_argument("--strategy", required=True)
    wf.add_argument("--grid", required=True, help="JSON file: [params, ...] or {id: params}")
    wf.add_argument("--symbols", required=True, help="e.g. spot:BTCUSDT,spot:ETHUSDT")
    wf.add_argument("--market", default="spot", help="market for symbols without a prefix")
    wf.add_argument(
        "--from",
        dest="from_",
        default=None,
        help="anchor, a UTC month start; default: the earliest one whose warm-up is in the data",
    )
    wf.add_argument("--to", default="2025-01-01", help="UTC month start (exclusive)")
    wf.add_argument("--initial-train-months", type=int, default=12)
    wf.add_argument("--test-months", type=int, default=3)
    wf.add_argument(
        "--embargo-minutes",
        type=int,
        default=1440,
        help="gap between train end and test start: at least one maximum holding period",
    )
    wf.add_argument("--n-min", type=int, default=30, help="trades a configuration needs")
    wf.add_argument(
        "--max-configs", type=int, default=12, help="budget: distinct configurations in the grid"
    )
    wf.add_argument(
        "--max-new-trials",
        type=int,
        default=None,
        help="budget: refuse if the walk-forward could add more registry trials than this",
    )
    wf.add_argument("--hypothesis", required=True, help="label stored with every trial")
    wf.add_argument("--prereg", default=None, help="committed preregistration id for every run")
    wf.add_argument(
        "--datasets",
        default=None,
        help='JSON file: the dataset ids every run may read, [...] or {"dataset_ids": [...]}',
    )
    wf.add_argument(
        "--no-resume",
        action="store_true",
        help="ignore recorded progress and run every evaluation again (a new report directory)",
    )
    wf.add_argument(
        "--final-selection",
        action="store_true",
        help="after a positive verdict, select one configuration on the whole development "
        "window (rule pending governance; adds up to one trial per configuration)",
    )
    wf.add_argument(
        "--min-oos-coverage",
        type=float,
        default=1.0,
        help="share of the planned test period that must be tested out of sample (default all)",
    )
    wf.add_argument("--config", default=str(DEFAULT_CONFIG))
    wf.add_argument("--repo-root", default=".")
    wf.set_defaults(func=_cmd_walkforward)
    from tsclaude.research.feasibility_cli import register_feasibility

    register_feasibility(bsub)
