"""Session-wide test hooks.

OD-2 (docs/tasks/M3-PB.md): the restart properties and the delayed-restart census record each
case's outcome (`paper_scenarios.report_outcome`). The end of the run prints RECOVERED,
SAFE_BLOCK (by reason) and FAIL separately for each group, so the blocking rate stays visible in
CI output, and writes them as JSON to $TSC_RESTART_REPORT when that variable is set.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_KINDS = ("RECOVERED", "SAFE_BLOCK", "FAIL")


def _summary(counts: Counter[str]) -> dict[str, Any]:
    by_kind = {k: sum(v for key, v in counts.items() if key.split(" ")[0] == k) for k in _KINDS}
    reasons = {key: v for key, v in sorted(counts.items()) if " " in key}
    return {"cases": sum(counts.values()), **by_kind, "by_reason": reasons}


def pytest_terminal_summary(terminalreporter: Any) -> None:
    module = sys.modules.get("paper_scenarios")
    report: dict[str, Counter[str]] = getattr(module, "OUTCOME_REPORT", {}) or {}
    if not report:
        return
    summaries = {group: _summary(report[group]) for group in sorted(report)}
    terminalreporter.write_sep("-", "OD-2 restart outcomes (gate cases)")
    for group, s in summaries.items():
        blocked = s["SAFE_BLOCK"] / s["cases"] if s["cases"] else 0.0
        terminalreporter.write_line(
            f"{group}: {s['cases']} cases, RECOVERED {s['RECOVERED']}, "
            f"SAFE_BLOCK {s['SAFE_BLOCK']} ({blocked:.0%}) {s['by_reason']}, FAIL {s['FAIL']}"
        )
    terminalreporter.write_line(
        "SAFE_BLOCK is a fail-closed stop: not recovery, not equivalence, not availability."
    )
    path = os.environ.get("TSC_RESTART_REPORT")
    if path:
        Path(path).write_text(json.dumps(summaries, indent=2, sort_keys=True), encoding="utf-8")
