"""Independent scorer for the evaluation export (format ``tsc-eval`` version 1, M6).

**Standard library only.** This module imports nothing from ``tsclaude`` (a unit test enforces it),
so the other team can run it with only this file and the schema files next to it::

    python scorer.py EXPORT_DIR [--schemas DIR] [--check-only]

It does two things, from the export directory alone:

1. ``check_export``: validates every line against the JSON Schemas (a small built-in validator for
   the subset of JSON Schema 2020-12 the schemas use), recomputes every line hash and the
   ``prev_line_hash`` links, checks line numbers, file hashes and line counts against the
   manifest, recomputes every source event's chain hash (ADR-0011) from the event fields, checks
   the chain links between consecutive exported events, the per-file monotonic event sequence,
   that every line names the manifest's chain head, and that every paper batch is complete
   (``batch_size`` lines, contiguous sequence numbers).
2. ``score``: recomputes the paper-book and signal values the dashboard shows (Paper and Signals
   pages, ``/api/paper/books|fills|activity``, ``/api/signals``) from the exported records: ledger
   cash and hash, mark-to-market equity from the journaled bar closes, unrealized P&L, open
   positions, closed-trade counts and flags, fill-time rejections, drawdown statistics, halts,
   the fills / closed trades / request-rejection lists, and the demo WATCH list.
   ``NOT_DERIVABLE`` lists what the dashboard shows that the export cannot reproduce, and why.

Money is recomputed with ``decimal`` at 40 significant digits, half-even, quantized to 1e-8 where
the simulator quantizes (the simulator's documented rules; SIMULATION.md). Bar prices are JSON
numbers; they are converted through their shortest round-trip decimal text (``repr``), exactly the
text the export contains.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

FORMAT = "tsc-eval"
FORMAT_VERSION = 1
SCORER_VERSION = "tsc-eval-scorer-1"
GENESIS = "0" * 64
MANIFEST = "manifest.json"
SCHEMA_DIR = Path(__file__).resolve().parent / "schemas" / "v1"

#: export file -> the record (event) types it holds; one file per record family (EVALUATION
#: PROTOCOL §4, extended with the records the books need: steps, orders, ledger, sim events)
FILES: dict[str, tuple[str, ...]] = {
    "signals.jsonl": ("signal_created",),
    "transitions.jsonl": ("signal_transitioned", "signal_shadow_outcome"),
    "steps.jsonl": ("paper_step",),
    "fills.jsonl": ("sim_order_filled",),
    "positions.jsonl": ("sim_position_opened", "sim_position_closed"),
    "orders.jsonl": (
        "sim_order_submitted",
        "sim_order_cancelled",
        "sim_order_expired",
        "sim_order_rejected",
        "sim_request_rejected",
        "sim_exit_requested",
    ),
    "ledger.jsonl": ("sim_ledger_posted",),
    "sim_events.jsonl": (
        "sim_run_started",
        "sim_run_completed",
        "sim_bar_gap",
        "sim_source_error",
        "sim_funding_applied",
        "sim_book_halted",
        "sim_book_reset",
        "sim_observation_changed",
    ),
    "coverage.jsonl": (
        "session_started",
        "session_stopped",
        "session_crash_detected",
        "monitoring_started",
        "monitoring_stopped",
        "gap_detected",
        "gap_resolved",
        "backfill_completed",
        "bar_late",
        "bar_conflict",
        "stream_state_changed",
        "clock_skew_changed",
        "chain_verified",
    ),
}
FILE_OF: dict[str, str] = {t: f for f, types in FILES.items() for t in types}

#: Dashboard values the export cannot reproduce (Paper and Signals pages), and why.
NOT_DERIVABLE: tuple[dict[str, str], ...] = (
    {
        "metric": "paper status: state, reason, observation anchor, engine fault, clock stats, "
        "dropped feed updates, downtime backfill, previous run, live bar since start",
        "why": "runtime state of the running process, not recorded as events",
    },
    {
        "metric": "paper status: session audit log",
        "why": "kept in paper_sessions.jsonl, outside the hash-chained event store",
    },
    {
        "metric": "freshness label of book values (SIMULATED vs STALE)",
        "why": "depends on the wall clock and the runtime state at read time",
    },
    {
        "metric": "reference strategy: CONFIRMED intents, WATCH proposals, history restarts, "
        "warm-up per instrument, recent reference signals, decisions-from time",
        "why": "per-run in-memory counters of the strategy adapter; WATCH proposals are never "
        "journaled and run boundaries are not in the paper journal",
    },
    {
        "metric": "books: no_trade_deferrals (served by the API, not shown by the page)",
        "why": "a simulator counter with no event of its own; reproducing it means re-running "
        "the fill rules",
    },
    {
        "metric": "display windows (last 100/200 fills, trades, rejections; last 200 signals)",
        "why": "the scorer returns every record; the dashboard lists are tails of these lists",
    },
)

#: EVALUATION_PROTOCOL §5 metrics: the dashboard does not show them, so this scorer does not
#: compute them (no invented metrics); what each needs is stated for the protocol agreement.
PROTOCOL_METRICS_NOT_SCORED: tuple[dict[str, str], ...] = (
    {"metric": "net return, max drawdown per book", "status": "inputs exported (equity marks)"},
    {
        "metric": "win rate (with n), profit factor, expectancy (R and %)",
        "status": "inputs exported (closed trades); CI method (block bootstrap) to be agreed",
    },
    {"metric": "Sharpe / Sortino", "status": "inputs exported; annualization to be agreed"},
    {
        "metric": "signal frequency, abstention rate",
        "status": "signals exported; abstention needs an agreed definition of an opportunity",
    },
    {
        "metric": "signal latency (bar close -> persisted)",
        "status": "decision_time_ms, created_at_ms and release_latency_ms exported",
    },
    {
        "metric": "risk-limit violations",
        "status": "request rejections exported; a violation "
        "(an accepted entry that broke a limit) needs the agreed limit set",
    },
    {"metric": "by-regime performance", "status": "not derivable: no regime label is recorded"},
    {"metric": "exposure-adjusted return", "status": "inputs exported (positions, marks)"},
    {"metric": "matched view against the other system", "status": "needs the other export"},
)

_D = Context(prec=40, rounding=ROUND_HALF_EVEN)
_Q8 = Decimal("0.00000001")
_ZERO = Decimal(0)


# ===================================================================== canonical form and hashes
def canonical_json(obj: Any) -> str:
    """Sorted keys, no whitespace, UTF-8 (not ASCII-escaped), NaN and Infinity forbidden."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chain_hash(prev_hash: str, body: Mapping[str, Any]) -> str:
    """ADR-0011: SHA-256(prev_hash || "\\n" || canonical_json(body))."""
    digest = hashlib.sha256()
    digest.update(prev_hash.encode("ascii"))
    digest.update(b"\n")
    digest.update(canonical_json(body).encode("utf-8"))
    return digest.hexdigest()


