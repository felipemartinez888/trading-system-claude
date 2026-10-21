"""`tsclaude backtest run | show | list | trials` (M2-T4).

``run`` reads the lake only through `LakeReader` with the holdout guard backed by the event store
(a range touching the sealed holdout is refused unless ``--prereg`` names a recorded unlock for
this strategy hash). Output is a JSON summary on stdout; artifacts go to ``<data_dir>/runs/``.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
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


#: Seam for tests (the default reader uses the Parquet codec inside store/).
ReaderFactory = Callable[[Path, "HoldoutGuard"], "LakeReader"]


def _default_reader(data_dir: Path, guard: HoldoutGuard) -> LakeReader:
    from tsclaude.store.lake_reader import LakeReader

    return LakeReader.open(data_dir, guard)


def _cmd_run(args: argparse.Namespace, reader_factory: ReaderFactory = _default_reader) -> int:
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
            reader=reader_factory(settings.data_dir, guard),
            trials=TrialRegistry(settings.state_db_path),
            runs_dir=settings.data_dir / "runs",
            now_ms=clock.now_ms,
        )
        try:
            out = run_backtest(req, env, cfg)
        except HoldoutError as exc:
            print(json.dumps({"ok": False, "refused": "holdout", "error": str(exc)}))
            return 3
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
    print(json.dumps(run if args.full else run["summary"], indent=2))
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
    print(json.dumps(registry.list(args.hypothesis), indent=2))
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
    run.add_argument("--seed", type=int, default=0)
    run.add_argument("--prereg", default=None, help="preregistration id (holdout runs)")
    run.add_argument("--hypothesis", default=None, help="trial-registry hypothesis id")
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
    tr.add_argument("--hypothesis", default=None)
    tr.set_defaults(func=_cmd_trials)
