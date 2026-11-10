"""Session-wide test hooks.

OD-2 (docs/tasks/M3-PB.md): the restart properties and the delayed-restart census record each
case's outcome (`paper_scenarios.report_outcome`). The end of the run prints RECOVERED,
SAFE_BLOCK (by reason) and FAIL separately for each group, so the blocking rate stays visible in
CI output, and writes them as JSON to $TSC_RESTART_REPORT when that variable is set.

Under pytest-xdist (CI proposal P2, experimental) the cases run in worker processes, so each
worker hands its counts to the controller at the end of its session, and the controller prints
their sum. A serial run has no workers and prints exactly what it printed before.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest

_KINDS = ("RECOVERED", "SAFE_BLOCK", "FAIL")
_WORKER_KEY = "od2_restart_outcomes"
#: counts handed over by pytest-xdist workers (empty in a serial run)
_FROM_WORKERS: dict[str, Counter[str]] = {}


def _summary(counts: Counter[str]) -> dict[str, Any]:
    by_kind = {k: sum(v for key, v in counts.items() if key.split(" ")[0] == k) for k in _KINDS}
    reasons = {key: v for key, v in sorted(counts.items()) if " " in key}
    return {"cases": sum(counts.values()), **by_kind, "by_reason": reasons}


def _local_report() -> dict[str, Counter[str]]:
    module = sys.modules.get("paper_scenarios")
    return getattr(module, "OUTCOME_REPORT", {}) or {}


def _merge(reports: Iterable[Mapping[str, Mapping[str, int]]]) -> dict[str, Counter[str]]:
    merged: dict[str, Counter[str]] = {}
    for report in reports:
        for group, counts in report.items():
            merged.setdefault(group, Counter()).update(dict(counts))
    return merged


def pytest_sessionfinish(session: pytest.Session) -> None:
    # a pytest-xdist worker: send this worker's counts to the controller with its other output
    workeroutput = getattr(session.config, "workeroutput", None)
    if workeroutput is not None:
        workeroutput[_WORKER_KEY] = {group: dict(c) for group, c in _local_report().items()}


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: Any) -> None:
    # pytest-xdist controller: a worker finished; keep its counts
    output = getattr(node, "workeroutput", None) or {}
    for group, counts in _merge([output.get(_WORKER_KEY) or {}]).items():
        _FROM_WORKERS.setdefault(group, Counter()).update(counts)


def pytest_terminal_summary(terminalreporter: Any) -> None:
    if hasattr(getattr(terminalreporter, "config", None), "workerinput"):
        return  # a pytest-xdist worker: its counts go to the controller, which reports the sum
    report = _merge([_local_report(), _FROM_WORKERS])
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
