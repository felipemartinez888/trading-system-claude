"""Owner evidence script: it gathers evidence from its own checkout (operational-readiness review
R-03, task T4).

`scripts/owner_checks/mvp_evidence.ps1` defaulted ``-Repo`` to an older checkout
(``tsc-ingestion-bounded``). The user guide also ran an untracked copy, so with the defaults the
evidence came from other code. PowerShell is not available in CI's Linux job, so these checks read
the script text. The Windows behaviour is the owner's run.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "owner_checks" / "mvp_evidence.ps1"
GUIDE = REPO / "docs" / "MVP_USER_GUIDE.md"


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_the_default_repository_is_the_scripts_own_checkout() -> None:
    text = _script()
    assert re.search(r"\[string\]\$Repo = ''", text), "no fixed checkout as the -Repo default"
    assert "tsc-ingestion-bounded" not in text
    assert "Join-Path $PSScriptRoot '..\\..'" in text  # scripts\owner_checks -> repository root


def test_a_copy_outside_a_checkout_is_refused_before_anything_is_written() -> None:
    text = _script()
    refuse = text.index("is not a Trading System Claude checkout")
    assert "pyproject.toml" in text[:refuse]
    first_write = text.index("New-Item -ItemType Directory -Path $out")
    first_cd = text.index("Set-Location -LiteralPath $Repo")
    assert refuse < first_write < first_cd


def test_the_guide_runs_the_tracked_script_and_keeps_trial_records_optional() -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    section = guide[guide.index("## 4. One-command evidence run") :]
    section = section[: section.index("\n## ", 5)]
    commands = re.findall(r"```powershell\n(.*?)```", section, flags=re.DOTALL)
    assert commands
    run = "\n".join(commands)
    assert r"-File scripts\owner_checks\mvp_evidence.ps1" in run
    assert r"tsc_runs\mvp_evidence.ps1" not in run  # the untracked copy is not run
    assert "-SkipBacktest" in run  # the only step with permanent trial records is opt-in
