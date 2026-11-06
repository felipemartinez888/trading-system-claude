"""C-1 verification: one source month (Binance archive ZIP) against one lake Parquet month.

Read-only. Writes exactly one new JSON report (refuses to overwrite). No network. Refuses 2026+ months
(the sealed holdout) and any destination that is not a lake directory. It lives outside `scripts/` on
purpose: `tests/unit/archive/test_arc_import_guard.py` forbids duckdb there.

Checks, per month (hypotheses are never hard-coded; findings are reported as found):
  S1 ZIP SHA-256 == the value in the .CHECKSUM file (and == --manifest-sha256 if given)
  S2 source rows: count, duplicates, ordering, close_time == open_time + 59_999, timestamp unit
  S3 source continuity: every minute of the month present in the SOURCE, else listed (genuine source gap)
  L1 lake rows: count, duplicates, first/last minute
  X1 source minutes missing in the lake / lake minutes absent from the source
  X2 every common minute: open, high, low, close, volume, quote_volume, trades, taker_buy_volume,
     taker_buy_quote_volume equal (floats compared exactly after float() of the source text)
Exit 0 = no finding at all, 1 = at least one finding, 2 = refused.

usage: python -I c1_verify.py <zip> <checksum_file> <lake_dir> <symbol> <YYYY-MM> <out_json> [--manifest-sha256 H]
"""

# ruff: noqa: E501, RUF005
from __future__ import annotations

import calendar
import csv
import hashlib
import io
import json
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MIN = 60_000
FIELDS = [
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "trades",
    "taker_buy_volume",
    "taker_buy_quote_volume",
]


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str]) -> int:
    if len(argv) < 6:
        print(__doc__)
        return 2
    zp, cs, lake, sym, ym, out = (
        Path(argv[0]),
        Path(argv[1]),
        Path(argv[2]),
        argv[3],
        argv[4],
        Path(argv[5]),
    )
    man = argv[7] if len(argv) > 7 and argv[6] == "--manifest-sha256" else None
    y, m = int(ym[:4]), int(ym[5:7])
    if y >= 2026:
        print("refusing: 2026+ is the sealed holdout", file=sys.stderr)
        return 2
    if out.exists():
        print(f"refusing to overwrite {out}", file=sys.stderr)
        return 2
    part_dir = (
        lake / "market=spot" / "kind=bars_1m" / f"symbol={sym}" / f"year={y}" / f"month={m:02d}"
    )
    parts = sorted(part_dir.glob("*.parquet"))
    if not zp.is_file() or not cs.is_file() or not parts:
        print("refusing: missing zip, checksum or lake part", file=sys.stderr)
        return 2

    findings: list[str] = []
    rep: dict[str, Any] = {
        "symbol": sym,
        "month": ym,
        "zip": zp.name,
        "lake_parts": [p.name for p in parts],
    }

    # S1
    sha = hashlib.sha256(zp.read_bytes()).hexdigest()
    want = cs.read_text(encoding="utf-8").split()[0].lower()
    rep["zip_sha256"], rep["checksum_file_sha256"] = sha, want
    if sha != want:
        findings.append("S1 ZIP SHA-256 differs from the CHECKSUM file")
    if man is not None and sha != man.lower():
        findings.append("S1 ZIP SHA-256 differs from the manifest source sha256")

    # S2/S3
    with zipfile.ZipFile(zp) as z:
        names = z.namelist()
        raw = z.read(names[0]).decode("utf-8")
    rep["zip_members"] = names
    rows: dict[int, list[str]] = {}
    dup = bad_close = unordered = header = 0
    prev = -1
    for rec in csv.reader(io.StringIO(raw)):
        if not rec:
            continue
        if not rec[0].isdigit():
            header += 1
            continue
        ot, ct = int(rec[0]), int(rec[6])
        unit = "us" if ot >= 10**15 else "ms"
        if unit == "us":
            ot, ct = ot // 1000, ct // 1000
        if ot in rows:
            dup += 1
        if ot < prev:
            unordered += 1
        prev = ot
        if ct != ot + MIN - 1:
            bad_close += 1
        rows[ot] = rec
    lo = int(datetime(y, m, 1, tzinfo=UTC).timestamp() * 1000)
    n_min = calendar.monthrange(y, m)[1] * 1440
    expected = set(range(lo, lo + n_min * MIN, MIN))
    out_of_month = sorted(set(rows) - expected)
    src_missing = sorted(expected - set(rows))
    rep.update(
        source_rows=len(rows),
        expected_minutes=n_min,
        source_duplicates=dup,
        source_unordered=unordered,
        source_bad_close_time=bad_close,
        header_lines=header,
        source_out_of_month=[iso(t) for t in out_of_month],
        source_missing_minutes=[iso(t) for t in src_missing],
    )
    for k, v in (
        ("S2 duplicate open_time", dup),
        ("S2 close_time != open+59999", bad_close),
        ("S2 out-of-month rows", len(out_of_month)),
    ):
        if v:
            findings.append(f"{k}: {v}")
    if src_missing:
        findings.append(f"S3 SOURCE itself lacks {len(src_missing)} minute(s) (genuine source gap)")

    # L1 / X1 / X2
    import duckdb

    con = duckdb.connect()
    glob = (part_dir / "*.parquet").as_posix()
    cols = ", ".join(["open_time_ms"] + FIELDS)
    lrows = {
        r[0]: r[1:] for r in con.execute(f"select {cols} from read_parquet('{glob}')").fetchall()
    }
    one = con.execute(f"select count(*) from read_parquet('{glob}')").fetchone()
    assert one is not None
    n_lake = one[0]
    rep["lake_rows"], rep["lake_distinct_minutes"] = n_lake, len(lrows)
    if n_lake != len(lrows):
        findings.append("L1 lake has duplicate open_time rows")
    miss_in_lake = sorted(set(rows) - set(lrows))
    extra_in_lake = sorted(set(lrows) - set(rows))
    rep["source_minutes_missing_in_lake"] = [iso(t) for t in miss_in_lake]
    rep["lake_minutes_absent_in_source"] = [iso(t) for t in extra_in_lake]
    if miss_in_lake:
        findings.append(f"X1 {len(miss_in_lake)} source minute(s) missing in the lake")
    if extra_in_lake:
        findings.append(f"X1 {len(extra_in_lake)} lake minute(s) absent from the source")
    diffs = []
    for t in sorted(set(rows) & set(lrows)):
        r = rows[t]
        src = [
            float(r[1]),
            float(r[2]),
            float(r[3]),
            float(r[4]),
            float(r[5]),
            float(r[7]),
            float(r[8]),
            float(r[9]),
            float(r[10]),
        ]
        lk = [float(v) for v in lrows[t]]
        if src != lk:
            diffs.append({"minute": iso(t), "source": src, "lake": lk})
    rep["value_mismatches"] = len(diffs)
    rep["value_mismatch_examples"] = diffs[:5]
    if diffs:
        findings.append(f"X2 {len(diffs)} common minute(s) differ in value")
    rep["findings"] = findings
    out.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "findings": findings,
                "source_rows": len(rows),
                "lake_rows": n_lake,
                "missing_in_lake": rep["source_minutes_missing_in_lake"],
            },
            indent=2,
        )
    )
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
