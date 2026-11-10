"""Versioned JSON Lines evaluation export (format ``tsc-eval`` v1; M6, EVALUATION_PROTOCOL §4).

What is exported is what the app recorded in its hash-chained event store (ADR-0011), read through
`EventStore`'s public read API only, never re-simulated:

- ``steps.jsonl``: the input entry of every paper batch (released bar with its observed flag,
  release time and minute close mark; funding; manual reset);
- ``signals.jsonl``, ``transitions.jsonl``: signal records at creation and their lifecycle
  (SIGNALS_AND_AGENTS §2), including shadow outcomes;
- ``fills.jsonl``, ``positions.jsonl``, ``orders.jsonl``, ``ledger.jsonl``, ``sim_events.jsonl``:
  every simulator event of each paper book;
- ``coverage.jsonl``: app sessions, monitoring intervals, crash detection, data gaps and their
  recovery, stream and clock state;
- ``manifest.json``: format version, app version, chain head, per-file line counts and hashes,
  schema hashes, the event types deliberately not exported (with counts).

Every line carries the verbatim source event (seq, hashes, payload), its provenance label
(ADR-0010: demo-detector signals ``DEMO``; paper records ``SIMULATED``; bar steps by bar source;
coverage ``SYSTEM``), the chain head at export time, a per-file line number, ``prev_line_hash`` and
its own ``line_hash``. Nothing is fabricated: a gap appears only as the gap records the store has
(``gap_detected`` / ``gap_resolved``, ``sim_bar_gap``, unobserved steps).

Fail closed (`ExportError`): a broken hash chain, a paper batch that is truncated, nested or holds
an event type this format does not map, a simulator or lifecycle event outside a paper batch that
is not a demo signal, or an output directory that is not empty.

Deterministic: the same store (up to the same head) exports byte-identical ``*.jsonl`` files; the
manifest differs only by the optional ``exported_at_ms`` / ``git_commit`` / protocol hash given.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tsclaude import __version__
from tsclaude.contracts.events import Event
from tsclaude.contracts.provenance import Provenance
from tsclaude.contracts.timeutil import to_iso
from tsclaude.evaluation.scorer import (
    FILE_OF,
    FILES,
    FORMAT,
    FORMAT_VERSION,
    GENESIS,
    MANIFEST,
    SchemaSet,
    canonical_json,
    line_hash,
)
from tsclaude.signals.demo import STRATEGY_ID as DEMO_STRATEGY_ID
from tsclaude.store.eventstore import EventStore

SYSTEM = "trading-system-claude"
PROTOCOL_DOCUMENT = "docs/EVALUATION_PROTOCOL.md"
PROTOCOL_STATUS = "DRAFT v0 (not frozen, not agreed with the other team)"
STEP_TYPE = "paper_step"
SIM_AGGREGATE = "sim"
READ_PAGE = 5000

_BAR_PROVENANCE = {
    "ws_live": Provenance.LIVE,
    "rest_recovered": Provenance.RECOVERED,
    "rest_backfill": Provenance.HISTORICAL,
    "archive": Provenance.HISTORICAL,
    "derived": Provenance.HISTORICAL,
}
_COVERAGE = frozenset(FILES["coverage.jsonl"])
_SIGNAL_TYPES = frozenset({"signal_created", "signal_transitioned", "signal_shadow_outcome"})


class ExportError(RuntimeError):
    """The store cannot be exported faithfully (nothing is written, or the partial output must be
    discarded)."""


@dataclass(frozen=True)
class ExportResult:
    directory: Path
    chain_head: dict[str, Any]
    lines: dict[str, int]
    manifest: dict[str, Any]


def _is_demo(payload: dict[str, Any]) -> bool:
    return payload.get("demo") is True or payload.get("strategy_id") == DEMO_STRATEGY_ID


def _events(store: EventStore, upto_seq: int) -> Iterator[Event]:
    after = 0
    while after < upto_seq:
        page = store.read(after_seq=after, limit=min(READ_PAGE, upto_seq - after))
        if not page:
            return
        yield from page
        after = page[-1].seq


def _event_dict(e: Event) -> dict[str, Any]:
    return {
        "seq": e.seq,
        "ts_ms": e.ts_ms,
        "type": str(e.type),
        "aggregate_type": str(e.aggregate_type),
        "aggregate_id": e.aggregate_id,
        "payload": e.payload,
        "schema_version": e.schema_version,
        "prev_hash": e.prev_hash,
        "hash": e.hash,
    }


def build_lines(
    store: EventStore, *, upto_seq: int | None = None
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any], Counter[str], list[str]]:
    """Every export line by file, the chain head, the not-exported type counts and the paper
    session ids (in first-seen order)."""
    report = store.verify()
    if not report.ok:
        raise ExportError(
            f"event store hash chain is broken at seq {report.first_bad_seq}: {report.reason}"
        )
    head_seq, head_hash = store.head
    if upto_seq is not None:
        if not 0 <= upto_seq <= head_seq:
            raise ExportError(f"upto_seq {upto_seq} outside the store (head {head_seq})")
        if upto_seq == 0:
            head_hash = GENESIS
        else:
            (at,) = store.read(after_seq=upto_seq - 1, limit=1)
            head_hash = at.hash
        head_seq = upto_seq
    head = {"seq": head_seq, "hash": head_hash}
    files: dict[str, list[dict[str, Any]]] = {name: [] for name in FILES}
    prev: dict[str, str] = dict.fromkeys(FILES, GENESIS)
    skipped: Counter[str] = Counter()
    sessions: list[str] = []
    demo_signals: set[str] = set()
    batch: tuple[int, str, int] | None = None  # (step seq, session id, events left)

    for e in _events(store, head_seq):
        kind = str(e.type)
        session: str | None = None
        batch_seq: int | None = None
        if kind == STEP_TYPE and str(e.aggregate_type) == SIM_AGGREGATE:
            if batch is not None:
                raise ExportError(f"paper step at seq {e.seq} inside the batch of seq {batch[0]}")
            size = e.payload.get("batch_size")
            if not isinstance(size, int) or size < 1:
                raise ExportError(f"paper step at seq {e.seq} has no valid batch_size")
            batch = (e.seq, e.aggregate_id, size)
            if e.aggregate_id not in sessions:
                sessions.append(e.aggregate_id)
        if batch is not None:
            batch_seq, session, left = batch
            if kind not in FILE_OF or kind in _COVERAGE:
                raise ExportError(f"event {kind!r} at seq {e.seq} inside a paper batch")
            left -= 1
            batch = None if left == 0 else (batch_seq, session, left)
            provenance = _batch_provenance(e)
        elif kind in FILE_OF:
            provenance = _outside_provenance(e, demo_signals)
        else:
            skipped[kind] += 1
            continue
        name = FILE_OF[kind]
        line: dict[str, Any] = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "record_type": kind,
            "line_no": len(files[name]) + 1,
            "prev_line_hash": prev[name],
            "provenance": str(provenance),
            "paper_session_id": session,
            "batch_seq": batch_seq,
            "event": _event_dict(e),
            "chain_head": head,
        }
        line["line_hash"] = line_hash(line)
        prev[name] = line["line_hash"]
        files[name].append(line)
    if batch is not None:
        raise ExportError(
            f"paper batch at seq {batch[0]} is cut by the chain head {head_seq} "
            f"({batch[2]} events missing)"
        )
    return files, head, skipped, sessions


def _batch_provenance(e: Event) -> Provenance:
    if str(e.type) == "signal_created" and _is_demo(e.payload):
        return Provenance.DEMO
    if str(e.type) == STEP_TYPE:
        if e.payload.get("kind") == "bar":
            source = str((e.payload.get("bar") or {}).get("source"))
            if source not in _BAR_PROVENANCE:
                raise ExportError(f"paper step at seq {e.seq}: unknown bar source {source!r}")
            return _BAR_PROVENANCE[source]
        return Provenance.SIMULATED
    return Provenance.SIMULATED


def _outside_provenance(e: Event, demo_signals: set[str]) -> Provenance:
    kind = str(e.type)
    if kind in _COVERAGE:
        return Provenance.SYSTEM
    if kind == "signal_created" and _is_demo(e.payload):
        demo_signals.add(e.aggregate_id)
        return Provenance.DEMO
    if kind in _SIGNAL_TYPES and e.aggregate_id in demo_signals:
        return Provenance.DEMO
    raise ExportError(
        f"{kind} at seq {e.seq} is outside any paper batch and not a demo signal: "
        "its provenance cannot be established"
    )


def export_store(
    store: EventStore,
    out_dir: Path | str,
    *,
    upto_seq: int | None = None,
    exported_at_ms: int | None = None,
    git_commit: str | None = None,
    protocol_sha256: str | None = None,
    schemas: SchemaSet | None = None,
) -> ExportResult:
    """Write the export into `out_dir` (created; must be absent or empty)."""
    out = Path(out_dir)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ExportError(f"{out} exists and is not an empty directory")
    files, head, skipped, sessions = build_lines(store, upto_seq=upto_seq)
    schemas = schemas or SchemaSet()
    out.mkdir(parents=True, exist_ok=True)
    entries: dict[str, Any] = {}
    for name in sorted(files):
        data = "".join(canonical_json(line) + "\n" for line in files[name]).encode("utf-8")
        (out / name).write_bytes(data)
        entries[name] = {
            "lines": len(files[name]),
            "sha256": hashlib.sha256(data).hexdigest(),
            "last_line_hash": files[name][-1]["line_hash"] if files[name] else None,
        }
    manifest: dict[str, Any] = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "system": SYSTEM,
        "app_version": __version__,
        "git_commit": git_commit,
        "protocol": {
            "document": PROTOCOL_DOCUMENT,
            "status": PROTOCOL_STATUS,
            "sha256": protocol_sha256,
        },
        "exported_at_ms": exported_at_ms,
        "exported_at": None if exported_at_ms is None else to_iso(exported_at_ms),
        "chain_head": head,
        "chain_verified": True,
        "files": entries,
        "schemas": schemas.digests(),
        "paper_sessions": sessions,
        "not_exported": dict(sorted(skipped.items())),
    }
    (out / MANIFEST).write_bytes((canonical_json(manifest) + "\n").encode("utf-8"))
    return ExportResult(out, head, {k: v["lines"] for k, v in entries.items()}, manifest)
