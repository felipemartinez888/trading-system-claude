"""Walk-forward progress ledger and verified reuse of recorded runs (audit WF-4: resumability).

The owner's PC can restart in the middle of a long walk-forward. Every evaluation is a normal
recorded run, so a restarted walk-forward does not need to repeat the ones that completed; it
must only be sure that what it reuses is exactly what it would have produced.

**Ledger** (``progress.jsonl`` in the walk-forward's directory): a header naming the plan id, the
plan values fixed at its start (written before any run), then one line per completed
evaluation: phase (``train``, ``test``, ``final``), fold, configuration, run id, trial key and
the SHA-256 of the run's metrics, trades and equity files. A line is
appended (and flushed to disk) only after the run's artifacts are written. A ledger belongs to
one plan: the plan id hashes everything that decides the runs (strategy, grid, symbols, folds,
configuration, cost model, interruption registry, preregistration, dataset pins, engine versions,
the code commit and the exact lake parts), so a ledger of another plan is refused.

**Fail closed.** A complete line that is not a valid entry, a duplicate entry, or a header of
another plan refuses the resume. Only an unterminated final line (a crash while appending) is
ignored; the next append rewrites the file without it (atomically). A run that completed but whose
line was never written is simply run again: it records the same trial key, so the trial count is
unchanged and only its evaluation counter grows.

**Verified reuse** (`verify_reused_run`): the run directory must exist inside the runs directory;
the manifest must re-hash to its ``manifest_hash``; the metrics, trades and equity files must hash
to both the manifest's and the ledger's values; the run's trial key must equal the key the
requested evaluation would record (`runner.trial_identity`: strategy, version, parameters,
symbols, window, segment, configuration, cost model); its preregistration id and code commit must
match the plan. Dataset pins and the lake parts are bound by the plan id (a ledger only serves its
own plan). Any mismatch refuses the resume (`ResumeRefused`) instead of silently re-running or
trusting it.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tsclaude.contracts.events import canonical_json

LEDGER_VERSION = "wf-progress-v1"
PHASES = ("train", "test", "final")


class ResumeRefused(ValueError):
    """The recorded progress cannot be trusted; nothing was reused."""


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


EntryKey = tuple[str, int, str]


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    phase: str
    fold: int
    config_id: str
    run_id: str
    trial_key: str
    metrics_sha256: str
    trades_sha256: str
    equity_sha256: str

    @property
    def key(self) -> EntryKey:
        return (self.phase, self.fold, self.config_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "run",
            "phase": self.phase,
            "fold": self.fold,
            "config_id": self.config_id,
            "run_id": self.run_id,
            "trial_key": self.trial_key,
            "metrics_sha256": self.metrics_sha256,
            "trades_sha256": self.trades_sha256,
            "equity_sha256": self.equity_sha256,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> LedgerEntry:
        if d.get("kind") != "run" or d.get("phase") not in PHASES:
            raise ValueError("not a run entry")
        fold = d.get("fold")
        if not isinstance(fold, int) or isinstance(fold, bool):
            raise ValueError("fold must be an integer")
        fields = (
            "config_id",
            "run_id",
            "trial_key",
            "metrics_sha256",
            "trades_sha256",
            "equity_sha256",
        )
        values = [d.get(f) for f in fields]
        if not all(isinstance(v, str) and v for v in values):
            raise ValueError("an entry field is missing")
        return cls(str(d["phase"]), fold, *(str(v) for v in values))


class ProgressLedger:
    """The progress of one plan. ``meta`` holds values fixed when the plan started (the trial
    count used to rescore every fold), written with the header before any run, so a resumed
    walk-forward uses exactly what the uninterrupted one used."""

    def __init__(
        self, path: Path | str, plan_id: str, meta: Mapping[str, Any] | None = None
    ) -> None:
        self.path = Path(path)
        self.plan_id = plan_id
        self._entries: dict[EntryKey, LedgerEntry] = {}
        self._repair = False
        self.ignored_partial_line = False
        self.meta: dict[str, Any] | None = None
        self._load()
        if self.meta is None:
            if self._entries:
                raise ResumeRefused(f"{self.path} has entries but no plan values")
            if meta is None:
                raise ResumeRefused(f"{self.path} has no plan values and none were given")
            self.meta = json.loads(canonical_json(dict(meta)))
            self._rewrite([])  # header and plan values on disk before the first run

    def _header(self) -> dict[str, Any]:
        return {"kind": "plan", "version": LEDGER_VERSION, "plan_id": self.plan_id}

    def _rewrite(self, extra: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = [
            canonical_json(self._header()),
            canonical_json({"kind": "meta", **(self.meta or {})}),
        ]
        body += [canonical_json(e.to_dict()) for e in self._entries.values()]
        body += extra
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(body) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def _load(self) -> None:
        if not self.path.exists():
            return
        data = self.path.read_bytes()
        lines = data.split(b"\n")
        tail = lines.pop()  # b"" when the file ends with a line feed
        if tail:
            self.ignored_partial_line = True  # a crash while appending: ignored, then repaired
            self._repair = True
        if not lines:
            raise ResumeRefused(f"{self.path} has no complete header")
        for n, raw in enumerate(lines, start=1):
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise ResumeRefused(f"{self.path} line {n} is not valid JSON") from exc
            if n == 1:
                if obj != self._header():
                    raise ResumeRefused(
                        f"{self.path} belongs to another walk-forward plan "
                        f"({obj.get('plan_id') if isinstance(obj, dict) else '?'}), not "
                        f"{self.plan_id}"
                    )
                continue
            if n == 2:
                if not isinstance(obj, dict) or obj.get("kind") != "meta":
                    raise ResumeRefused(f"{self.path} line 2 is not the plan values")
                self.meta = {k: v for k, v in obj.items() if k != "kind"}
                continue
            try:
                entry = LedgerEntry.from_dict(obj)
            except (ValueError, AttributeError, TypeError) as exc:
                raise ResumeRefused(f"{self.path} line {n} is not a valid entry: {exc}") from exc
            if entry.key in self._entries:
                raise ResumeRefused(f"{self.path} line {n} repeats {entry.key}")
            self._entries[entry.key] = entry

    @property
    def entries(self) -> dict[EntryKey, LedgerEntry]:
        return dict(self._entries)

    def get(self, phase: str, fold: int, config_id: str) -> LedgerEntry | None:
        return self._entries.get((phase, fold, config_id))

    def append(self, entry: LedgerEntry) -> None:
        if entry.key in self._entries:
            raise ResumeRefused(f"{entry.key} is already recorded")
        line = canonical_json(entry.to_dict()) + "\n"
        if self._repair or not self.path.exists():
            self._rewrite([line.rstrip("\n")])
            self._repair = False
        else:
            with self.path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
        self._entries[entry.key] = entry


def entry_for(
    phase: str, fold: int, config_id: str, run_dir: Path, manifest: Mapping[str, Any]
) -> LedgerEntry:
    outputs = manifest.get("outputs") or {}
    return LedgerEntry(
        phase=phase,
        fold=fold,
        config_id=config_id,
        run_id=str(manifest["run_id"]),
        trial_key=str((manifest.get("trials") or {}).get("trial_key")),
        metrics_sha256=str(outputs.get("metrics_sha256")),
        trades_sha256=str(outputs.get("trades_parquet_sha256")),
        equity_sha256=str(outputs.get("equity_parquet_sha256")),
    )


@dataclass(frozen=True)
class ReuseExpectation:
    """What the requested evaluation would record; a reused run must match all of it."""

    trial_key: str
    from_ms: int
    to_ms: int
    prereg_id: str | None
    code_commit: str


def verify_reused_run(
    runs_dir: Path, entry: LedgerEntry, expect: ReuseExpectation
) -> tuple[Path, dict[str, Any], dict[str, Any], str]:
    """``(run_dir, manifest, metrics, metrics_sha256)`` of a verified run, or `ResumeRefused`."""
    from tsclaude.research.runner import manifest_hash_of

    def refuse(why: str) -> ResumeRefused:
        return ResumeRefused(f"recorded run {entry.run_id} ({entry.key}) cannot be reused: {why}")

    root = Path(runs_dir).resolve()
    run_dir = (root / entry.run_id).resolve()
    if run_dir.parent != root or not run_dir.is_dir():
        raise refuse("its directory is missing")
    try:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        metrics_text = (run_dir / "metrics.json").read_text(encoding="utf-8").rstrip("\n")
        metrics = json.loads(metrics_text)
    except (OSError, ValueError) as exc:
        raise refuse(f"its manifest or metrics cannot be read ({exc})") from exc
    outputs = manifest.get("outputs") or {}
    if manifest.get("run_id") != entry.run_id:
        raise refuse("the manifest names another run")
    if manifest_hash_of(manifest) != manifest.get("manifest_hash"):
        raise refuse("the manifest does not match its own hash")
    metrics_sha = hashlib.sha256(metrics_text.encode("utf-8")).hexdigest()
    if not metrics_sha == outputs.get("metrics_sha256") == entry.metrics_sha256:
        raise refuse("the metrics file does not match the manifest and the ledger")
    if metrics.get("manifest_hash") != manifest.get("manifest_hash"):
        raise refuse("the metrics belong to another manifest")
    for name, key, recorded in (
        ("trades.parquet", "trades_parquet_sha256", entry.trades_sha256),
        ("equity.parquet", "equity_parquet_sha256", entry.equity_sha256),
    ):
        path = run_dir / name
        if not path.is_file() or not file_sha256(path) == outputs.get(key) == recorded:
            raise refuse(f"{name} does not match the manifest and the ledger")
    trials = manifest.get("trials") or {}
    if not trials.get("trial_key") == entry.trial_key == expect.trial_key:
        raise refuse("it is not the evaluation requested (trial key differs)")
    rng = manifest.get("range") or {}
    if (rng.get("from_ms"), rng.get("to_ms")) != (expect.from_ms, expect.to_ms):
        raise refuse("its window differs from the fold's")
    if manifest.get("prereg_id") != expect.prereg_id:
        raise refuse("its preregistration id differs from the plan's")
    code = manifest.get("code") or {}
    if code.get("commit") != expect.code_commit or code.get("dirty") is not False:
        raise refuse("it was produced by other code than the plan's commit")
    return run_dir, manifest, metrics, metrics_sha


__all__ = [
    "LEDGER_VERSION",
    "LedgerEntry",
    "ProgressLedger",
    "ResumeRefused",
    "ReuseExpectation",
    "entry_for",
    "file_sha256",
    "verify_reused_run",
]
