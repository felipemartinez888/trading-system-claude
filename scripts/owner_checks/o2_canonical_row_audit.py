"""O-2: read-only audit of canonical 1m datasets against their cached source archives.

For every canonical bars_1m dataset (data/imports/*/manifest.json) and every data source archive
outside the 2026 holdout, it compares the row count recorded in the manifest with the data lines in
the cached ZIP, and lists source rows whose minute falls inside a gap reported by the canonical QA
(rows present at the source but missing from the lake). Nothing is written except the two report
files given on the command line. No network, no lake/Parquet access, no holdout archive opened.

usage:
  python -I scripts/owner_checks/o2_canonical_row_audit.py <repo_root> <out_json> <out_txt>
         [--only SUBSTRING]
"""

import io
import json
import sys
import time
import zipfile
from pathlib import Path

MINUTE = 60_000


def iso(ms: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))


def main() -> int:
    repo, out_json, out_txt = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    only = sys.argv[5] if len(sys.argv) > 5 and sys.argv[4] == "--only" else None
    if not (repo / "data" / "imports").is_dir() or not (repo / "pyproject.toml").is_file():
        print("refusing: not the repository root", file=sys.stderr)
        return 2
    for p in (out_json, out_txt):
        if p.exists():
            print(f"refusing to overwrite {p}", file=sys.stderr)
            return 2
    started = time.time()
    files: list[dict] = []
    dropped: list[dict] = []
    for manifest in sorted((repo / "data" / "imports").glob("*/manifest.json")):
        d = json.loads(manifest.read_text(encoding="utf-8"))
        if d.get("kind") != "bars_1m":
            continue
        qa = json.loads((manifest.parent / "qa_report.json").read_text(encoding="utf-8"))
        gaps = [(int(g["from_ms"]), int(g["to_ms"])) for g in qa.get("gaps", [])]
        for s in d.get("sources", []):
            path = s.get("cache_path", "")
            if s.get("role") != "data" or "-2026-" in path:
                continue  # data sources only; 2026 holdout archives are never opened
            if only and only not in path:
                continue
            zp = repo / "data" / "archive_cache" / path
            entry = {
                "symbol": d["symbol"],
                "dataset_id": d["dataset_id"][:16],
                "file": path,
                "recorded_rows": s.get("rows"),
            }
            if not zp.is_file():
                entry["status"] = "NOT_CACHED"
                files.append(entry)
                continue
            lines = 0
            malformed: list[dict] = []
            in_gaps: list[str] = []
            with zipfile.ZipFile(zp) as z:
                stream = io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf-8")
                for line_no, line in enumerate(stream, start=1):
                    c = line.strip().split(",")
                    if not c[0].isdigit():
                        continue
                    lines += 1
                    scale = 1000 if int(c[0]) > 10**14 else 1
                    t, close_t = int(c[0]) // scale, int(c[6]) // scale
                    if close_t != t + MINUTE - 1:
                        malformed.append({"line": line_no, "open": iso(t)})
                        continue
                    if any(lo <= t < hi for lo, hi in gaps):
                        in_gaps.append(iso(t))
                        dropped.append(
                            {"symbol": d["symbol"], "file": path, "line": line_no, "open": iso(t)}
                        )
            entry.update(
                zip_data_lines=lines,
                malformed_lines=malformed,
                source_rows_in_canonical_gaps=in_gaps,
            )
            # the importer records malformed lines as rejected, not as rows
            parsed = lines - len(malformed)
            if in_gaps or parsed != s.get("rows"):
                entry["status"] = "MISMATCH"
            else:
                entry["status"] = "OK_WITH_MALFORMED" if malformed else "OK"
            files.append(entry)
    mismatches = [f for f in files if f["status"] not in ("OK", "OK_WITH_MALFORMED")]
    explained = [f for f in files if f["status"] == "OK_WITH_MALFORMED"]
    report = {
        "check": "O-2 canonical row audit (read-only)",
        "started_utc": iso(int(started * 1000)),
        "seconds": round(time.time() - started, 1),
        "python": sys.version.split()[0],
        "files_checked": len(files),
        "mismatching_files": len(mismatches),
        "files_ok_with_malformed_lines": [f["file"] for f in explained],
        "source_rows_missing_from_canonical": dropped,
        "files": files,
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    text = [
        f"files checked: {len(files)}; mismatching files: {len(mismatches)}; "
        f"source rows missing from canonical: {len(dropped)}; "
        f"files OK with malformed lines: {len(explained)}; "
        f"{report['seconds']} s"
    ]
    for f in mismatches:
        text.append(
            f"{f['status']} {f['file']} recorded={f['recorded_rows']} "
            f"zip_lines={f.get('zip_data_lines')} "
            f"malformed={[m['line'] for m in f.get('malformed_lines', [])]} "
            f"rows_in_canonical_gaps={f.get('source_rows_in_canonical_gaps')}"
        )
    out_txt.write_text("\n".join(text) + "\n", encoding="utf-8")
    print("\n".join(text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
