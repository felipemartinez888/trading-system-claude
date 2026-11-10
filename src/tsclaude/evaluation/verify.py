"""Export verifier (M6): the scorer's structural checks plus, when the store is at hand, the
store-side checks only the producing system can make.

Structural (from the export alone, `scorer.check_export`): schema validation of every line and
the manifest, canonical form, line hashes and ``prev_line_hash`` links, line numbers, manifest
line counts and file hashes, every source event's chain hash, chain links between consecutive
exported events, monotonic event sequence per file, a single chain head, complete paper batches;
and the manifest's schema hashes equal the bundled schema files.

Against the store (`store` given): the store's whole hash chain verifies; the export's chain head
is in the store (same hash at that seq; the store may have grown since); and re-exporting the
store up to that head reproduces every file byte for byte, so no line is missing, added or edited
and nothing outside the export was left out unannounced.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tsclaude.evaluation.export import ExportError, export_store
from tsclaude.evaluation.scorer import FILES, MANIFEST, SchemaSet, check_export
from tsclaude.store.eventstore import EventStore


@dataclass
class VerifyReport:
    ok: bool
    problems: list[str] = field(default_factory=list)
    lines: int = 0
    chain_head: dict[str, Any] | None = None
    store_checked: bool = False
    #: with a store: its head seq and how many events it holds after the export's head (a prefix
    #: export verifies, but it does not cover those events); None without a store. Without a
    #: store only internal consistency is checked: the hashes are unkeyed, so a fully re-chained
    #: forgery cannot be told apart from a genuine export
    store_head_seq: int | None = None
    events_after_head: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "problems": self.problems,
            "lines": self.lines,
            "chain_head": self.chain_head,
            "store_checked": self.store_checked,
            "store_head_seq": self.store_head_seq,
            "events_after_head": self.events_after_head,
            "covers_whole_store": None
            if self.events_after_head is None
            else self.events_after_head == 0,
        }


def verify_export(
    directory: Path | str, *, store: EventStore | None = None, schemas: SchemaSet | None = None
) -> VerifyReport:
    root = Path(directory)
    schemas = schemas or SchemaSet()
    check, export = check_export(root, schemas)
    report = VerifyReport(check.ok, list(check.problems), check.lines, check.chain_head)
    if export is not None and export.manifest.get("schemas") != schemas.digests():
        report.problems.append("manifest schema hashes differ from the bundled schemas")
    if store is not None and export is not None:
        report.store_checked = True
        report.problems.extend(_against_store(root, export.manifest, store, schemas))
        store_seq, _ = store.head
        report.store_head_seq = int(store_seq)
        report.events_after_head = max(
            0, int(store_seq) - int(export.manifest["chain_head"]["seq"])
        )
    report.ok = not report.problems
    return report


def _against_store(
    root: Path, manifest: dict[str, Any], store: EventStore, schemas: SchemaSet
) -> list[str]:
    problems: list[str] = []
    chain = store.verify()
    if not chain.ok:
        return [f"store hash chain is broken at seq {chain.first_bad_seq}: {chain.reason}"]
    head = manifest["chain_head"]
    seq = int(head["seq"])
    store_seq, _ = store.head
    if seq > store_seq:
        return [f"export chain head seq {seq} is beyond the store head {store_seq}"]
    if seq > 0:
        (at,) = store.read(after_seq=seq - 1, limit=1)
        if at.hash != head["hash"]:
            return [f"export chain head hash differs from the store's event at seq {seq}"]
    with tempfile.TemporaryDirectory(prefix="tsc-eval-verify-") as tmp:
        again = Path(tmp) / "export"
        try:
            export_store(
                store,
                again,
                upto_seq=seq,
                exported_at_ms=manifest.get("exported_at_ms"),
                git_commit=manifest.get("git_commit"),
                protocol_sha256=(manifest.get("protocol") or {}).get("sha256"),
                schemas=schemas,
            )
        except ExportError as exc:
            return [f"re-export failed: {exc}"]
        for name in [*sorted(FILES), MANIFEST]:
            mine = again / name
            theirs = root / name
            if not theirs.is_file() or theirs.read_bytes() != mine.read_bytes():
                problems.append(f"{name}: differs from a fresh export of the store")
        extra = sorted(p.name for p in root.iterdir() if p.name not in {*FILES, MANIFEST})
        if extra:
            problems.append(f"unexpected files in the export: {extra}")
    return problems


def report_json(report: VerifyReport) -> str:
    return json.dumps(report.to_dict(), indent=2, sort_keys=True)
