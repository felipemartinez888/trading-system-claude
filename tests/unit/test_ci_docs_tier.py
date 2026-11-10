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
        # built at run time by test_preregistration.py, invisible to the literal scan
        ("docs/research/H1-v3.preregistration.DRAFT.yaml", True),
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
    cases = [
        ('- "**"\n- "docs/?.md"\n', "not supported"),
        ('- "**"\n- "!docs/**/*.md"\n', "not supported"),  # GitHub's '**/' is not emulated
        ('- "**"\n- "!**.md"\n', "not supported"),
        ('- "docs/**"\n', "must start"),
    ]
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


def test_classify_never_claims_a_verdict_it_cannot_know(monkeypatch: pytest.MonkeyPatch) -> None:
    assert tier.classify("0" * 40, "HEAD")[0] == "unknown"  # a new branch: GitHub's own diff
    assert tier.classify("not-a-commit", "HEAD")[0] == "unknown"
    many = "\n".join(f"docs/devlog/n{i}.md" for i in range(tier.GITHUB_FILTER_LIMIT + 1))
    monkeypatch.setattr(tier, "_git", lambda *args: many)
    assert tier.classify("a" * 40, "b" * 40)[0] == "too-large"
    assert tier.main(["classify", "a" * 40, "b" * 40]) == 1  # the docs-tier job fails
    monkeypatch.setattr(tier, "_git", lambda *args: "docs/devlog/n1.md\nsrc/x.py")
    assert tier.classify("a" * 40, "b" * 40)[0] == "full"


def test_the_marked_list_is_the_push_paths_filter_of_ci_yml() -> None:
    import yaml

    wf = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text("utf-8"))
    on = wf.get("on", wf.get(True))
    assert on["push"]["paths"] == PATS
    assert "paths" not in (on.get("pull_request") or {})  # pull requests are never filtered
    assert "workflow_dispatch" in on  # the unfiltered, uncancellable formal-run route (P1)


def test_formal_checkpoints_never_depend_on_a_filtered_push() -> None:
    import yaml

    for name in ("ci.yml", "ci-docs.yml"):
        wf = yaml.safe_load((REPO / ".github" / "workflows" / name).read_text("utf-8"))
        branches = wf.get("on", wf.get(True))["push"]["branches"]
        assert not any(b.startswith("acceptance/") for b in branches), name


def _guard_steps(name: str) -> list[dict[str, str]]:
    import yaml

    wf = yaml.safe_load((REPO / ".github" / "workflows" / name).read_text("utf-8"))
    steps = [s for job in wf["jobs"].values() for s in job["steps"]]
    return [
        {"name": s["name"], "run": s["run"]}
        for s in steps
        if s.get("name", "").startswith(("No private/signed", "No committed secrets"))
    ]


def test_the_docs_tier_runs_the_same_safety_guards_as_the_full_ci() -> None:
    full, docs = _guard_steps("ci.yml"), _guard_steps("ci-docs.yml")
    assert len(full) == 2
    assert docs == full
