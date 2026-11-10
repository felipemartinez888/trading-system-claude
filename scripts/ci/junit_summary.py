"""CI test reporting (operational-readiness CI proposal P0). Standard library only.

Reads the JUnit XML that pytest writes with ``--junitxml`` and never changes what was tested:

* ``summarize``: counts, suite time and the slowest tests, as Markdown for the job summary, plus a
  sorted outcome list (one ``[test id, outcome]`` pair per test case) that two runs can be
  compared with.
* ``compare``: two outcome lists must hold the same test ids with the same outcomes.
* ``compare-json``: two JSON files (for example the OD-2 restart reports) must be equal.
* ``compare-freeze``: two ``uv pip freeze`` listings must be equal, apart from named packages.

Every ``compare`` command exits 1 on a difference and 2 on a missing or unreadable input, so a
comparison can never pass by default.
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any

OUTCOMES = ("passed", "failed", "error", "skipped")


def parse(path: Path) -> dict[str, Any]:
    """Test cases and totals of one JUnit XML file (pytest's ``xunit2`` family)."""
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    cases = []
    for case in root.iter("testcase"):
        tags = {child.tag for child in case}
        if "error" in tags:
            outcome = "error"
        elif "failure" in tags:
            outcome = "failed"
        elif "skipped" in tags:
            outcome = "skipped"
        else:
            outcome = "passed"
        test_id = f"{case.get('classname', '')}::{case.get('name', '')}"
        cases.append({"id": test_id, "outcome": outcome, "time": float(case.get("time") or 0)})
    counts = Counter(c["outcome"] for c in cases)
    return {
        "cases": cases,
        "counts": {k: counts.get(k, 0) for k in OUTCOMES},
        "suite_time_s": round(sum(float(s.get("time") or 0) for s in suites), 3),
    }


def summarize(junit: Path, label: str, outcomes_out: Path | None, top: int) -> str:
    report = parse(junit)
    cases = report["cases"]
    if outcomes_out is not None:
        payload = {
            "label": label,
            "tests": len(cases),
            "counts": report["counts"],
            "outcomes": sorted([c["id"], c["outcome"]] for c in cases),
        }
        outcomes_out.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", "utf-8")
    c = report["counts"]
    lines = [
        f"### Test report: {label}",
        "",
        f"- test cases: {len(cases)} (passed {c['passed']}, failed {c['failed']}, "
        f"errors {c['error']}, skipped {c['skipped']})",
        f"- pytest suite time: {report['suite_time_s']:.1f} s",
        "",
        f"| Slowest {top} | s |",
        "|---|---|",
    ]
    for case in sorted(cases, key=lambda x: -x["time"])[:top]:
        lines.append(f"| `{case['id']}` | {case['time']:.1f} |")
    return "\n".join(lines) + "\n"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text("utf-8"))


def compare_outcomes(a: Path, b: Path, limit: int = 50) -> tuple[bool, list[str]]:
    left = Counter(tuple(x) for x in _load_json(a)["outcomes"])
    right = Counter(tuple(x) for x in _load_json(b)["outcomes"])
    if left == right:
        return True, [f"identical: {sum(left.values())} test cases"]
    only_a = sorted((left - right).elements())
    only_b = sorted((right - left).elements())
    out = [f"{len(only_a)} only in {a.name}, {len(only_b)} only in {b.name}"]
    out += [f"- {a.name}: {i} {o}" for i, o in only_a[:limit]]
    out += [f"+ {b.name}: {i} {o}" for i, o in only_b[:limit]]
    return False, out


def _freeze(path: Path, ignore: set[str]) -> dict[str, str]:
    out = {}
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split("==")[0].split(" @ ")[0].strip().lower().replace("_", "-")
        if name not in ignore:
            out[name] = line
    return out


def compare_freeze(a: Path, b: Path, ignore: set[str]) -> tuple[bool, list[str]]:
    left, right = _freeze(a, ignore), _freeze(b, ignore)
    diff = sorted(
        f"{name}: {left.get(name, '<absent>')} vs {right.get(name, '<absent>')}"
        for name in set(left) | set(right)
        if left.get(name) != right.get(name)
    )
    return (not diff), (diff or [f"identical: {len(left)} packages"])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("summarize")
    s.add_argument("junit", type=Path)
    s.add_argument("--label", required=True)
    s.add_argument("--outcomes-out", type=Path)
    s.add_argument("--summary-out", type=Path)
    s.add_argument("--top", type=int, default=20)
    for name in ("compare", "compare-json", "compare-freeze"):
        c = sub.add_parser(name)
        c.add_argument("a", type=Path)
        c.add_argument("b", type=Path)
        if name == "compare-freeze":
            c.add_argument("--ignore", action="append", default=[])
    args = p.parse_args(argv)
    if args.cmd == "summarize":
        if not args.junit.is_file():
            print(f"no JUnit report at {args.junit} (pytest did not run or did not finish)")
            return 0  # reporting only: the pytest step itself decides the job result
        text = summarize(args.junit, args.label, args.outcomes_out, args.top)
        print(text)
        if args.summary_out is not None:
            with args.summary_out.open("a", encoding="utf-8") as fh:
                fh.write(text)
        return 0
    for path in (args.a, args.b):
        if not path.is_file():
            print(f"missing input: {path}")
            return 2
    try:
        if args.cmd == "compare":
            ok, lines = compare_outcomes(args.a, args.b)
        elif args.cmd == "compare-json":
            ok = _load_json(args.a) == _load_json(args.b)
            lines = ["identical"] if ok else [f"{args.a.name} != {args.b.name}"]
        else:
            ignore = {n.lower().replace("_", "-") for n in args.ignore}
            ok, lines = compare_freeze(args.a, args.b, ignore)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"unreadable input: {type(exc).__name__}: {exc}")
        return 2
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
