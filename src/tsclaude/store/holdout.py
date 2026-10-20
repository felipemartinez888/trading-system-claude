"""Holdout guard (ADR-0009, RESEARCH_PROTOCOL §2, decisions D-05 and L-07).

The sealed holdout is 2026-01-01 → 2026-09-30 UTC, i.e. `[HOLDOUT_START_MS, HOLDOUT_END_MS)`.

Reads
- A `research` read whose range touches the holdout is refused unless a `holdout_unlocked` event
  exists for exactly that (`prereg_id`, `strategy_hash`). Reads ending at or before the holdout
  start (warm-up) are served; nothing inside the holdout is served as warm-up.
- `HoldoutGuard.check` returns a `HoldoutApproval`; only an approval with `holdout_access` lets
  `Lake.read_sealed_part` open a part under the sealed root (L-07 b).
- Unknown purposes are refused (fail closed).

Unlocks (L-07 a)
- Only through the owner-run CLI `tsclaude research unlock-holdout` (store/holdout_cli.py), which
  requires an interactive terminal and typing a confirmation phrase.
- Only for a preregistration `preregistrations/<prereg_id>.yaml` that is tracked by git and has
  no uncommitted changes; the file must declare `prereg_id` (equal to its name) and
  `strategy_hash`, and that hash must equal the one given on the command line. The file's
  SHA-256, the git HEAD commit and blob id are recorded in the event.
- Once per preregistration, enforced by a unique claim row in `state.db` taken in a single
  `BEGIN IMMEDIATE` transaction (so two concurrent unlocks cannot both succeed).
- The library refuses to unlock without an `UnlockAuthorization`, which only the CLI issues and
  which can be used once. Python cannot make this unforgeable; the durable controls are the
  committed preregistration (reviewable in git history), the hashes in the chained event, and the
  single unlock per preregistration.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from tsclaude.contracts.events import AggregateType, EventType, NewEvent
from tsclaude.contracts.timeutil import to_iso
from tsclaude.store.eventstore import EventStore

log = logging.getLogger(__name__)

HOLDOUT_START_MS = 1_767_225_600_000  # 2026-01-01T00:00:00Z
HOLDOUT_END_MS = 1_790_812_800_000  # 2026-10-01T00:00:00Z (exclusive; last day 2026-09-30)
PREREG_DIR = "preregistrations"
_PREREG_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_STRATEGY_HASH = re.compile(r"[A-Za-z0-9:._-]{8,128}")


class Purpose(StrEnum):
    RESEARCH = "research"


class HoldoutError(PermissionError):
    pass


class HoldoutLocked(HoldoutError):
    """A research read touched the sealed holdout without a matching unlock."""


class HoldoutAlreadyUnlocked(HoldoutError):
    """The preregistration already used its single unlock."""


class PreregistrationError(HoldoutError):
    """The preregistration file is missing, not committed, modified, or inconsistent."""


def confirmation_phrase(prereg_id: str) -> str:
    return f"UNLOCK HOLDOUT {prereg_id}"


def touches_holdout(start_ms: int, end_ms: int) -> bool:
    return start_ms < HOLDOUT_END_MS and end_ms > HOLDOUT_START_MS


# ---------------------------------------------------------------------------- approvals
_APPROVAL_ISSUER = object()


class HoldoutApproval:
    """Issued by `HoldoutGuard.check` for one read range."""

    __slots__ = ("end_ms", "holdout_access", "prereg_id", "start_ms", "strategy_hash")

    def __init__(
        self,
        *,
        issuer: object,
        start_ms: int,
        end_ms: int,
        holdout_access: bool,
        prereg_id: str | None,
        strategy_hash: str | None,
    ) -> None:
        if issuer is not _APPROVAL_ISSUER:
            raise HoldoutError("holdout approvals are issued by HoldoutGuard.check only")
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.holdout_access = holdout_access
        self.prereg_id = prereg_id
        self.strategy_hash = strategy_hash


# ---------------------------------------------------------------------------- preregistrations
@dataclass(frozen=True, slots=True)
class PreregEvidence:
    prereg_id: str
    strategy_hash: str
    path: str  # posix, relative to the repository root
    sha256: str  # of the file content
    git_head: str
    git_blob: str


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if git is None:
        raise PreregistrationError("git is not available; cannot verify the preregistration")
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [git, "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


def inspect_preregistration(repo_root: Path | str, prereg_id: str) -> PreregEvidence:
    """Verify `preregistrations/<prereg_id>.yaml` is committed, clean and self-consistent."""
    if not _PREREG_ID.fullmatch(prereg_id):
        raise PreregistrationError(f"invalid preregistration id {prereg_id!r}")
    repo = Path(repo_root).resolve()
    rel = f"{PREREG_DIR}/{prereg_id}.yaml"
    path = repo / rel
    if not path.is_file():
        raise PreregistrationError(f"{rel} does not exist")
    if _git(repo, "ls-files", "--error-unmatch", "--", rel).returncode != 0:
        raise PreregistrationError(f"{rel} is not tracked by git")
    status = _git(repo, "status", "--porcelain", "--", rel)
    if status.returncode != 0 or status.stdout.strip():
        raise PreregistrationError(f"{rel} has uncommitted changes")
    head = _git(repo, "rev-parse", "HEAD")
    blob = _git(repo, "rev-parse", f"HEAD:{rel}")
    if head.returncode != 0 or blob.returncode != 0:
        raise PreregistrationError(f"{rel} is not in the HEAD commit")
    content = path.read_bytes()
    import yaml

    try:
        doc = yaml.safe_load(content.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise PreregistrationError(f"{rel} is not valid YAML") from exc
    if not isinstance(doc, dict):
        raise PreregistrationError(f"{rel} must be a mapping")
    if doc.get("prereg_id") != prereg_id:
        raise PreregistrationError(f"{rel} declares prereg_id {doc.get('prereg_id')!r}")
    declared = doc.get("strategy_hash")
    if not isinstance(declared, str) or not _STRATEGY_HASH.fullmatch(declared):
        raise PreregistrationError(f"{rel} must declare a strategy_hash")
    return PreregEvidence(
        prereg_id=prereg_id,
        strategy_hash=declared,
        path=rel,
        sha256=hashlib.sha256(content).hexdigest(),
        git_head=head.stdout.strip(),
        git_blob=blob.stdout.strip(),
    )


class UnlockAuthorization:
    """Single-use permission to record one unlock. Issued by store.holdout_cli only."""

    __slots__ = ("_used", "evidence", "reason")

    def __init__(self, *, issuer: object, evidence: PreregEvidence, reason: str) -> None:
        from tsclaude.store import holdout_cli

        if issuer is not holdout_cli.ISSUER:
            raise HoldoutError(
                "holdout unlocks are authorized only by `tsclaude research "
                "unlock-holdout` (owner, interactive)"
            )
        if not reason.strip():
            raise HoldoutError("an unlock needs a reason")
        self.evidence = evidence
        self.reason = reason.strip()
        self._used = False

    def consume(self) -> PreregEvidence:
        if self._used:
            raise HoldoutError("this unlock authorization was already used")
        self._used = True
        return self.evidence


# ---------------------------------------------------------------------------- registries
@dataclass(frozen=True, slots=True)
class UnlockRecord:
    prereg_id: str
    strategy_hash: str
    unlocked_at_ms: int
    reason: str
    prereg_sha256: str | None = None


class UnlockRegistry(Protocol):
    def find(self, prereg_id: str) -> UnlockRecord | None: ...


_CLAIMS = """
CREATE TABLE IF NOT EXISTS holdout_unlock_claims (
    prereg_id     TEXT PRIMARY KEY,
    strategy_hash TEXT NOT NULL,
    prereg_sha256 TEXT NOT NULL,
    claimed_at_ms INTEGER NOT NULL
);
"""


class EventStoreUnlocks:
    """Unlocks as chained `holdout_unlocked` events; one per preregistration."""

    def __init__(self, store: EventStore) -> None:
        self._store = store

    @staticmethod
    def aggregate_id(prereg_id: str) -> str:
        return f"prereg:{prereg_id}"

    def find(self, prereg_id: str) -> UnlockRecord | None:
        events = self._store.read(
            types=[EventType.HOLDOUT_UNLOCKED],
            aggregate=(AggregateType.RESEARCH, self.aggregate_id(prereg_id)),
            limit=1,
        )
        if not events:
            return None
        p = events[0].payload
        return UnlockRecord(
            str(p["prereg_id"]),
            str(p["strategy_hash"]),
            int(p["unlocked_at_ms"]),
            str(p["reason"]),
            str(p.get("prereg_sha256")) if p.get("prereg_sha256") else None,
        )

    def unlock(self, authorization: UnlockAuthorization, *, now_ms: int) -> UnlockRecord:
        if not isinstance(authorization, UnlockAuthorization):
            raise HoldoutError("an UnlockAuthorization is required")
        ev = authorization.consume()
        self._claim(ev, now_ms)
        payload: dict[str, Any] = {
            "prereg_id": ev.prereg_id,
            "strategy_hash": ev.strategy_hash,
            "prereg_path": ev.path,
            "prereg_sha256": ev.sha256,
            "git_head": ev.git_head,
            "git_blob": ev.git_blob,
            "reason": authorization.reason,
            "unlocked_at_ms": now_ms,
            "holdout_from_ms": HOLDOUT_START_MS,
            "holdout_to_ms": HOLDOUT_END_MS,
            "confirmation": "owner typed the confirmation phrase in an interactive terminal",
        }
        self._store.append(
            NewEvent(
                type=EventType.HOLDOUT_UNLOCKED,
                aggregate_type=AggregateType.RESEARCH,
                aggregate_id=self.aggregate_id(ev.prereg_id),
                ts_ms=now_ms,
                payload=payload,
            )
        )
        log.warning(
            "holdout unlocked", extra={"prereg_id": ev.prereg_id, "strategy_hash": ev.strategy_hash}
        )
        return UnlockRecord(ev.prereg_id, ev.strategy_hash, now_ms, authorization.reason, ev.sha256)

    def _claim(self, ev: PreregEvidence, now_ms: int) -> None:
        """Atomically take the single unlock of this preregistration (or fail)."""
        conn = sqlite3.connect(self._store.path, isolation_level=None, timeout=30.0)
        try:
            conn.executescript(_CLAIMS)
            conn.execute("BEGIN IMMEDIATE")
            try:
                if self.find(ev.prereg_id) is not None:
                    raise HoldoutAlreadyUnlocked(f"{ev.prereg_id} already unlocked the holdout")
                conn.execute(
                    "INSERT INTO holdout_unlock_claims VALUES (?,?,?,?)",
                    (ev.prereg_id, ev.strategy_hash, ev.sha256, now_ms),
                )
                conn.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                conn.execute("ROLLBACK")
                raise HoldoutAlreadyUnlocked(
                    f"{ev.prereg_id} already claimed its single holdout unlock"
                ) from exc
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()


class SealedUnlocks:
    """A registry with no unlocks: for readers that must never touch the holdout."""

    def find(self, prereg_id: str) -> UnlockRecord | None:
        return None


# ---------------------------------------------------------------------------- guard
class HoldoutGuard:
    def __init__(self, unlocks: UnlockRegistry) -> None:
        self._unlocks = unlocks

    def check(
        self,
        start_ms: int,
        end_ms: int,
        *,
        purpose: Purpose | str,
        prereg_id: str | None = None,
        strategy_hash: str | None = None,
    ) -> HoldoutApproval:
        """Approve the read [start_ms, end_ms) for `purpose` or raise."""
        try:
            purpose = Purpose(purpose)
        except ValueError:
            raise HoldoutError(f"unknown read purpose {purpose!r}; refused (fail closed)") from None
        access = end_ms > start_ms and touches_holdout(start_ms, end_ms)
        if access:
            self._require_unlock(start_ms, end_ms, prereg_id, strategy_hash)
        return HoldoutApproval(
            issuer=_APPROVAL_ISSUER,
            start_ms=start_ms,
            end_ms=end_ms,
            holdout_access=access,
            prereg_id=prereg_id if access else None,
            strategy_hash=strategy_hash if access else None,
        )

    def _require_unlock(
        self, start_ms: int, end_ms: int, prereg_id: str | None, strategy_hash: str | None
    ) -> None:
        if not prereg_id or not strategy_hash:
            raise HoldoutLocked(
                f"[{to_iso(start_ms)}, {to_iso(end_ms)}) touches the sealed holdout "
                f"[{to_iso(HOLDOUT_START_MS)}, {to_iso(HOLDOUT_END_MS)}); research reads there "
                "need prereg_id and strategy_hash with a recorded unlock"
            )
        record = self._unlocks.find(prereg_id)
        if record is None:
            raise HoldoutLocked(f"no holdout unlock recorded for preregistration {prereg_id}")
        if record.strategy_hash != strategy_hash:
            raise HoldoutLocked(
                f"the holdout unlock of {prereg_id} is for strategy {record.strategy_hash}, "
                f"not {strategy_hash}"
            )
