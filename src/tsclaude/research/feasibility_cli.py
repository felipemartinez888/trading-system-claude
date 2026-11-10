"""`tsclaude backtest feasibility`: P&L-free stage counts on development data (audit H1-1).

Reads the lake only through `LakeReader` behind the holdout guard (event-store unlocks), like
``backtest run``. It evaluates no outcome and records no trial. Each configuration (``--params``,
or one per entry of ``--grid``) writes ``<data_dir>/feasibility/feas-<hash12>/feasibility.json``;
a JSON summary goes to stdout. Refusals exit 2 (``{"ok": false, "refused": "feasibility"}``); a
range touching the holdout exits 3. The Lead wires `register_feasibility` into ``backtest``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tsclaude.runtime.settings import Settings

if TYPE_CHECKING:
    from tsclaude.store.holdout import HoldoutGuard
    from tsclaude.store.lake_reader import LakeReader

DEFAULT_CONFIG = Path("configs") / "backtest.yaml"


def _default_reader(data_dir: Path, guard: HoldoutGuard) -> LakeReader:
    from tsclaude.store.lake_reader import LakeReader

    return LakeReader.open(data_dir, guard)


def lake_dataset_ids(data_dir: Path) -> set[str]:
    """Dataset ids committed to the lake (import manifests only, as `Lake.dataset_ids`)."""
    imports = data_dir / "imports"
    if not imports.is_dir():
        return set()
    return {p.parent.name for p in imports.glob("*/manifest.json") if len(p.parent.name) == 64}


def load_dataset_ids(path: Path, known: set[str]) -> list[str]:
    """A JSON list of dataset ids or ``{"dataset_ids": [...]}``; every id must be in the lake."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        if set(raw) != {"dataset_ids"}:
            raise ValueError('the datasets file must be a list or {"dataset_ids": [...]}')
        raw = raw["dataset_ids"]
    if not isinstance(raw, list) or not raw or not all(isinstance(x, str) for x in raw):
        raise ValueError("dataset_ids must be a non-empty list of strings")
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValueError(f"unknown dataset ids (not in the lake): {unknown}")
    return sorted(set(raw))


def _refuse(error: str, kind: str = "feasibility", code: int = 2) -> int:
    print(json.dumps({"ok": False, "refused": kind, "error": error}))
    return code


def cmd_feasibility(args: argparse.Namespace) -> int:
    from tsclaude.research.backtest_cli import load_grid, parse_symbols, parse_time
    from tsclaude.research.feasibility import (
        FeasibilityHoldoutRefused,
        FeasibilityRefused,
        check_request,
        count_feasibility,
        planning_costs_for,
        stage_names,
    )
    from tsclaude.research.runner import BacktestConfig
    from tsclaude.sim.costs import CostModel
    from tsclaude.store.eventstore import EventStore
    from tsclaude.store.holdout import EventStoreUnlocks, HoldoutError, HoldoutGuard
    from tsclaude.strategies import Strategy, build_strategy

    settings = Settings.from_env()
    try:
        if (args.params is None) == (args.grid is None):
            raise ValueError("give exactly one of --params or --grid")
        grid: dict[str, dict[str, Any]] = (
            load_grid(Path(args.grid))
            if args.grid is not None
            else {"params": dict(json.loads(args.params))}
        )
        strategies: dict[str, Strategy[Any]] = {
            cid: build_strategy(args.strategy, p) for cid, p in grid.items()
        }
        symbols = parse_symbols(args.symbols, args.market)
        from_ms, to_ms = parse_time(args.from_), parse_time(args.to)
        cfg_path = Path(args.config)
        cfg = BacktestConfig.load(cfg_path) if cfg_path.is_file() else BacktestConfig()
        pinned = (
            None
            if args.datasets is None
            else load_dataset_ids(Path(args.datasets), lake_dataset_ids(settings.data_dir))
        )
        for strategy in strategies.values():  # refuse every configuration before any read
            check_request(strategy, from_ms, to_ms)
            stage_names(strategy)
    except FeasibilityHoldoutRefused as exc:
        return _refuse(str(exc), "holdout", 3)
    except (ValueError, TypeError, OSError) as exc:  # TypeError: unknown parameter names
        return _refuse(str(exc))

    # the cost model `backtest run` uses (the `RunEnv.cost_model` default), so the cost filter
    # is evaluated with the same planning costs as in a backtest
    costs_for = planning_costs_for(CostModel())
    out_root = settings.data_dir / "feasibility"
    summaries: list[dict[str, Any]] = []
    with EventStore(settings.state_db_path) as store:
        guard = HoldoutGuard(EventStoreUnlocks(store, Path(args.repo_root)))
        reader = _default_reader(settings.data_dir, guard)  # module lookup: test seam
        for cid, strategy in strategies.items():
            try:
                result = count_feasibility(
                    reader,
                    strategy,
                    symbols,
                    from_ms,
                    to_ms,
                    costs_for=costs_for,
                    dataset_ids=pinned,
                )
            except (HoldoutError, FeasibilityHoldoutRefused) as exc:
                return _refuse(str(exc), "holdout", 3)
            except (FeasibilityRefused, ValueError) as exc:
                return _refuse(str(exc))
            result["config_id"] = cid
            result["backtest_config_hash"] = cfg.config_hash
            out_dir = out_root / f"feas-{result['content_hash'][:12]}"
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / "feasibility.json"
            path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            summaries.append(
                {
                    "config_id": cid,
                    "params_hash": strategy.params_hash,
                    "path": str(path),
                    "content_hash": result["content_hash"],
                    "stages": result["stages"],
                    "totals": {k: v["total"] for k, v in result["symbols"].items()},
                }
            )
    print(
        json.dumps(
            {
                "ok": True,
                "kind": "feasibility_count",
                "pnl_free": True,
                "trials_recorded": 0,
                "strategy_id": args.strategy,
                "results": summaries,
                "provenance": "BACKTEST",
            },
            indent=2,
        )
    )
    return 0


def register_feasibility(bsub: Any) -> None:
    """Add ``feasibility`` to the ``backtest`` sub-parsers (called by `backtest_cli.register`)."""
    fe = bsub.add_parser(
        "feasibility",
        help="P&L-free count of decision bars passing each entry stage (development data only)",
    )
    fe.add_argument("--strategy", required=True)
    fe.add_argument("--params", default=None, help="JSON object of strategy params")
    fe.add_argument("--grid", default=None, help="JSON file: [params, ...] or {id: params}")
    fe.add_argument("--symbols", required=True, help="e.g. spot:BTCUSDT,spot:ETHUSDT")
    fe.add_argument("--market", default="spot", help="market for symbols without a prefix")
    fe.add_argument("--from", dest="from_", required=True, help="UTC date or ISO-8601 Z")
    fe.add_argument("--to", required=True, help="UTC date or ISO-8601 Z (exclusive)")
    fe.add_argument("--config", default=str(DEFAULT_CONFIG))
    fe.add_argument("--repo-root", default=".")
    fe.add_argument(
        "--datasets",
        default=None,
        help='JSON file: [dataset_id, ...] or {"dataset_ids": [...]}; pins the read',
    )
    fe.set_defaults(func=cmd_feasibility)
