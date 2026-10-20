"""Command-line entry point: `tsclaude app | data | verify-chain | version`."""

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
