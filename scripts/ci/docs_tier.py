"""Docs-only CI tier (CI proposal P3). Standard library only.

GitHub decides whether ``.github/workflows/ci.yml`` runs for a push with that workflow's
``on.push.paths`` list: the full CI runs when at least one pushed file matches it. The list
includes everything, then excludes ``docs/**`` and root ``*.md``, then includes back every doc
that code, tests, scripts or configs read. A push that touches only the remaining docs does not
start ci.yml at all, so it cannot cancel or replace a full run that is in progress.

This module reads that list from ci.yml (between the ``docs-tier patterns`` markers), applies
GitHub's rules for the pattern forms it allows (``**`` alone or at the end, ``*``, literals, a
leading ``!``; the last matching pattern decides; anything else is refused), and finds the docs
whose path or file name appears literally in other files. The guard test
``tests/unit/test_ci_docs_tier.py`` fails the full CI when such a doc is not included back.

Limit of the guard: it sees literal names only. A doc read through a path built at run time, or
by listing a folder, is invisible to it. That is why ``docs/research/**`` and ``docs/tasks/**``
are included back whole (tests build file names there), and why a new test that builds a doc
path elsewhere must add the doc, or its folder, to the list by hand.

``classify <before> <after>``: reports, for the docs-tier workflow, which pushed files would
start the full CI, and exits 1 for a push of more than 300 files, which GitHub's path filter
does not fully evaluate. It is a report: the decision itself is GitHub's, from ci.yml.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CI_YML = REPO / ".github" / "workflows" / "ci.yml"
BEGIN, END = "# docs-tier patterns: begin", "# docs-tier patterns: end"
#: where a reference to a doc can make it an input of the code or the tests
SCANNED = ("src", "tests", "scripts", "configs", "frontend", ".github", "preregistrations")
#: root files other than docs that tools read (pyproject.toml configures pytest, ruff, mypy)
SCANNED_ROOT_FILES = ("pyproject.toml", ".gitattributes", ".gitignore")
#: GitHub matches a path filter against the first 300 changed files only
GITHUB_FILTER_LIMIT = 300
#: files that only name docs as examples or as the pattern list itself (never read them)
_NOT_SCANNED = (CI_YML, REPO / "tests" / "unit" / "test_ci_docs_tier.py")
_ALLOWED = re.compile(r"^!?[A-Za-z0-9_./*-]+$")
_ZERO = "0" * 40


def patterns(ci_yml: Path = CI_YML) -> list[str]:
    """The ``on.push.paths`` entries of ci.yml, in order."""
    lines = ci_yml.read_text("utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == BEGIN)
    end = next(i for i, line in enumerate(lines) if line.strip() == END)
    out = []
    for line in lines[start + 1 : end]:
        item = line.strip()
        if not item.startswith("- "):
            continue
        value = item[2:].strip().strip('"').strip("'")
        body = value.lstrip("!")
        if (
            not _ALLOWED.match(value)
            or "***" in value
            or ("**" in body and body not in ("**",) and not body.endswith("/**"))
            or body.count("**") > 1
        ):
            # GitHub's '**/' and mid-pattern '**' rules are not emulated here: refuse them
            raise ValueError(f"pattern form not supported by docs_tier.py: {value!r}")
        out.append(value)
    if not out or out[0] != "**":
        raise ValueError("the list must start with '**' (everything runs the full CI)")
    return out


def _regex(pattern: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def runs_full_ci(path: str, pats: Sequence[str]) -> bool:
    """True when ``path`` matches the list (the last matching pattern decides)."""
    included = False
    for pat in pats:
        negative = pat.startswith("!")
        if _regex(pat.lstrip("!")).match(path):
            included = not negative
    return included


def tracked_docs(files: Iterable[str]) -> list[str]:
    return sorted(f for f in files if f.startswith("docs/") or ("/" not in f and f.endswith(".md")))


def referenced_docs(docs: Iterable[str], repo: Path = REPO) -> list[str]:
    """Docs whose path or file name appears literally in a scanned file (ci.yml and the guard
    test excluded: they list docs as patterns and examples). File names are matched alone, so
    this errs towards more docs."""
    texts = []
    for name in SCANNED_ROOT_FILES:
        f = repo / name
        if f.is_file():
            texts.append(f.read_text("utf-8", errors="ignore"))
    for root in SCANNED:
        base = repo / root
        if not base.exists():
            continue
        for p in base.rglob("*"):
            skip = {x.resolve() for x in _NOT_SCANNED}
            parts = set(p.parts)
            if (
                p.is_file()
                and p.resolve() not in skip
                and not parts & {"__pycache__", "node_modules", "dist"}
            ):
                try:
                    texts.append(p.read_text("utf-8", errors="ignore"))
                except OSError:
                    continue
    blob = "\n".join(texts)
    return [d for d in docs if d in blob or d.rsplit("/", 1)[-1] in blob]


def _git(*args: str) -> str:
    proc = subprocess.run(["git", *args], capture_output=True, text=True, check=False, cwd=REPO)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


def classify(before: str, after: str) -> tuple[str, list[str]]:
    if before in ("", _ZERO):
        return "unknown", [
            "new branch or no previous commit: GitHub compares against the parent of the oldest "
            "pushed commit; this report cannot tell whether ci.yml runs (see the Actions list)"
        ]
    try:
        # --no-renames: a rename is a deletion plus an addition, so both paths are classified
        files = _git("diff", "--name-only", "--no-renames", before, after).split()
    except RuntimeError as exc:
        return "unknown", [f"diff not available ({exc}): this report cannot classify the push"]
    if len(files) > GITHUB_FILTER_LIMIT:
        return "too-large", [
            f"{len(files)} changed files: GitHub matches only the first {GITHUB_FILTER_LIMIT}, "
            "so ci.yml may not run although it should. Dispatch a full CI run for this commit."
        ]
    pats = patterns()
    rows = [f"{'FULL' if runs_full_ci(f, pats) else 'docs'}  {f}" for f in files]
    verdict = "full" if any(r.startswith("FULL") for r in rows) else "docs"
    return verdict, rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Docs-only CI tier report (CI proposal P3)")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("classify")
    c.add_argument("before")
    c.add_argument("after")
    c.add_argument("--summary", type=Path)
    args = p.parse_args(argv)
    verdict, rows = classify(args.before, args.after)
    text = "\n".join(
        [
            "### Docs tier (CI proposal P3)",
            "",
            "Not evidence for code: only a full ci.yml run of a commit is (evidence.sh).",
            "",
            f"Pushed range {args.before[:12]}..{args.after[:12]}: "
            + {
                "full": "**ci.yml also runs** for this push (at least one file below is FULL).",
                "docs": "docs only: ci.yml does not run, and no full run is cancelled.",
                "unknown": "not classified here.",
                "too-large": "**too many files for GitHub's path filter.**",
            }[verdict],
            "",
            "```",
            *rows,
            "```",
        ]
    )
    print(text)
    if args.summary is not None:
        with args.summary.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 1 if verdict == "too-large" else 0


if __name__ == "__main__":
    sys.exit(main())
