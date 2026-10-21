"""`tsclaude research unlock-holdout` — the only way to unlock the sealed holdout (L-07 a).

    tsclaude research unlock-holdout --prereg <id> --strategy-hash <hash> --reason "<text>"

Run by the owner in an interactive terminal from the repository root. It verifies the committed
preregistration (`store.holdout.inspect_preregistration`), shows what will be recorded, and
requires typing `UNLOCK HOLDOUT <id>` exactly. Then it records the single `holdout_unlocked`
event for that preregistration in `state.db`. Any mismatch fails closed and records nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, TextIO

from tsclaude.contracts.timeutil import SystemClock, WallClock, to_iso
from tsclaude.store.eventstore import EventStore
from tsclaude.store.holdout import (
    HOLDOUT_END_MS,
    HOLDOUT_START_MS,
    EventStoreUnlocks,
    HoldoutError,
    UnlockAuthorization,
    UnlockRecord,
    confirmation_phrase,
    inspect_preregistration,
)

ISSUER = object()  # identity checked by UnlockAuthorization


def authorize(
    repo_root: Path,
    prereg_id: str,
    strategy_hash: str,
    reason: str,
    *,
    stdin: TextIO,
    stdout: TextIO,
) -> UnlockAuthorization:
    """Verify the preregistration and obtain the owner's typed confirmation."""
    if not stdin.isatty():
        raise HoldoutError("unlock-holdout must be run by the owner in an interactive terminal")
    evidence = inspect_preregistration(repo_root, prereg_id)
    if evidence.strategy_hash != strategy_hash:
        raise HoldoutError(
            f"{evidence.path} declares strategy_hash {evidence.strategy_hash}, "
            f"not {strategy_hash}; nothing unlocked"
        )
    if not reason.strip():
        raise HoldoutError("--reason is required")
    phrase = confirmation_phrase(prereg_id)
    print(
        "You are about to unlock the SEALED HOLDOUT. This can happen once per preregistration",
        file=stdout,
    )
    print("and is recorded permanently in the event log.", file=stdout)
    print(f"  holdout        : {to_iso(HOLDOUT_START_MS)} .. {to_iso(HOLDOUT_END_MS)}", file=stdout)
    print(f"  preregistration: {evidence.path} (sha256 {evidence.sha256})", file=stdout)
    print(f"  git HEAD       : {evidence.git_head}", file=stdout)
    print(f"  strategy hash  : {evidence.strategy_hash}", file=stdout)
    print(f"  reason         : {reason.strip()}", file=stdout)
    print(f"Type exactly: {phrase}", file=stdout)
    stdout.write("> ")
    stdout.flush()
    typed = stdin.readline().strip()
    if typed != phrase:
        raise HoldoutError("confirmation phrase not typed; nothing unlocked")
    return UnlockAuthorization(issuer=ISSUER, evidence=evidence, reason=reason)


def unlock(
    store: EventStore,
    repo_root: Path,
    prereg_id: str,
    strategy_hash: str,
    reason: str,
    *,
    stdin: TextIO,
    stdout: TextIO,
    clock: WallClock | None = None,
) -> UnlockRecord:
    auth = authorize(repo_root, prereg_id, strategy_hash, reason, stdin=stdin, stdout=stdout)
    return EventStoreUnlocks(store, repo_root).unlock(
        auth, now_ms=(clock or SystemClock()).now_ms()
    )


def _cmd_unlock(args: argparse.Namespace) -> int:
    from tsclaude.runtime.settings import Settings

    settings = Settings.from_env()
    try:
        with EventStore(settings.state_db_path) as store:
            record = unlock(
                store,
                Path(args.repo_root),
                args.prereg,
                args.strategy_hash,
                args.reason,
                stdin=sys.stdin,
                stdout=sys.stdout,
            )
    except HoldoutError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    print(
        json.dumps(
            {
                "unlocked": record.prereg_id,
                "strategy_hash": record.strategy_hash,
                "prereg_sha256": record.prereg_sha256,
                "at": to_iso(record.unlocked_at_ms),
            }
        )
    )
    return 0


def register(sub: Any) -> None:
    research = sub.add_parser("research", help="research protocol commands (owner)")
    rsub = research.add_subparsers(dest="research_command", required=True)
    u = rsub.add_parser("unlock-holdout", help="unlock the sealed holdout once for a prereg")
    u.add_argument("--prereg", required=True)
    u.add_argument("--strategy-hash", required=True)
    u.add_argument("--reason", required=True)
    u.add_argument("--repo-root", default=".")
    u.set_defaults(func=_cmd_unlock)
