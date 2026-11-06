"""Command-line entry point.

Commands: `tsclaude app | data | research | backtest | verify-chain | verify-replay | version`.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Callable, Sequence

from tsclaude import __version__
from tsclaude.runtime.settings import Settings
from tsclaude.store.eventstore import EventStore


def _cmd_version(_: argparse.Namespace) -> int:
    print(__version__)
    return 0


def _cmd_verify_chain(_: argparse.Namespace) -> int:
    settings = Settings.from_env()
    if not settings.state_db_path.exists():
        print(json.dumps({"ok": True, "events_checked": 0, "note": "no state.db yet"}))
        return 0
    with EventStore(settings.state_db_path) as store:
        report = store.verify()
    print(json.dumps(report.__dict__))
    return 0 if report.ok else 2


def _cmd_verify_replay(args: argparse.Namespace) -> int:
    """Replay a recorded paper session and compare it with its session-end evidence (M3 gate)."""
    from pathlib import Path

    from tsclaude.runtime.paper_runtime import PaperSettings, build_paper_config
    from tsclaude.runtime.session_evidence import verify_recorded_session
    from tsclaude.runtime.supervisor import config_dir

    base = Path(args.config_dir) if args.config_dir else config_dir()
    try:
        config = build_paper_config(PaperSettings.load(base / "paper.yaml"), base)
    except Exception as exc:
        print(json.dumps({"verdict": "INCONCLUSIVE", "reasons": [f"configuration: {exc}"]}))
        return 2
    try:
        report = verify_recorded_session(
            Path(args.data_dir), config, evidence=Path(args.evidence) if args.evidence else None
        )
    except Exception as exc:  # an unexpected error is "not proven", never a FAIL traceback
        print(json.dumps({"verdict": "INCONCLUSIVE", "reasons": [f"{type(exc).__name__}: {exc}"]}))
        return 2
    print(report.to_json())
    return report.exit_code


def _cmd_app(args: argparse.Namespace) -> int:
    # Imported lazily: the API pulls heavier dependencies than the other commands need.
    server = importlib.import_module("tsclaude.api.server")
    run: Callable[[Settings], int] = server.run

    settings = Settings.from_env()
    if args.no_browser:
        settings = settings.model_copy(update={"open_browser": False})
    if args.port:
        settings = Settings.model_validate({**settings.model_dump(), "port": args.port})
    return run(settings)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tsclaude", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    app = sub.add_parser("app", help="start the local backend and dashboard")
    app.add_argument("--no-browser", action="store_true", help="do not open the browser")
    app.add_argument("--port", type=int, default=None)
    app.set_defaults(func=_cmd_app)

    # Historical data commands (M2-T1); their module imports nothing heavy at load time.
    importlib.import_module("tsclaude.marketdata.datacli").register(sub)
    # Owner-only research protocol commands (holdout unlock, L-07 a).
    importlib.import_module("tsclaude.store.holdout_cli").register(sub)
    # Backtests with run manifests and certified reports (M2-T4); heavy imports are lazy.
    importlib.import_module("tsclaude.research.backtest_cli").register(sub)

    vr = sub.add_parser(
        "verify-replay",
        help="replay a recorded paper session and compare it with its session-end evidence",
    )
    vr.add_argument("--data-dir", required=True, help="the recorded session's data directory")
    vr.add_argument("--config-dir", default=None, help="configs used by the session")
    vr.add_argument("--evidence", default=None, help="a session_end/*.json (default: the latest)")
    vr.set_defaults(func=_cmd_verify_replay)

    verify = sub.add_parser("verify-chain", help="verify the hash chain of the event store")
    verify.set_defaults(func=_cmd_verify_chain)

    version = sub.add_parser("version", help="print the version")
    version.set_defaults(func=_cmd_version)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
