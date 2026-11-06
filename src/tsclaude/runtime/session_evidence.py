"""Recorded-session replay verification (M3 gate: a recorded session replays to identical ledgers).

Two halves, both outside the journal (no journal format, event type or `PAPER_VERSION` change):

* `capture_session_end` runs when monitoring stops. It writes ``<data_dir>/session_end/<ts>.json``
  holding what the LIVE service computed incrementally: its ledger hashes and fingerprint, plus a
  digest of the journal it had written. It never overwrites a file.
* `verify_recorded_session` works on a COPY of the data directory's store (the original is never
  opened, so nothing in it can change), replays the journal with the existing `paper.replay`
  (which also checks, batch by batch, that every journaled event is reproduced) and compares the
  recomputed ledger hashes with the live ones in the evidence file.

Independence: the expected values come from the live run's own accumulation, written before the
replay existed; the actual values come from a fresh recomputation from the journaled inputs. Two
values derived from the same replay are never compared. Limits: the evidence is written by the
same process that ran the session, so it proves reproducibility, not honesty of the run; a crash
leaves no evidence (the verifier then reports INCONCLUSIVE).

Verdicts: PASS (exit 0), FAIL (exit 1: evidence contradicts the journal or the replay), and
INCONCLUSIVE (exit 2: evidence absent, stale or not comparable). Anything not proven is not PASS.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tsclaude.contracts.events import canonical_json
from tsclaude.sim.engine import ledger_hash
from tsclaude.sim.journal import Batch, EventStoreJournal, JournalError, flatten
from tsclaude.sim.paper import PAPER_VERSION, PaperConfig, RestoreError, replay
from tsclaude.store.eventstore import EventStore

log = logging.getLogger(__name__)

EVIDENCE_FORMAT = "tsc-session-end-v1"
EVIDENCE_DIR = "session_end"
PASS, FAIL, INCONCLUSIVE = "PASS", "FAIL", "INCONCLUSIVE"
EXIT_CODES = {PASS: 0, FAIL: 1, INCONCLUSIVE: 2}
_REQUIRED = (
    "format",
    "paper_version",
    "session_id",
    "written_at_ms",
    "batches",
    "journal_digest",
    "config_digest",
    "ledger_hashes",
    "fingerprint",
)


def journal_digest(batches: list[Batch]) -> str:
    """SHA-256 over the canonical form of every journal entry, in order."""
    text = canonical_json([e.canonical() for e in flatten(batches)])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def config_digest(config: PaperConfig) -> str:
    """The inputs the replay depends on: `describe()` (equity, latency, risk hash, costs, books)
    and the instrument keys."""
    payload = {
        "describe": config.describe(),
        "instruments": sorted(f"{s.market}:{s.symbol}" for s in config.instruments),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------- capture
def capture_session_end(
    data_dir: Path,
    *,
    config: PaperConfig,
    session_id: str,
    ledger_hashes: Mapping[str, str],
    fingerprint: str,
    batches: list[Batch],
    state: str,
    written_at_ms: int,
) -> Path:
    """Write the evidence file (exclusive create: an existing file is never touched)."""
    doc = {
        "format": EVIDENCE_FORMAT,
        "paper_version": PAPER_VERSION,
        "session_id": session_id,
        "written_at_ms": written_at_ms,
        "state": state,
        "batches": len(batches),
        "journal_digest": journal_digest(batches),
        "config_digest": config_digest(config),
        "ledger_hashes": dict(ledger_hashes),
        "fingerprint": fingerprint,
    }
    folder = data_dir / EVIDENCE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{written_at_ms:016d}-{session_id}.json"
    with path.open("x", encoding="utf-8") as fh:
        fh.write(json.dumps(doc, sort_keys=True, indent=2))
        fh.flush()
        os.fsync(fh.fileno())
    return path


# ---------------------------------------------------------------------------- verify
@dataclass
class Report:
    verdict: str
    reasons: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.verdict]

    def to_json(self) -> str:
        return json.dumps(
            {"verdict": self.verdict, "reasons": self.reasons, **self.details},
            sort_keys=True,
            indent=2,
        )


def _fingerprint_files(data_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(data_dir.rglob("*")):
        rel = p.relative_to(data_dir)
        if p.is_file() and "logs" not in rel.parts and p.name != "tsclaude.lock":
            out[str(p.relative_to(data_dir))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _pick_evidence(data_dir: Path, explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit
    files = sorted((data_dir / EVIDENCE_DIR).glob("*.json"))
    return files[-1] if files else None


def verify_recorded_session(
    data_dir: Path, config: PaperConfig, *, evidence: Path | None = None
) -> Report:
    """PASS / FAIL / INCONCLUSIVE for the recorded session in `data_dir` (read only)."""
    rep = Report(INCONCLUSIVE, details={"data_dir": str(data_dir)})
    db = data_dir / "state.db"
    if not db.is_file():
        rep.reasons.append(f"{db} not found: no recorded session")
        return rep
    path = _pick_evidence(data_dir, evidence)
    if path is None or not path.is_file():
        rep.reasons.append(
            "no session-end evidence (the session was not stopped cleanly, or it predates the "
            "capture): nothing independent to compare with"
        )
        return rep
    rep.details["evidence_file"] = str(path)
    try:
        ev = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(ev, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as exc:
        rep.verdict = FAIL
        rep.reasons.append(f"evidence file unreadable or corrupted: {exc}")
        return rep
    missing = [k for k in _REQUIRED if k not in ev]
    if (
        missing
        or ev.get("format") != EVIDENCE_FORMAT
        or not isinstance(ev["ledger_hashes"], dict)
        or isinstance(ev["batches"], bool)
        or not isinstance(ev["batches"], int)
    ):
        rep.verdict = FAIL
        rep.reasons.append(f"evidence malformed or of another format (missing {missing})")
        return rep
    if ev["paper_version"] != PAPER_VERSION:
        rep.reasons.append(
            f"evidence is for {ev['paper_version']}, this code is {PAPER_VERSION}: not comparable"
        )
        return rep
    if ev["config_digest"] != config_digest(config):
        rep.reasons.append("the configuration differs from the recorded session's: not comparable")
        return rep

    rep.details["session_state_at_stop"] = ev.get("state")
    try:
        before = _fingerprint_files(data_dir)
    except OSError as exc:
        rep.reasons.append(f"the data directory cannot be read (is the app still running?): {exc}")
        return rep
    with tempfile.TemporaryDirectory(prefix="tsc-verify-") as tmp:
        for name in ("state.db", "state.db-wal", "state.db-shm"):
            src = data_dir / name
            if src.is_file():
                shutil.copy2(src, Path(tmp) / name)
        with EventStore(Path(tmp) / "state.db") as store:
            try:
                batches = EventStoreJournal(store, str(ev["session_id"])).batches()
            except JournalError as exc:
                rep.verdict = FAIL
                rep.reasons.append(f"journal integrity: {exc}")
                return rep
    n = int(ev["batches"])
    rep.details.update(batches_in_journal=len(batches), batches_in_evidence=n)
    if n == 0 or not batches:
        rep.reasons.append("empty journal or evidence: no session to replay")
        return rep
    if len(batches) < n:
        rep.verdict = FAIL
        rep.reasons.append(f"journal has {len(batches)} batches, the evidence covers {n}")
        return rep
    if journal_digest(batches[:n]) != ev["journal_digest"]:
        rep.verdict = FAIL
        rep.reasons.append("the journal differs from the one the evidence was written for")
        return rep
    try:
        session = replay(config, batches[:n], session_id=str(ev["session_id"]))
    except (RestoreError, KeyError, ValueError) as exc:
        rep.verdict = FAIL
        rep.reasons.append(f"replay diverged from the journal: {type(exc).__name__}: {exc}")
        return rep
    actual = {name: ledger_hash(b) for name, b in session.sim.books.items()}
    expected = dict(ev["ledger_hashes"])
    rep.details.update(replayed_ledger_hashes=actual, expected_ledger_hashes=expected)
    if set(actual) != set(expected):
        rep.verdict = FAIL
        rep.reasons.append(f"books differ: replay {sorted(actual)} vs evidence {sorted(expected)}")
    for name in sorted(set(actual) & set(expected)):
        if actual[name] != expected[name]:
            rep.verdict = FAIL
            rep.reasons.append(
                f"ledger hash of book {name!r} differs from the live session's "
                f"(session state at stop: {ev.get('state')!r})"
            )
    if session.sim.result().fingerprint != ev["fingerprint"]:
        rep.verdict = FAIL
        rep.reasons.append("session fingerprint differs from the live session's")
    if rep.verdict == FAIL:
        return rep
    try:
        unchanged = _fingerprint_files(data_dir) == before
    except OSError:
        unchanged = False
    if not unchanged:
        rep.reasons.append(
            "the data directory changed during verification (is the app still running?): "
            "stop it and verify again"
        )
        return rep
    if ev.get("state") != "RUNNING":
        rep.reasons.append(
            f"the session ended {ev.get('state')!r}, not RUNNING: the replay reproduces, but a "
            "stopped, blocked or faulted session is not gate evidence"
        )
        return rep
    if len(batches) > n:
        rep.reasons.append(
            f"the journal continues {len(batches) - n} batches past the evidence (a later run did "
            "not stop cleanly): the first part reproduces, the whole session is not proven"
        )
        return rep
    rep.verdict = PASS
    rep.reasons.append(f"{n} batches replayed; every ledger hash equals the live session's")
    # Activity is read from the REPLAYED ledgers by memo ("capital" is the starting deposit;
    # "entry:" / "exit:" are simulated fills), never from the evidence file.
    books = session.sim.books
    fills = {
        name: {
            "entries": sum(1 for t in b.ledger.entries if t.memo.startswith("entry:")),
            "exits": sum(1 for t in b.ledger.entries if t.memo.startswith("exit:")),
        }
        for name, b in books.items()
    }
    observed = sum(1 for b in batches[:n] if b[0].payload.get("observed") is True)
    strategy_entries = sum(fills.get(k, {"entries": 0})["entries"] for k in ("baseline", "shadow"))
    rep.details.update(
        ledger_entries={name: len(b.ledger.entries) for name, b in books.items()},
        simulated_fills=fills,
        observed_bars=observed,
        trading_exercised=observed > 0 and any(f["entries"] > 0 for f in fills.values()),
        strategy_books_exercised=observed > 0 and strategy_entries > 0,
    )
    if not rep.details["trading_exercised"]:
        rep.reasons.append(
            f"NO TRADING EXERCISED ({observed} observed bars, fills {fills}): this PASS is "
            "a recording/replay reproducibility smoke test, not a validation of strategy execution"
        )
    elif not rep.details["strategy_books_exercised"]:
        rep.reasons.append(
            "only the benchmark book traded: baseline and shadow (the strategy books) made no "
            "simulated entry, so strategy execution is not demonstrated"
        )
    return rep
