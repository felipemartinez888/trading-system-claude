"""Immutable, content-addressed Parquet lake (DATA_ARCHITECTURE §5).

    <data>/lake/market=<m>/kind=<k>/symbol=<S>/year=<YYYY>/month=<MM>/part-<hash32>.parquet
    <data>/imports/<dataset_id>/manifest.json      (+ qa_report.json)
    <data>/catalog.duckdb                          (derived; rebuilt from the manifests)

- A **part** holds the rows of one (market, kind, symbol, month) produced by one import. Its name
  carries the first 32 hex chars of its **content hash**: SHA-256 over a canonical text form of the
  rows (independent of the Parquet encoder: stable across pyarrow versions and OSes;
  `ingested_at_ms` is excluded). Parts are never modified; writing a part whose content already
  exists reuses the existing file.
- A **dataset** is one import batch for one (market, kind, symbol):
  `dataset_id = SHA-256("\\n".join(sorted(part content hashes)))`. Re-importing the same data yields
  the same id; the first manifest is kept.
- Only parts listed in a committed manifest are visible to readers (a crash between writing parts
  and the manifest leaves invisible files that the next identical import reuses).
- `catalog.duckdb` holds `datasets`/`parts` tables and per-kind views over the Parquet files. It
  is a convenience index for analysis; the manifests are the source of truth.

The file format sits behind `PartCodec` (`store.parquet_codec.ParquetCodec` in production), so the
lake logic can be tested without pyarrow.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

from tsclaude.contracts.market import Market

type ColumnType = Literal["int", "float", "float?", "str"]
type Row = dict[str, Any]

LAKE_FORMAT_VERSION = 1
EXCLUDED_FROM_HASH = frozenset({"ingested_at_ms"})


class LakeKind(StrEnum):
    BARS_1M = "bars_1m"
    MARK_PRICE_1M = "mark_price_1m"
    FUNDING = "funding"
    KLINES_1D = "klines_1d"  # native daily klines; used for the universe rule (volume, listing)


_KLINE_COLUMNS: tuple[tuple[str, ColumnType], ...] = (
    ("open_time_ms", "int"),
    ("close_time_ms", "int"),
    ("open", "float"),
    ("high", "float"),
    ("low", "float"),
    ("close", "float"),
    ("volume", "float"),
    ("quote_volume", "float"),
    ("trades", "int"),
    ("taker_buy_volume", "float"),
    ("taker_buy_quote_volume", "float"),
    ("source", "str"),
    ("ingested_at_ms", "int"),
)

SCHEMAS: dict[LakeKind, tuple[tuple[str, ColumnType], ...]] = {
    LakeKind.BARS_1M: _KLINE_COLUMNS,
    LakeKind.KLINES_1D: _KLINE_COLUMNS,
    LakeKind.MARK_PRICE_1M: (
        ("open_time_ms", "int"),
        ("close_time_ms", "int"),
        ("open", "float"),
        ("high", "float"),
        ("low", "float"),
        ("close", "float"),
        ("source", "str"),
        ("ingested_at_ms", "int"),
    ),
    LakeKind.FUNDING: (
        ("funding_time_ms", "int"),
        ("funding_rate", "float"),
        ("mark_price", "float?"),
        ("source", "str"),
        ("ingested_at_ms", "int"),
    ),
}

TIME_KEY: dict[LakeKind, str] = {
    LakeKind.BARS_1M: "open_time_ms",
    LakeKind.KLINES_1D: "open_time_ms",
    LakeKind.MARK_PRICE_1M: "open_time_ms",
    LakeKind.FUNDING: "funding_time_ms",
}


class LakeError(RuntimeError):
    pass


class PartCodec(Protocol):
    extension: str

    def write(
        self, path: Path, schema: Sequence[tuple[str, ColumnType]], rows: Sequence[Row]
    ) -> None: ...

    def read(self, path: Path, schema: Sequence[tuple[str, ColumnType]]) -> list[Row]: ...


@dataclass(frozen=True, slots=True)
class PartInfo:
    market: str
    kind: str
    symbol: str
    year: int
    month: int
    path: str  # posix, relative to the data dir
    content_hash: str
    file_sha256: str
    rows: int
    first_time_ms: int
    last_time_ms: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> PartInfo:
        return cls(**{k: d[k] for k in cls.__slots__})


@dataclass(frozen=True, slots=True)
class IndexedPart:
    part: PartInfo
    dataset_id: str


def _canon(value: Any, ctype: ColumnType) -> str:
    if value is None:
        if ctype != "float?":
            raise LakeError("null in a non-nullable column")
        return ""
    if ctype in ("float", "float?"):
        return repr(float(value))
    if ctype == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise LakeError(f"expected int, got {type(value).__name__}")
        return str(value)
    return str(value)


def content_hash(market: str, kind: LakeKind, symbol: str, rows: Sequence[Row]) -> str:
    """Canonical, encoder-independent hash of a part's rows (sorted by time key)."""
    schema = [(n, t) for n, t in SCHEMAS[kind] if n not in EXCLUDED_FROM_HASH]
    digest = hashlib.sha256()
    digest.update(f"tsclaude-lake/{LAKE_FORMAT_VERSION}|{market}|{kind}|{symbol}\n".encode())
    digest.update(("|".join(f"{n}:{t}" for n, t in schema) + "\n").encode())
    for row in rows:
        digest.update((",".join(_canon(row[n], t) for n, t in schema) + "\n").encode())
    return digest.hexdigest()