def event_body(event: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("seq", "ts_ms", "type", "aggregate_type", "aggregate_id", "payload", "schema_version")
    return {k: event[k] for k in keys}


def line_hash(line: Mapping[str, Any]) -> str:
    return sha256_text(canonical_json({k: v for k, v in line.items() if k != "line_hash"}))


def to_iso(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


# ===================================================================== minimal JSON Schema
class SchemaSet:
    """The JSON Schema 2020-12 keywords these schemas use: type, enum, const, pattern,
    minLength, minimum, required, properties, additionalProperties, items, minItems, maxItems,
    $ref (same file or a sibling file), allOf, anyOf, oneOf, if/then. Unknown keywords are
    annotations (title, description, $id, $schema, $defs)."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or SCHEMA_DIR
        self.schemas: dict[str, dict[str, Any]] = {}
        for path in sorted(self.directory.glob("*.schema.json")):
            self.schemas[path.name] = json.loads(path.read_text(encoding="utf-8"))
        if not self.schemas:
            raise FileNotFoundError(f"no *.schema.json in {self.directory}")

    def digests(self) -> dict[str, str]:
        return {
            name: hashlib.sha256((self.directory / name).read_bytes()).hexdigest()
            for name in sorted(self.schemas)
        }

    def validate(self, instance: Any, name: str) -> list[str]:
        errors: list[str] = []
        self._check(instance, self.schemas[name], name, "$", errors)
        return errors

    def _resolve(self, ref: str, base: str) -> tuple[dict[str, Any], str]:
        file_part, _, pointer = ref.partition("#")
        name = file_part.rsplit("/", 1)[-1] if file_part else base
        node: Any = self.schemas[name]
        for token in [t for t in pointer.split("/") if t]:
            node = node[token.replace("~1", "/").replace("~0", "~")]
        return node, name

    def _check(self, inst: Any, schema: Any, base: str, path: str, errors: list[str]) -> None:
        if schema is True:
            return
        if schema is False:
            errors.append(f"{path}: not allowed")
            return
        if "$ref" in schema:
            target, target_base = self._resolve(str(schema["$ref"]), base)
            self._check(inst, target, target_base, path, errors)
        if "type" in schema:
            wanted = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
            if not any(_is_type(inst, t) for t in wanted):
                errors.append(f"{path}: expected {'/'.join(wanted)}, got {_type_name(inst)}")
                return
        if "const" in schema and not _json_equal(inst, schema["const"]):
            errors.append(f"{path}: expected {schema['const']!r}")
        if "enum" in schema and not any(_json_equal(inst, v) for v in schema["enum"]):
            errors.append(f"{path}: {inst!r} not in {schema['enum']!r}")
        if isinstance(inst, str):
            if "pattern" in schema:
                import re

                if re.search(schema["pattern"], inst) is None:
                    errors.append(f"{path}: {inst!r} does not match {schema['pattern']}")
            if len(inst) < int(schema.get("minLength", 0)):
                errors.append(f"{path}: shorter than {schema['minLength']}")
        if _is_type(inst, "number") and "minimum" in schema and inst < schema["minimum"]:
            errors.append(f"{path}: {inst} < {schema['minimum']}")
        if isinstance(inst, dict):
            for key in schema.get("required", ()):
                if key not in inst:
                    errors.append(f"{path}: missing {key!r}")
            props: dict[str, Any] = schema.get("properties", {})
            extra = schema.get("additionalProperties", True)
            for key, value in inst.items():
                if key in props:
                    self._check(value, props[key], base, f"{path}.{key}", errors)
                elif extra is False:
                    errors.append(f"{path}: unexpected property {key!r}")
                elif isinstance(extra, dict):
                    self._check(value, extra, base, f"{path}.{key}", errors)
        if isinstance(inst, list):
            if len(inst) < int(schema.get("minItems", 0)):
                errors.append(f"{path}: fewer than {schema['minItems']} items")
            if "maxItems" in schema and len(inst) > int(schema["maxItems"]):
                errors.append(f"{path}: more than {schema['maxItems']} items")
            if "items" in schema:
                for i, item in enumerate(inst):
                    self._check(item, schema["items"], base, f"{path}[{i}]", errors)
        for sub in schema.get("allOf", ()):
            self._check(inst, sub, base, path, errors)
        if "anyOf" in schema and not any(self._ok(inst, s, base) for s in schema["anyOf"]):
            errors.append(f"{path}: matches none of anyOf")
        if "oneOf" in schema:
            n = sum(1 for s in schema["oneOf"] if self._ok(inst, s, base))
            if n != 1:
                errors.append(f"{path}: matches {n} of oneOf (expected exactly 1)")
        if "if" in schema and self._ok(inst, schema["if"], base) and "then" in schema:
            self._check(inst, schema["then"], base, path, errors)

    def _ok(self, inst: Any, schema: Any, base: str) -> bool:
        errors: list[str] = []
        self._check(inst, schema, base, "$", errors)
        return not errors


def _is_type(inst: Any, name: str) -> bool:
    if name == "object":
        return isinstance(inst, dict)
    if name == "array":
        return isinstance(inst, list)
    if name == "string":
        return isinstance(inst, str)
    if name == "boolean":
        return isinstance(inst, bool)
    if name == "null":
        return inst is None
    if name == "integer":
        return isinstance(inst, int) and not isinstance(inst, bool)
    if name == "number":
        return isinstance(inst, int | float) and not isinstance(inst, bool)
    raise ValueError(f"unknown JSON Schema type {name!r}")


def _type_name(inst: Any) -> str:
    for name in ("null", "boolean", "integer", "number", "string", "array", "object"):
        if _is_type(inst, name):
            return name
    return type(inst).__name__


def _json_equal(a: Any, b: Any) -> bool:
    return isinstance(a, bool) == isinstance(b, bool) and a == b


# ===================================================================== loading and checking
@dataclass(frozen=True)
class Line:
    file: str
    raw: dict[str, Any]

    @property
    def type(self) -> str:
        return str(self.raw["record_type"])

    @property
    def event(self) -> dict[str, Any]:
        out: dict[str, Any] = self.raw["event"]
        return out

    @property
    def seq(self) -> int:
        return int(self.event["seq"])

    @property
    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = self.event["payload"]
        return out

    @property
    def session(self) -> str | None:
        value = self.raw.get("paper_session_id")
        return None if value is None else str(value)

    @property
    def provenance(self) -> str:
        return str(self.raw["provenance"])


@dataclass
class Export:
    directory: Path
    manifest: dict[str, Any]
    lines: dict[str, list[Line]] = field(default_factory=dict)

    def all_lines(self) -> list[Line]:
        """Every line of every file, in source event order."""
        return sorted((ln for lines in self.lines.values() for ln in lines), key=lambda x: x.seq)


@dataclass
class CheckReport:
    ok: bool
    problems: list[str]
    lines: int
    chain_head: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "problems": self.problems,
            "lines": self.lines,
            "chain_head": self.chain_head,
        }


class ExportInvalid(ValueError):
    """The export failed `check_export` (the scorer refuses to score it: fail closed)."""

    def __init__(self, report: CheckReport) -> None:
        super().__init__("; ".join(report.problems[:5]) or "invalid export")
        self.report = report


MAX_PROBLEMS = 200


def check_export(
    directory: Path | str, schemas: SchemaSet | None = None
) -> tuple[CheckReport, Export | None]:
    """Structural verification of an export directory from its files alone."""
    root = Path(directory)
    schemas = schemas or SchemaSet()
    problems: list[str] = []

    def problem(text: str) -> None:
        if len(problems) < MAX_PROBLEMS:
            problems.append(text)

    manifest_path = root / MANIFEST
    if not manifest_path.is_file():
        return CheckReport(False, [f"{MANIFEST} is missing"], 0, None), None
    manifest_raw = manifest_path.read_bytes().decode("utf-8")
    try:
        manifest = json.loads(manifest_raw)
    except json.JSONDecodeError as exc:
        return CheckReport(False, [f"{MANIFEST}: not JSON ({exc})"], 0, None), None
    for err in schemas.validate(manifest, "manifest.schema.json"):
        problem(f"{MANIFEST}: {err}")
    if problems:
        return CheckReport(False, problems, 0, None), None
    if manifest_raw != canonical_json(manifest) + "\n":
        problem(f"{MANIFEST}: not in canonical form")
    head = manifest["chain_head"]
    export = Export(root, manifest)
    files = manifest["files"]
    if sorted(files) != sorted(FILES):
        problem(f"{MANIFEST}: files {sorted(files)} != {sorted(FILES)}")
    total = 0
    for name in sorted(FILES):
        path = root / name
        if not path.is_file():
            problem(f"{name}: missing")
            continue
        data = path.read_bytes()
        expected = files.get(name, {})
        if hashlib.sha256(data).hexdigest() != expected.get("sha256"):
            problem(f"{name}: sha256 does not match the manifest")
        text = data.decode("utf-8")
        raw_lines = text.split("\n")
        if raw_lines and raw_lines[-1] == "":
            raw_lines.pop()
        elif raw_lines:
            problem(f"{name}: last line is not terminated")
        if len(raw_lines) != expected.get("lines"):
            problem(f"{name}: {len(raw_lines)} lines, manifest says {expected.get('lines')}")
        schema_name = name.replace(".jsonl", ".schema.json")
        prev = GENESIS
        last_seq = 0
        parsed: list[Line] = []
        for n, raw in enumerate(raw_lines, 1):
            where = f"{name}:{n}"
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as exc:
                problem(f"{where}: not JSON ({exc})")
                continue
            if not isinstance(obj, dict):
                problem(f"{where}: not an object")
                continue
            errors = schemas.validate(obj, schema_name)
            if errors:
                problem(f"{where}: schema: {'; '.join(errors[:3])}")
                continue
            if raw != canonical_json(obj):
                problem(f"{where}: not in canonical form")
            if obj["line_no"] != n:
                problem(f"{where}: line_no {obj['line_no']} (expected {n})")
            if obj["prev_line_hash"] != prev:
                problem(f"{where}: prev_line_hash does not link to the previous line")
            if line_hash(obj) != obj["line_hash"]:
                problem(f"{where}: line_hash mismatch (content changed)")
            prev = obj["line_hash"]
            ev = obj["event"]
            if obj["record_type"] != ev["type"] or FILE_OF.get(ev["type"]) != name:
                problem(f"{where}: record type {obj['record_type']!r} does not belong here")
            if chain_hash(ev["prev_hash"], event_body(ev)) != ev["hash"]:
                problem(f"{where}: source event hash does not match its content (seq {ev['seq']})")
            if ev["seq"] <= last_seq:
                problem(f"{where}: event seq {ev['seq']} not increasing")
            last_seq = ev["seq"]
            if obj["chain_head"] != head:
                problem(f"{where}: chain_head differs from the manifest")
            if ev["seq"] > head["seq"]:
                problem(f"{where}: event seq {ev['seq']} beyond the chain head {head['seq']}")
            parsed.append(Line(name, obj))
        if parsed and expected.get("last_line_hash") != prev:
            problem(f"{name}: last line hash does not match the manifest")
        if not parsed and expected.get("last_line_hash") is not None:
            problem(f"{name}: manifest names a last line, file has none")
        export.lines[name] = parsed
        total += len(parsed)
    _check_cross_file(export, head, problem)
    return CheckReport(not problems, problems, total, head), (None if problems else export)


def _check_cross_file(export: Export, head: Mapping[str, Any], problem: Any) -> None:
    lines = export.all_lines()
    by_seq: dict[int, Line] = {}
    for ln in lines:
        if ln.seq in by_seq:
            problem(f"event seq {ln.seq} exported twice ({by_seq[ln.seq].file}, {ln.file})")
        by_seq[ln.seq] = ln
    for ln in lines:
        nxt = by_seq.get(ln.seq + 1)
        if nxt is not None and nxt.event["prev_hash"] != ln.event["hash"]:
            problem(f"seq {ln.seq + 1}: prev_hash does not link to seq {ln.seq}")
    top = by_seq.get(int(head["seq"]))
    if top is not None and top.event["hash"] != head["hash"]:
        problem("the exported event at the chain head seq has a different hash")
    members: dict[int, list[int]] = {}
    for ln in lines:
        batch = ln.raw.get("batch_seq")
        if batch is not None:
            members.setdefault(int(batch), []).append(ln.seq)
    for ln in lines:
        if ln.type != "paper_step":
            continue
        size = ln.payload.get("batch_size")
        if not isinstance(size, int) or size < 1:
            problem(f"step seq {ln.seq}: invalid batch_size")
            continue
        got = sorted(members.get(ln.seq, []))
        want = list(range(ln.seq, ln.seq + size))
        if got != want:
            missing = sorted(set(want) - set(got))
            problem(f"paper batch at seq {ln.seq}: lines for seq {missing[:5]} missing or extra")
        for s in got:
            if by_seq[s].session != ln.session:
                problem(f"seq {s}: paper_session_id differs from its batch")
    for batch in members:
        if batch not in by_seq or by_seq[batch].type != "paper_step":
            problem(f"batch_seq {batch} names no exported paper step")


def load_export(directory: Path | str, schemas: SchemaSet | None = None) -> Export:
    report, export = check_export(directory, schemas)
    if export is None:
        raise ExportInvalid(report)
    return export


# ===================================================================== scoring
def _dec(text: Any) -> Decimal:
    if isinstance(text, bool) or text is None:
        raise ValueError(f"not an amount: {text!r}")
    if isinstance(text, float):
        return Decimal(repr(text))
    try:
        return Decimal(str(text))
    except InvalidOperation as exc:
        raise ValueError(f"not an amount: {text!r}") from exc


def _q8(value: Decimal) -> Decimal:
    return value.quantize(_Q8, rounding=ROUND_HALF_EVEN)


def _s(value: Decimal) -> str:
    return format(value, "f")


ASSET_PREFIXES = ("position:", "margin:")
EQUITY_ACCOUNTS = ("capital", "realized_pnl", "fees", "funding")


@dataclass
class _Position:
    market: str
    side: str
    qty: Decimal
    notional: Decimal


@dataclass
class _Book:
    name: str
    starting_equity: Decimal
    ledger: list[dict[str, Any]] = field(default_factory=list)
    positions: dict[tuple[str, str], _Position] = field(default_factory=dict)
    trades: list[dict[str, Any]] = field(default_factory=list)
    rejections: dict[str, int] = field(default_factory=dict)
    halted: str | None = None
    curve: list[tuple[int, Decimal]] = field(default_factory=list)
    balances: dict[str, Decimal] = field(default_factory=dict)

    @property
    def cash(self) -> Decimal:
        return self.balances.get("cash", _ZERO)

    def value(
        self, key: tuple[str, str], pos: _Position, marks: Mapping[tuple[str, str], Decimal]
    ) -> Decimal:
        """Asset value at the last mark: spot inventory, USD-M margin plus unrealized P&L."""
        marked = pos.qty * marks[key]
        if pos.market == "spot" or pos.side == "long":
            return marked
        return 2 * pos.notional - marked

    def equity(self, marks: Mapping[tuple[str, str], Decimal]) -> Decimal:
        with localcontext(_D):
            total = self.cash + sum(
                (self.value(k, p, marks) for k, p in self.positions.items()), _ZERO
            )
            return _q8(total)

    def unrealized(self, marks: Mapping[tuple[str, str], Decimal]) -> Decimal:
        with localcontext(_D):
            return sum(
                (
                    (1 if p.side == "long" else -1) * (p.qty * marks[k] - p.notional)
                    for k, p in self.positions.items()
                ),
                _ZERO,
            )

    def reconcile(self) -> tuple[bool, Decimal]:
        """Double-entry checks of the exported ledger (every transaction balances, assets equal
        equity from entries, transaction ids consecutive); returns (ok, equity at cost)."""
        ok = True
        replay: dict[str, Decimal] = {}
        for i, tx in enumerate(self.ledger, start=1):
            if tx["tx_id"] != i:
                ok = False
            total = _ZERO
            for account, amount in tx["legs"]:
                value = _dec(amount)
                total += value
                replay[account] = replay.get(account, _ZERO) + value
            if total != _ZERO:
                ok = False
        assets = sum(
            (v for a, v in replay.items() if a == "cash" or a.startswith(ASSET_PREFIXES)), _ZERO
        )
        equity = -sum((replay.get(a, _ZERO) for a in EQUITY_ACCOUNTS), _ZERO)
        if assets != equity or sum(replay.values(), _ZERO) != _ZERO:
            ok = False
        return ok, equity

    def ledger_hash(self) -> str:
        return sha256_text(canonical_json(self.ledger))


def drawdown(points: Sequence[tuple[int, Decimal]]) -> dict[str, Any]:
    """Max drawdown as a fraction of the running peak (1e-8), its peak and trough times and the
    longest time spent below a previous peak (the simulator's drawdown rules)."""
    max_dd = _q8(_ZERO)
    peak_ts: int | None = None
    trough_ts: int | None = None
    running_peak_ts: int | None = None
    peak: Decimal | None = None
    dd_peak: Decimal | None = None
    under_since: int | None = None
    longest = 0
    with localcontext(_D):
        for ts, eq in points:
            dd_peak = eq if dd_peak is None or eq > dd_peak else dd_peak
            d = _q8((dd_peak - eq) / dd_peak) if dd_peak > 0 else _ZERO
            if peak is None or eq >= peak:
                peak = eq
                running_peak_ts = ts
                if under_since is not None:
                    longest = max(longest, ts - under_since)
                    under_since = None
            elif under_since is None:
                under_since = running_peak_ts
            if d > max_dd:
                max_dd, peak_ts, trough_ts = d, running_peak_ts, ts
    if under_since is not None and points:
        longest = max(longest, points[-1][0] - under_since)
    return {
        "max_drawdown": _s(max_dd),
        "max_drawdown_peak_ts_ms": peak_ts,
        "max_drawdown_trough_ts_ms": trough_ts,
        "longest_drawdown_ms": longest,
        "equity_points": len(points),
    }


def _sim_record(ln: Line) -> dict[str, Any]:
    """A simulator event as the dashboard lists it: time, aggregate and the payload without the
    journal's ``sim_seq``."""
    payload = {k: v for k, v in ln.payload.items() if k != "sim_seq"}
    return {"ts_ms": ln.event["ts_ms"], "aggregate_id": ln.event["aggregate_id"], **payload}


MINUTE_MS = 60_000


def score_session(lines: Iterable[Line]) -> dict[str, Any]:
    """Replay one paper session's exported records in event order."""
    books: dict[str, _Book] = {}
    marks: dict[tuple[str, str], Decimal] = {}
    minute: int | None = None
    fills: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    steps = observed = unobserved = 0
    last_minute: int | None = None
    provenance: set[str] = set()

    def flush(open_ms: int) -> None:
        for book in books.values():
            book.curve.append((open_ms + MINUTE_MS - 1, book.equity(marks)))

    for ln in lines:
        provenance.add(ln.provenance)
        p = ln.payload
        t = ln.type
        if t == "paper_step":
            steps += 1
            if p["kind"] != "bar":
                continue
            bar = p["bar"]
            if minute is not None and bar["open_time_ms"] != minute:
                flush(minute)
            minute = int(bar["open_time_ms"])
            last_minute = minute
            marks[(str(bar["market"]), str(bar["symbol"]))] = _dec(bar["close"])
            if p["observed"]:
                observed += 1
            else:
                unobserved += 1
        elif t == "sim_run_started":
            for desc in p["books"]:
                name = str(desc["book"])
                book = _Book(name, _dec(desc["starting_equity"]))
                book.curve.append((int(ln.event["ts_ms"]), _q8(book.starting_equity)))
                books[name] = book
        elif t == "sim_ledger_posted":
            book = books[str(p["book"])]
            tx = {"tx_id": p["tx_id"], "ts_ms": p["ts_ms"], "memo": p["memo"], "legs": p["legs"]}
            book.ledger.append(tx)
            for account, amount in p["legs"]:
                book.balances[account] = book.balances.get(account, _ZERO) + _dec(amount)
        elif t == "sim_position_opened":
            key = (str(p["market"]), str(p["symbol"]))
            books[str(p["book"])].positions[key] = _Position(
                key[0], str(p["side"]), _dec(p["qty"]), _dec(p["notional"])
            )
        elif t == "sim_position_closed":
            book = books[str(p["book"])]
            book.positions.pop((str(p["market"]), str(p["symbol"])), None)
            book.trades.append({k: v for k, v in p.items() if k != "sim_seq"})
        elif t == "sim_order_filled":
            fills.append(_sim_record(ln))
        elif t == "sim_request_rejected":
            rejections.append(_sim_record(ln))
        elif t == "sim_order_rejected":
            book = books[str(p["book"])]
            reason = str(p["reason"])
            book.rejections[reason] = book.rejections.get(reason, 0) + 1
        elif t == "sim_book_halted":
            book = books[str(p["book"])]
            if book.halted is None:
                book.halted = str(p["reason"])
        elif t == "sim_book_reset":
            books[str(p["book"])].halted = None

    out_books: dict[str, Any] = {}
    for name, book in books.items():
        ok, at_cost = book.reconcile()
        trades = book.trades
        out_books[name] = {
            "equity": _s(book.equity(marks)),
            "cash": _s(book.cash),
            "unrealized_pnl": _s(book.unrealized(marks)),
            "open_positions": len(book.positions),
            "positions": {
                f"{m}:{s}": _s((1 if pos.side == "long" else -1) * pos.qty)
                for (m, s), pos in sorted(book.positions.items())
            },
            "halted": book.halted,
            "ledger_reconciled": ok,
            "ledger_equity_at_cost": _s(at_cost),
            "ledger_transactions": len(book.ledger),
            "ledger_hash": book.ledger_hash(),
            "trades": len(trades),
            "ambiguous_trades": sum(1 for x in trades if x.get("ambiguous_bar")),
            "gap_trades": sum(1 for x in trades if x.get("spans_gap")),
            "flagged_trades": sum(
                1 for x in trades if x.get("ambiguous_bar") or x.get("spans_gap")
            ),
            "deferred_no_trade_trades": sum(1 for x in trades if x.get("deferred_no_trade")),
            "exchange_downtime_trades": sum(1 for x in trades if x.get("spans_exchange_downtime")),
            "rejections_by_reason": dict(sorted(book.rejections.items())),
            **drawdown(book.curve),
        }
    all_trades = [t for b in books.values() for t in b.trades]
    return {
        "provenance": "SIMULATED",
        "input_provenance": sorted(provenance),
        "batches": steps,
        "bar_steps": {"observed": observed, "unobserved": unobserved},
        "last_minute_ms": last_minute,
        "values_as_of_ms": None if last_minute is None else last_minute + MINUTE_MS - 1,
        "books": out_books,
        "fills": fills,
        "trades": sorted(all_trades, key=lambda x: (x["exit_bar_open_ms"], x["book"])),
        "request_rejections": rejections,
    }


def demo_signals(export: Export) -> list[dict[str, Any]]:
    """The Signals page list (`/api/signals`): demo-detector WATCH events, newest first."""
    out: list[dict[str, Any]] = []
    for ln in reversed(export.lines.get("signals.jsonl", [])):
        if ln.provenance != "DEMO":
            continue
        p = ln.payload
        out.append(
            {
                "signal_id": str(p["signal_id"]),
                "seq": ln.seq,
                "created_at": to_iso(int(p["created_at_ms"])),
                "decision_time": to_iso(int(p["decision_time_ms"])),
                "market": str(p["market"]),
                "symbol": str(p["symbol"]),
                "timeframe": str(p["timeframe"]),
                "side": str(p["side"]),
                "stage": str(p["stage"]),
                "ref_price": float(p["ref_price"]),
                "move": float(p.get("move", 0.0)),
                "threshold": float(p.get("threshold", 0.0)),
                "strategy_id": str(p["strategy_id"]),
                "evidence_grade": str(p["evidence_grade"]),
                "run_mode": str(p["run_mode"]),
                "rationale": str(p.get("rationale", "")),
            }
        )
    return out


def score(export: Export) -> dict[str, Any]:
    by_session: dict[str, list[Line]] = {}
    for ln in export.all_lines():
        if ln.session is not None:
            by_session.setdefault(ln.session, []).append(ln)
    return {
        "scorer_version": SCORER_VERSION,
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "chain_head": export.manifest["chain_head"],
        "paper_sessions": {sid: score_session(ls) for sid, ls in sorted(by_session.items())},
        "demo_signals": {"provenance": "DEMO", "items": demo_signals(export)},
        "not_derivable": list(NOT_DERIVABLE),
        "protocol_metrics_not_scored": list(PROTOCOL_METRICS_NOT_SCORED),
    }


def score_directory(directory: Path | str, schemas: SchemaSet | None = None) -> dict[str, Any]:
    """Check, then score (refuses an invalid export with `ExportInvalid`)."""
    return score(load_export(directory, schemas))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score a tsc-eval v1 export (stdlib only).")
    parser.add_argument("export", help="export directory (manifest.json and *.jsonl)")
    parser.add_argument("--schemas", default=None, help="schema directory (default: bundled)")
    parser.add_argument("--check-only", action="store_true", help="verify, do not score")
    args = parser.parse_args(argv)
    schemas = SchemaSet(Path(args.schemas) if args.schemas else None)
    report, export = check_export(args.export, schemas)
    if export is None:
        print(json.dumps({"check": report.to_dict()}, indent=2, sort_keys=True))
        return 2
    if args.check_only:
        print(json.dumps({"check": report.to_dict()}, indent=2, sort_keys=True))
        return 0
    print(json.dumps({"check": report.to_dict(), "score": score(export)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
