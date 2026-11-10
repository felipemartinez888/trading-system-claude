"""Docs-only CI tier (CI proposal P3): which pushes may skip the full CI. Fail-closed guard: a doc
that code, tests, scripts or configs refer to must always run the full CI."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ci_docs_tier", REPO / "scripts/ci/docs_tier.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tier = _load()
PATS = tier.patterns()


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True, cwd=REPO
    ).stdout
    return out.split()


def test_every_doc_that_something_reads_runs_the_full_ci() -> None:
    docs = tier.tracked_docs(_tracked())
    referenced = tier.referenced_docs(docs)
    assert "docs/MVP_USER_GUIDE.md" in referenced  # the scan works (tests read the guide)
    missing = [d for d in referenced if not tier.runs_full_ci(d, PATS)]
    assert missing == [], (
        "these docs are read by code, tests, scripts or configs but a docs-only push to them "
        f"would skip the full CI; add them to the docs-tier patterns in ci.yml: {missing}"
    )


@pytest.mark.parametrize(
    ("path", "full"),
    [
        ("src/tsclaude/sim/paper.py", True),
        ("tests/unit/test_contracts.py", True),
        ("configs/risk.yaml", True),
        (".github/workflows/ci-docs.yml", True),
        ("scripts/ci/docs_tier.py", True),
        ("frontend/README.md", True),  # only root *.md and docs/** can be docs-only
        ("pyproject.toml", True),
        ("docs/research/H1_DECISION_PACKAGE.md", True),
        ("docs/research/evidence/new.json", True),
        ("docs/tasks/M3-PB.md", True),
        ("docs/EVALUATION_PROTOCOL.md", True),
        ("README.md", True),
        ("docs/MVP_TASK_QUEUE.md", False),
        ("docs/MVP_OVERNIGHT_REPORT.md", False),
        ("docs/devlog/2026-11-10-operational-readiness-t1-t4.md", False),
        ("docs/devlog/metrics.csv", False),
        ("NOTES.md", False),
    ],
)
def test_classification_of_sample_paths(path: str, full: bool) -> None:
    assert tier.runs_full_ci(path, PATS) is full


def test_pattern_rules_follow_github_last_match_semantics() -> None:
    pats = ["**", "!docs/**", "docs/keep/**", "!docs/keep/skip.md"]
    assert tier.runs_full_ci("a/b.py", pats)
    assert not tier.runs_full_ci("docs/x.md", pats)
    assert tier.runs_full_ci("docs/keep/y.md", pats)
    assert not tier.runs_full_ci("docs/keep/skip.md", pats)
    assert tier.runs_full_ci("x.md", ["**", "!docs/**"])
    assert not tier.runs_full_ci("x.md", ["**", "!*.md"])
    assert tier.runs_full_ci("sub/x.md", ["**", "!*.md"])  # '*' never crosses '/'


def test_unsupported_pattern_forms_are_refused(tmp_path: Path) -> None:
    bad = tmp_path / "ci.yml"
    cases = [('- "**"\n- "docs/?.md"\n', "not supported"), ('- "docs/**"\n', "must start")]
    for body, message in cases:
        bad.write_text(f"{tier.BEGIN}\n{body}{tier.END}\n", "utf-8")
        with pytest.raises(ValueError, match=message):
            tier.patterns(bad)


def test_the_docs_workflow_only_triggers_on_docs_and_has_its_own_group() -> None:
    text = (REPO / ".github" / "workflows" / "ci-docs.yml").read_text("utf-8")
    assert 'paths:\n      - "docs/**"\n      - "*.md"\n' in text
    assert "group: ci-docs-${{ github.ref }}" in text
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    assert 'group: "ci-${{ github.ref }}-' in ci  # ci.yml's groups never start with ci-docs-


def test_classify_reports_a_new_branch_or_a_missing_base_as_full() -> None:
    assert tier.classify("0" * 40, "HEAD")[0] == "full"
    assert tier.classify("not-a-commit", "HEAD")[0] == "full"
