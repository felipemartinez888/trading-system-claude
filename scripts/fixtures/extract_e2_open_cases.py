"""Extract the ADR-0015 regression fixture from cached Binance spot archives (M2-E2).

Read-only over the archives: each zip is opened for reading, its SHA-256 must equal the value
recorded in ADR-0015, and only the few CSV lines each case needs are copied, verbatim, with their
line numbers. For each documented bucket that is the 1m candle before the bucket, the bucket's 15
1m candles, and the native 15m candle. Nothing else is read into the output, no source file is
written, and the sealed 2026 holdout is refused (no 2026 archive is accepted, and every copied
line must open before 2026-01-01).

Usage (owner machine; NORMAL — no administrator; writes only the --out file):
    python -I scripts/fixtures/extract_e2_open_cases.py --cache-root <archive_cache>
        [--cache-root ...] --out tests/fixtures/archive/e2_open_exception.json

The output is deterministic: re-running it on the same archives gives the same bytes, which is
how the fixture can be re-verified independently.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

MS_PER_MINUTE = 60_000
HOLDOUT_START_MS = 1_767_225_600_000  # 2026-01-01T00:00:00Z

#: ADR-0015 source table (file name -> SHA-256 of the zip)
SOURCES = {
    "AVAXUSDT-1m-2025-12.zip": "6464c58b341850ccc7212f23d82b5604c74944c916c5258936c4f42a1736c1df",
    "AVAXUSDT-15m-2025-12.zip": "1a777574924bf8f055f7318cb39fea6459d6f039b29d161435c7a6973e1b2b36",
    "HBARUSDT-1m-2024-01.zip": "f75724733830d1521895a655b5e682280a3ebead1fb0540c2a1269c099852b3d",
    "HBARUSDT-15m-2024-01.zip": "843f84b2e6faa5fcf5b1618cd34c1f1efcc37dd6dae54cb3050bf8a57a123735",
    "HBARUSDT-1m-2025-12.zip": "3d2ffff772cfb4c8544c59d5865880784398b21a8196a64163d588af3965813c",
    "HBARUSDT-15m-2025-12.zip": "639d2e01a719186e12b3fb4b8013004520a2c4adb419376fdf2fd34b548154dc",
}

#: ADR-0015 case table: (symbol, month, bucket open UTC, 1m line of the bucket's first minute,
#: native 15m line)
CASES = [
    ("AVAXUSDT", "2025-12", "2025-12-07T03:45:00Z", 8866, 592),
    ("AVAXUSDT", "2025-12", "2025-12-29T23:15:00Z", 41716, 2782),
    ("AVAXUSDT", "2025-12", "2025-12-30T18:15:00Z", 42856, 2858),
    ("HBARUSDT", "2024-01", "2024-01-25T18:30:00Z", 35671, 2379),
    ("HBARUSDT", "2025-12", "2025-12-14T03:45:00Z", 18946, 1264),
]


def _ms(iso: str) -> int:
    return int(
        datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(UTC).timestamp() * 1000
    )


def _open_ms(field: str) -> int:
    v = int(field)
    return v // 1000 if v >= 10**15 else v  # 2025+ spot archives use microseconds


def _find(roots: list[Path], name: str) -> Path:
    hits = sorted({p.resolve() for r in roots for p in r.rglob(name) if p.is_file()})
    if not hits:
        raise SystemExit(f"{name}: not found under the given cache roots")
    return hits[0]


def _lines(path: Path, name: str) -> tuple[str, dict[int, tuple[int, str]]]:
    """(member name, open_ms -> (line number, raw line)) of the single CSV in a verified zip."""
    if name.split("-")[-2].startswith("2026") or "-2026-" in name:
        raise SystemExit(f"{name}: 2026 archives are sealed holdout data and are refused")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != SOURCES[name]:
        raise SystemExit(f"{name}: SHA-256 {digest} differs from ADR-0015 {SOURCES[name]}")
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        (member,) = zf.namelist()
        text = zf.read(member).decode("utf-8")
    out: dict[int, tuple[int, str]] = {}
    for line_no, line in enumerate(text.splitlines(), start=1):
        row = next(csv.reader([line]))
        if row and row[0].strip().isdigit():
            out[_open_ms(row[0].strip())] = (line_no, line)
    return member, out


def _take(index: dict[int, tuple[int, str]], t: int, what: str) -> dict[str, object]:
    if t >= HOLDOUT_START_MS:
        raise SystemExit(f"{what}: {t} is inside the sealed holdout")
    if t not in index:
        raise SystemExit(f"{what}: no line opens at {t}")
    line_no, line = index[t]
    return {"line": line_no, "csv": line}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache-root", action="append", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)

    files: dict[str, tuple[str, dict[int, tuple[int, str]]]] = {}
    for name in SOURCES:
        files[name] = _lines(_find(args.cache_root, name), name)

    cases = []
    for symbol, month, bucket_iso, first_line, native_line in CASES:
        m1, m15 = f"{symbol}-1m-{month}.zip", f"{symbol}-15m-{month}.zip"
        start = _ms(bucket_iso)
        minutes = [_take(files[m1][1], start + i * MS_PER_MINUTE, m1) for i in range(-1, 15)]
        native = _take(files[m15][1], start, m15)
        if minutes[1]["line"] != first_line or native["line"] != native_line:
            raise SystemExit(f"{symbol} {bucket_iso}: line numbers differ from ADR-0015")
        cases.append(
            {
                "id": f"{symbol}-{bucket_iso}",
                "symbol": symbol,
                "market": "spot",
                "bucket_open": bucket_iso,
                "bucket_open_ms": start,
                "derived_1m": {
                    "file": m1,
                    "csv_member": files[m1][0],
                    "sha256": SOURCES[m1],
                    "rows": minutes,  # the candle before the bucket, then its 15 minutes
                },
                "native_15m": {
                    "file": m15,
                    "csv_member": files[m15][0],
                    "sha256": SOURCES[m15],
                    "row": native,
                },
            }
        )
    doc = {
        "fixture": "e2_open_exception",
        "version": 1,
        "description": (
            "ADR-0015 regression fixture: verbatim CSV lines (with line numbers) cut from the "
            "verified Binance spot archives named here; extracted read-only by "
            "scripts/fixtures/extract_e2_open_cases.py"
        ),
        "cases": cases,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"wrote {args.out} ({len(cases)} cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
