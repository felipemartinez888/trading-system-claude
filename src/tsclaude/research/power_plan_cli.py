"""``tsclaude backtest power-plan``: statistical-power planning from a P&L-free feasibility count.

Reads one ``feasibility.json`` (no lake, no trial registry, no market data), plans the
walk-forward folds the preregistration would use, and prints the candidates inside the test
windows and the smallest win-rate margin each take-profit geometry could certify with 80 % power
at the trial-corrected level (`research.power_plan`; optimistic, see there). Records nothing.
Exit codes: 0 ok, 2 refused.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _floats(text: str) -> list[float]:
    try:
        out = [float(x) for x in text.split(",") if x.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected comma-separated numbers: {text}") from exc
    if not out:
        raise argparse.ArgumentTypeError("at least one value is required")
    return out


def cmd_power_plan(args: argparse.Namespace) -> int:
    from tsclaude.contracts.timeutil import MS_PER_MINUTE
    from tsclaude.research.backtest_cli import parse_time
    from tsclaude.research.power_plan import plan_from_feasibility, test_windows_of
    from tsclaude.research.walkforward import plan_folds

    try:
        feas = json.loads(Path(args.feasibility).read_text(encoding="utf-8"))
        rng = feas["range"]
        anchor = parse_time(args.anchor) if args.anchor else int(rng["from_ms"])
        end = parse_time(args.end) if args.end else int(rng["to_ms"])
        folds = plan_folds(
            anchor, end, initial_train_months=args.initial_train_months,
            test_months=args.test_months, embargo_ms=args.embargo_minutes * MS_PER_MINUTE,
        )  # fmt: skip
        result: dict[str, Any] = plan_from_feasibility(
            feas, test_windows=test_windows_of(folds), configurations=args.configurations,
            geometries=[(f"{b:g}R", b) for b in args.tp_r], cost_r=args.max_cost_r,
            alpha=args.alpha, prior_family_rows=args.prior_family_rows,
        )  # fmt: skip
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"ok": False, "refused": "power_plan", "error": f"{exc}"}))
        return 2
    result["folds"] = [f.to_dict() for f in folds]
    print(json.dumps({"ok": True, **result}, indent=2))
    return 0


def register_power_plan(bsub: Any) -> None:
    pp = bsub.add_parser(
        "power-plan",
        help="P&L-free power planning from a feasibility.json (optimistic; records nothing)",
    )
    pp.add_argument(
        "--feasibility", required=True, help="a feasibility.json written by feasibility"
    )
    pp.add_argument("--anchor", default=None, help="first train month (default: the count's from)")
    pp.add_argument("--end", default=None, help="walk-forward end (default: the count's to)")
    pp.add_argument("--initial-train-months", type=int, default=12)
    pp.add_argument("--test-months", type=int, default=3)
    pp.add_argument("--embargo-minutes", type=int, default=1440)
    pp.add_argument("--configurations", type=int, default=24)
    pp.add_argument("--tp-r", type=_floats, default=[2.0, 2.5, 3.0], help="e.g. 2,2.5,3")
    pp.add_argument("--max-cost-r", type=float, default=0.2)
    pp.add_argument("--alpha", type=float, default=0.05)
    pp.add_argument(
        "--prior-family-rows", type=int, default=0,
        help="registry rows already recorded for the strategy family (H1 today: 0)",
    )  # fmt: skip
    pp.set_defaults(func=cmd_power_plan)
