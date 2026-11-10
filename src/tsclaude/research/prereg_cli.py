"""`tsclaude prereg validate | freeze | select` (M4-T1 deliverable 1).

``validate`` checks a preregistration against schema ``tsc-prereg-v1`` and lists the owner
decisions still open; ``freeze`` writes the frozen block into the file (refused while any problem or
open owner decision remains; committing the file is the owner's act); ``select`` writes a selection
record for a walk-forward report's final-selection candidate (its holdout procedure stays PENDING,
decision D-D). Exit codes: 0 ok, 2 invalid or refused.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _cmd_validate(args: argparse.Namespace) -> int:
    from tsclaude.research.preregistration import validate_file

    v = validate_file(Path(args.file))
    _print(v.to_dict())
    return 0 if v.ok else 2


def _cmd_freeze(args: argparse.Namespace) -> int:
    from tsclaude.contracts.timeutil import SystemClock
    from tsclaude.research.preregistration import PreregistrationInvalid, freeze
    from tsclaude.research.runner import git_version

    try:
        block = freeze(Path(args.file), now_ms=SystemClock().now_ms(), code=git_version())
    except PreregistrationInvalid as exc:
        _print({"ok": False, "refused": "freeze", "problems": exc.problems})
        return 2
    _print({"ok": True, "frozen": block, "next": "review the diff, then commit the file"})
    return 0


def _cmd_select(args: argparse.Namespace) -> int:
    import yaml

    from tsclaude.research.preregistration import PreregistrationInvalid, make_selection_record

    out = Path(args.out) if args.out else Path(args.search).parent / f"{args.id}.yaml"
    if out.exists():
        _print({"ok": False, "refused": "select", "problems": [f"{out} already exists"]})
        return 2
    try:
        record = make_selection_record(Path(args.search), Path(args.report), prereg_id=args.id)
    except (PreregistrationInvalid, OSError, ValueError) as exc:
        problems = exc.problems if isinstance(exc, PreregistrationInvalid) else [str(exc)]
        _print({"ok": False, "refused": "select", "problems": problems})
        return 2
    out.write_text(yaml.safe_dump(record, sort_keys=False), encoding="utf-8")
    _print({"ok": True, "path": str(out), "holdout_procedure": record["holdout_procedure"]})
    return 0


def register(sub: Any) -> None:
    p = sub.add_parser("prereg", help="preregistration schema, freeze and selection records")
    psub = p.add_subparsers(dest="prereg_command", required=True)
    v = psub.add_parser("validate", help="check a preregistration and list open owner decisions")
    v.add_argument("file")
    v.set_defaults(func=_cmd_validate)
    f = psub.add_parser("freeze", help="freeze a search preregistration (then commit it)")
    f.add_argument("file")
    f.set_defaults(func=_cmd_freeze)
    s = psub.add_parser("select", help="selection record for a walk-forward's candidate")
    s.add_argument("--search", required=True, help="the frozen search preregistration")
    s.add_argument("--report", required=True, help="the walk-forward's walkforward.json")
    s.add_argument("--id", required=True, help="prereg_id of the selection record")
    s.add_argument("--out", default=None)
    s.set_defaults(func=_cmd_select)


__all__ = ["register"]