def dataset_id_of(content_hashes: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(sorted(content_hashes)).encode("ascii")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def _year_month(ms: int) -> tuple[int, int]:
    dt = datetime.fromtimestamp(ms / 1000, tz=UTC)
    return dt.year, dt.month


def _write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class Lake:
    def __init__(self, data_dir: Path | str, codec: PartCodec | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.lake_dir = self.data_dir / "lake"
        self.imports_dir = self.data_dir / "imports"
        if codec is None:
            from tsclaude.store.parquet_codec import ParquetCodec  # needs pyarrow

            codec = ParquetCodec()
        self.codec = codec
        self._index: list[IndexedPart] | None = None

    # ------------------------------------------------------------------ writing
    def write_parts(
        self, market: Market | str, kind: LakeKind, symbol: str, rows: Sequence[Row]
    ) -> list[PartInfo]:
        """Split rows by UTC month and write one immutable part per month."""
        key = TIME_KEY[kind]
        by_month: dict[tuple[int, int], list[Row]] = {}
        for row in rows:
            by_month.setdefault(_year_month(int(row[key])), []).append(row)
        parts: list[PartInfo] = []
        for (year, month), month_rows in sorted(by_month.items()):
            parts.append(self._write_part(str(market), kind, symbol, year, month, month_rows))
        return parts

    def _write_part(
        self, market: str, kind: LakeKind, symbol: str, year: int, month: int, rows: list[Row]
    ) -> PartInfo:
        key = TIME_KEY[kind]
        rows = sorted(rows, key=lambda r: int(r[key]))
        times = [int(r[key]) for r in rows]
        if len(set(times)) != len(times):
            raise LakeError(f"duplicate {key} in a part for {market}/{kind}/{symbol}")
        for r in rows:
            missing = [n for n, _ in SCHEMAS[kind] if n not in r]
            if missing:
                raise LakeError(f"row lacks columns {missing}")
        chash = content_hash(market, kind, symbol, rows)
        rel = (
            f"lake/market={market}/kind={kind}/symbol={symbol}/year={year:04d}/month={month:02d}/"
            f"part-{chash[:32]}.{self.codec.extension}"
        )
        path = self.data_dir / rel
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            self.codec.write(tmp, SCHEMAS[kind], rows)
            os.replace(tmp, path)
        return PartInfo(
            market=market,
            kind=str(kind),
            symbol=symbol,
            year=year,
            month=month,
            path=rel,
            content_hash=chash,
            file_sha256=_file_sha256(path),
            rows=len(rows),
            first_time_ms=times[0],
            last_time_ms=times[-1],
        )

    def commit_dataset(
        self, parts: Sequence[PartInfo], meta: Mapping[str, Any], qa: Mapping[str, Any] | None
    ) -> tuple[str, bool]:
        """Write the manifest (and QA report). Returns (dataset_id, created)."""
        if not parts:
            raise LakeError("a dataset needs at least one part")
        dsid = dataset_id_of(p.content_hash for p in parts)
        manifest_path = self.imports_dir / dsid / "manifest.json"
        if manifest_path.exists():
            return dsid, False
        manifest = {
            **dict(meta),
            "dataset_id": dsid,
            "lake_format_version": LAKE_FORMAT_VERSION,
            "parts": [p.to_dict() for p in sorted(parts, key=lambda p: p.path)],
            "row_count": sum(p.rows for p in parts),
            "time_range": {
                "from_ms": min(p.first_time_ms for p in parts),
                "to_ms": max(p.last_time_ms for p in parts),
            },
        }
        if qa is not None:
            manifest["qa_summary"] = qa.get("summary", {})
            _write_json_atomic(self.imports_dir / dsid / "qa_report.json", dict(qa))
        _write_json_atomic(manifest_path, manifest)
        self._index = None
        return dsid, True

    # ------------------------------------------------------------------ reading
    def manifest(self, dataset_id: str) -> dict[str, Any]:
        path = self.imports_dir / dataset_id / "manifest.json"
        if not path.exists():
            raise LakeError(f"unknown dataset {dataset_id}")
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data

    def dataset_ids(self) -> list[str]:
        if not self.imports_dir.exists():
            return []
        return sorted(
            p.parent.name
            for p in self.imports_dir.glob("*/manifest.json")
            if len(p.parent.name) == 64
        )

    def index(self) -> list[IndexedPart]:
        if self._index is None:
            out: list[IndexedPart] = []
            for dsid in self.dataset_ids():
                for d in self.manifest(dsid)["parts"]:
                    out.append(IndexedPart(PartInfo.from_dict(d), dsid))
            self._index = out
        return self._index

    def refresh(self) -> None:
        self._index = None

    def read_part(self, part: PartInfo) -> list[Row]:
        return self.codec.read(self.data_dir / part.path, SCHEMAS[LakeKind(part.kind)])

    # ------------------------------------------------------------------ integrity
    def verify(self, dataset_id: str, raw_cache_dir: Path | None = None) -> dict[str, Any]:
        """Recompute file hashes, content hashes, row counts and the dataset id."""
        errors: list[str] = []
        manifest = self.manifest(dataset_id)
        parts = [PartInfo.from_dict(d) for d in manifest["parts"]]
        if dataset_id_of(p.content_hash for p in parts) != dataset_id:
            errors.append("dataset_id does not match the parts' content hashes")
        for p in parts:
            path = self.data_dir / p.path
            if not path.exists():
                errors.append(f"missing part file {p.path}")
                continue
            if _file_sha256(path) != p.file_sha256:
                errors.append(f"file hash mismatch for {p.path}")
            try:
                rows = self.read_part(p)
            except Exception as exc:
                errors.append(f"unreadable part {p.path}: {exc!r}")
                continue
            if len(rows) != p.rows:
                errors.append(f"row count mismatch for {p.path}")
            if content_hash(p.market, LakeKind(p.kind), p.symbol, rows) != p.content_hash:
                errors.append(f"content hash mismatch for {p.path}")
        sources_checked = 0
        if raw_cache_dir is not None:
            from tsclaude.marketdata.archive.checksum import sha256_file

            for src in manifest.get("sources", []):
                rel = src.get("cache_path")
                if not rel:
                    continue
                path = raw_cache_dir / rel
                if not path.exists():
                    continue
                sources_checked += 1
                if sha256_file(path) != src.get("sha256"):
                    errors.append(f"cached source {rel} no longer matches its checksum")
        return {
            "dataset_id": dataset_id,
            "ok": not errors,
            "parts_checked": len(parts),
            "rows": sum(p.rows for p in parts),
            "sources_checked": sources_checked,
            "errors": errors,
        }

    # ------------------------------------------------------------------ catalog
    def build_catalog(self) -> Path:
        """(Re)build catalog.duckdb from the manifests. Needs duckdb."""
        import duckdb

        path = self.data_dir / "catalog.duckdb"
        tmp = self.data_dir / f".catalog.{os.getpid()}.duckdb"
        if tmp.exists():
            tmp.unlink()
        con = duckdb.connect(str(tmp))
        try:
            con.execute(
                "CREATE TABLE datasets (dataset_id VARCHAR PRIMARY KEY, market VARCHAR,"
                " kind VARCHAR, symbol VARCHAR, row_count BIGINT, from_ms BIGINT, to_ms BIGINT,"
                " manifest_path VARCHAR)"
            )
            con.execute(
                "CREATE TABLE parts (dataset_id VARCHAR, market VARCHAR, kind VARCHAR,"
                " symbol VARCHAR, year INTEGER, month INTEGER, path VARCHAR,"
                " content_hash VARCHAR, file_sha256 VARCHAR, rows BIGINT, first_time_ms BIGINT,"
                " last_time_ms BIGINT)"
            )
            for dsid in self.dataset_ids():
                m = self.manifest(dsid)
                con.execute(
                    "INSERT INTO datasets VALUES (?,?,?,?,?,?,?,?)",
                    [
                        dsid,
                        m.get("market"),
                        m.get("kind"),
                        m.get("symbol"),
                        m["row_count"],
                        m["time_range"]["from_ms"],
                        m["time_range"]["to_ms"],
                        f"imports/{dsid}/manifest.json",
                    ],
                )
                for d in m["parts"]:
                    con.execute(
                        "INSERT INTO parts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        [
                            dsid,
                            d["market"],
                            d["kind"],
                            d["symbol"],
                            d["year"],
                            d["month"],
                            d["path"],
                            d["content_hash"],
                            d["file_sha256"],
                            d["rows"],
                            d["first_time_ms"],
                            d["last_time_ms"],
                        ],
                    )
            for kind in LakeKind:
                files = sorted(
                    {
                        (self.data_dir / ip.part.path).as_posix()
                        for ip in self.index()
                        if ip.part.kind == kind
                    }
                )
                if not files:
                    continue
                listing = ", ".join("'" + f.replace("'", "''") + "'" for f in files)
                # Overlapping imports may list the same minute in several parts; the lake reader
                # resolves those. The views are for exploration, not for research reads.
                con.execute(
                    f"CREATE VIEW {kind} AS SELECT * FROM read_parquet([{listing}],"  # noqa: S608
                    " hive_partitioning = true, filename = true)"
                )
        finally:
            con.close()
        os.replace(tmp, path)
        return path
