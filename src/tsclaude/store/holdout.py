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
    """Issued by `HoldoutGuard.check` for one read range. Immutable and informational only:
    `Lake.read_sealed_part` never trusts it and re-checks the unlock through the guard."""

    __slots__ = ("end_ms", "holdout_access", "prereg_id", "start_ms", "strategy_hash")
    start_ms: int
    end_ms: int
    holdout_access: bool
    prereg_id: str | None
    strategy_hash: str | None

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
        set_ = object.__setattr__
        set_(self, "start_ms", start_ms)
        set_(self, "end_ms", end_ms)
        set_(self, "holdout_access", holdout_access)
        set_(self, "prereg_id", prereg_id)
        set_(self, "strategy_hash", strategy_hash)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("HoldoutApproval is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("HoldoutApproval is immutable")


# ---------------------------------------------------------------------------- preregistrations
@dataclass(frozen=True, slots=True)
class PreregEvidence:
    prereg_id: str
    strategy_hash: str
    path: str  # posix, relative to the repository root
    sha256: str  # of the committed blob content
    git_head: str
    git_blob: str


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if git is None:
        raise PreregistrationError("git is not available; cannot verify the preregistration")
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [git, "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


def _git_bytes(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    git = shutil.which("git")
    if git is None:
        raise PreregistrationError("git is not available; cannot verify the preregistration")
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [git, "-C", str(repo), *args], capture_output=True, check=False
    )


def _declared(content: bytes, rel: str) -> tuple[object, object]:
    """(prereg_id, strategy_hash) declared by a preregistration file's content."""
    import yaml

    try:
        doc = yaml.safe_load(content.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise PreregistrationError(f"{rel} is not valid YAML") from exc
    if not isinstance(doc, dict):
        raise PreregistrationError(f"{rel} must be a mapping")
    return doc.get("prereg_id"), doc.get("strategy_hash")


def verify_recorded_unlock(repo_root: Path | str, payload: dict[str, Any]) -> str | None:
    """Why a recorded unlock is NOT valid against the repository (None = valid).

    The recorded blob must exist, hash to the recorded SHA-256, be the preregistration file at the
    recorded commit, declare the same prereg_id and strategy_hash, and that commit must be an
    ancestor of the current HEAD.
    """
    repo = Path(repo_root).resolve()
    try:
        prereg_id = str(payload["prereg_id"])
        strategy = str(payload["strategy_hash"])
        sha = str(payload["prereg_sha256"])
        head = str(payload["git_head"])
        blob = str(payload["git_blob"])
    except KeyError as exc:
        return f"unlock event lacks {exc.args[0]}"
    if not _PREREG_ID.fullmatch(prereg_id) or not re.fullmatch(r"[0-9a-f]{40,64}", blob):
        return "unlock event has invalid identifiers"
    if not re.fullmatch(r"[0-9a-f]{40,64}", head):
        return "unlock event has an invalid git_head"
    rel = f"{PREREG_DIR}/{prereg_id}.yaml"
    content = _git_bytes(repo, "cat-file", "blob", blob)
    if content.returncode != 0:
        return "recorded preregistration blob does not exist in the repository"
    if hashlib.sha256(content.stdout).hexdigest() != sha:
        return "recorded preregistration blob does not match the recorded SHA-256"
    at_head = _git(repo, "rev-parse", f"{head}:{rel}")
    if at_head.returncode != 0 or at_head.stdout.strip() != blob:
        return "recorded blob is not the preregistration file at the recorded commit"
    if _git(repo, "merge-base", "--is-ancestor", head, "HEAD").returncode != 0:
        return "recorded commit is not an ancestor of HEAD"
    try:
        declared_id, declared_hash = _declared(content.stdout, rel)
    except PreregistrationError as exc:
        return str(exc)
    if declared_id != prereg_id or declared_hash != strategy:
        return "recorded preregistration does not declare this prereg_id and strategy_hash"
    return None


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
    # Hash and parse the *committed* blob (line endings in the working tree vary by platform).
    blob_bytes = _git_bytes(repo, "cat-file", "blob", blob.stdout.strip())
    if blob_bytes.returncode != 0:
        raise PreregistrationError(f"{rel}: committed blob cannot be read")
    content = blob_bytes.stdout
    declared_id, declared = _declared(content, rel)
    if declared_id != prereg_id:
        raise PreregistrationError(f"{rel} declares prereg_id {declared_id!r}")
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
    """Unlocks as chained `holdout_unlocked` events; one per preregistration.

    An event alone is not trusted (R1): `find` accepts an unlock only if the matching claim row
    exists and the recorded preregistration verifies against the git repository
    (`verify_recorded_unlock`). Anything else fails closed.
    """

    def __init__(self, store: EventStore, repo_root: Path | str) -> None:
        self._store = store
        self._repo = Path(repo_root)

    @staticmethod
    def aggregate_id(prereg_id: str) -> str:
        return f"prereg:{prereg_id}"

    def _events(self, prereg_id: str) -> list[Any]:
        return self._store.read(
            types=[EventType.HOLDOUT_UNLOCKED],
            aggregate=(AggregateType.RESEARCH, self.aggregate_id(prereg_id)),
        )

    def _claim_row(self, prereg_id: str) -> tuple[str, str] | None:
        conn = sqlite3.connect(self._store.path, isolation_level=None, timeout=30.0)
        try:
            row = conn.execute(
                "SELECT strategy_hash, prereg_sha256 FROM holdout_unlock_claims"
                " WHERE prereg_id = ?",
                (prereg_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None  # no claims table: nothing was ever unlocked through the CLI
        finally:
            conn.close()
        return None if row is None else (str(row[0]), str(row[1]))

    def find(self, prereg_id: str) -> UnlockRecord | None:
        events = self._events(prereg_id)
        if not events:
            return None
        if len(events) != 1:
            log.error("several holdout_unlocked events; refusing", extra={"prereg_id": prereg_id})
            return None
        p = events[0].payload
        claim = self._claim_row(prereg_id)
        if claim is None or claim != (p.get("strategy_hash"), p.get("prereg_sha256")):
            log.error(
                "holdout unlock without a matching claim; refusing", extra={"prereg_id": prereg_id}
            )
            return None
        if p.get("prereg_id") != prereg_id:
            return None
        problem = verify_recorded_unlock(self._repo, p)
        if problem is not None:
            log.error(
                "holdout unlock does not verify; refusing",
                extra={"prereg_id": prereg_id, "problem": problem},
            )
            return None
        return UnlockRecord(
            str(p["prereg_id"]),
            str(p["strategy_hash"]),
            int(p["unlocked_at_ms"]),
            str(p["reason"]),
            str(p["prereg_sha256"]),
        )

    def unlock(self, authorization: UnlockAuthorization, *, now_ms: int) -> UnlockRecord:
        if not isinstance(authorization, UnlockAuthorization):
            raise HoldoutError("an UnlockAuthorization is required")
        ev = authorization.consume()
        # The evidence is never trusted as given: it must be exactly what the repository says now.
        if inspect_preregistration(self._repo, ev.prereg_id) != ev:
            raise PreregistrationError("unlock evidence does not match the repository")
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
                if self._events(ev.prereg_id):  # any recorded event, valid or not
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
        # Only the two real registries: a custom registry could simply claim an unlock exists.
        if type(unlocks) not in (EventStoreUnlocks, SealedUnlocks):
            raise HoldoutError("HoldoutGuard accepts EventStoreUnlocks or SealedUnlocks only")
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
