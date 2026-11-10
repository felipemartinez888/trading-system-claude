"""`tsclaude backtest run | show | list | trials | walkforward` (M2-T4; walk-forward: MVP).

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


def _cmd_walkforward(args: argparse.Namespace) -> int:
    """Anchored walk-forward on the development segment only (RESEARCH_PROTOCOL §3).

    Without ``--from`` the anchor is the earliest UTC month start (not before the development
    segment) whose warm-up lies inside the stored data; an explicit earlier anchor is refused
    (audit WF-2)."""
    from tsclaude.contracts.timeutil import MS_PER_MINUTE
    from tsclaude.research.runner import DEV_START_MS, BacktestConfig, RunEnv, segment_of
    from tsclaude.research.trials import TrialRegistry
    from tsclaude.research.walkforward import (
        WalkForwardError,
        check_anchor,
        data_start_ms,
        earliest_anchor_ms,
        grid_warmup_ms,
        plan_folds,
        run_walk_forward,
    )
    from tsclaude.store.eventstore import EventStore
    from tsclaude.store.holdout import EventStoreUnlocks, HoldoutError, HoldoutGuard

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
    except (ValueError, TypeError, OSError) as exc:  # TypeError: unknown parameter names
        print(json.dumps({"ok": False, "refused": "walkforward", "error": str(exc)}))
        return 2
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
        print(json.dumps({"ok": False, "refused": "exchange_interruptions", "error": str(exc)}))
        return 5
    clock = SystemClock()
    stamp = clock.now_ms()
    with EventStore(settings.state_db_path) as store:
        guard = HoldoutGuard(EventStoreUnlocks(store, Path(args.repo_root)))
        env = RunEnv(
            reader=_default_reader(settings.data_dir, guard),
            trials=TrialRegistry(settings.state_db_path),
            runs_dir=settings.data_dir / "runs",
            now_ms=clock.now_ms,
            repo_root=Path(args.repo_root),
            exchange_interruptions=interruptions,
        )
        try:  # the anchor and the folds need the data's coverage; nothing is recorded yet
            start = data_start_ms(env.reader, symbols)
            if start is None:
                raise WalkForwardError("no stored 1m bars for the requested symbols")
            from_ms = (
                explicit_from
                if explicit_from is not None
                else max(DEV_START_MS, earliest_anchor_ms(start, warmup))
            )
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
        except ValueError as exc:
            print(json.dumps({"ok": False, "refused": "walkforward", "error": str(exc)}))
            return 2
        try:
            report = run_walk_forward(
                env,
                cfg,
                strategy_id=args.strategy,
                grid=grid,
                symbols=symbols,
                folds=folds,
                n_min=args.n_min,
                hypothesis_id=args.hypothesis,
                report_dir=settings.data_dir / "walkforward" / f"wf-{stamp}",
            )
        except HoldoutError as exc:
            print(json.dumps({"ok": False, "refused": "holdout", "error": str(exc)}))
            return 3
    print(
        json.dumps(
            {
                "ok": True,
                "report": str(
                    settings.data_dir / "walkforward" / f"wf-{stamp}" / "walkforward.json"
                ),
                "strategy_id": report["strategy_id"],
                "folds": len(report["folds"]),
                "n_train_evaluations": report["n_train_evaluations"],
                "n_test_evaluations": report["n_test_evaluations"],
                "warmup": report["warmup"],
                "oos": report["oos"],
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
    wf.add_argument("--max-configs", type=int, default=12, help="trial budget for the grid")
    wf.add_argument("--hypothesis", required=True, help="label stored with every trial")
    wf.add_argument("--config", default=str(DEFAULT_CONFIG))
    wf.add_argument("--repo-root", default=".")
    wf.set_defaults(func=_cmd_walkforward)
