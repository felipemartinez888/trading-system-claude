"""``tsclaude evaluation export | verify | score`` (M6). Registered by the Lead in ``cli.py``
through `register_evaluation(sub)`.

- ``export --out DIR``: write the tsc-eval v1 export of the event store (default
  ``$TSC_DATA_DIR/state.db``; ``--db`` to choose); ``--upto-seq`` exports a prefix of the chain;
  the wall-clock export time goes into the manifest only.
- ``verify --export DIR``: structural checks and, unless ``--no-store``, the store-side checks
  (chain head present, byte-identical re-export).
- ``score --export DIR``: the stdlib scorer (refuses an export that fails its checks).

Exit codes: 0 ok, 2 failed check or refused export. Read-only on the store: nothing is appended.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from tsclaude.contracts.timeutil import SystemClock


def _db_path(args: argparse.Namespace) -> Path:
    if args.db:
        return Path(args.db)
    from tsclaude.runtime.settings import Settings

    return Settings.from_env().state_db_path


def _open_store(path: Path) -> Any:
    from tsclaude.store.eventstore import EventStore

    if not path.is_file():
        raise FileNotFoundError(f"no event store at {path}")
    return EventStore(path)


def _cmd_export(args: argparse.Namespace) -> int:
    from tsclaude.evaluation.export import ExportError, export_store

    try:
        with _open_store(_db_path(args)) as store:
            result = export_store(
                store,
                Path(args.out),
                upto_seq=args.upto_seq,
                exported_at_ms=SystemClock().now_ms(),
                git_commit=args.git_commit,
                protocol_sha256=args.protocol_sha256,
            )
    except (ExportError, FileNotFoundError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "directory": str(result.directory),
                "chain_head": result.chain_head,
                "lines": result.lines,
                "not_exported": result.manifest["not_exported"],
            },
            sort_keys=True,
        )
    )
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    from tsclaude.evaluation.verify import report_json, verify_export

    if args.no_store:
        report = verify_export(Path(args.export))
    else:
        try:
            with _open_store(_db_path(args)) as store:
                report = verify_export(Path(args.export), store=store)
        except FileNotFoundError as exc:
            print(json.dumps({"ok": False, "problems": [str(exc)]}))
            return 2
    print(report_json(report))
    return 0 if report.ok else 2


def _cmd_score(args: argparse.Namespace) -> int:
    from tsclaude.evaluation.scorer import main

    argv = [args.export] + (["--schemas", args.schemas] if args.schemas else [])
    return main(argv)


def register_evaluation(sub: Any) -> None:
    ev = sub.add_parser("evaluation", help="evaluation export, verification and scoring (M6)")
    esub = ev.add_subparsers(dest="evaluation_command", required=True)

    ex = esub.add_parser("export", help="write the tsc-eval v1 JSONL export of the event store")
    ex.add_argument("--out", required=True, help="output directory (absent or empty)")
    ex.add_argument("--db", default=None, help="event store (default: $TSC_DATA_DIR/state.db)")
    ex.add_argument("--upto-seq", type=int, default=None, help="export the chain up to this seq")
    ex.add_argument("--git-commit", default=None, help="commit recorded in the manifest")
    ex.add_argument("--protocol-sha256", default=None, help="hash of the frozen protocol")
    ex.set_defaults(func=_cmd_export)

    ve = esub.add_parser("verify", help="verify an export (schemas, hashes, store)")
    ve.add_argument("--export", required=True)
    ve.add_argument("--db", default=None, help="event store (default: $TSC_DATA_DIR/state.db)")
    ve.add_argument("--no-store", action="store_true", help="structural checks only")
    ve.set_defaults(func=_cmd_verify)

    sc = esub.add_parser("score", help="recompute dashboard metrics from an export alone")
    sc.add_argument("--export", required=True)
    sc.add_argument("--schemas", default=None, help="schema directory (default: bundled)")
    sc.set_defaults(func=_cmd_score)
