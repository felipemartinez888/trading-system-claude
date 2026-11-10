"""The OD-2 restart-outcome report (tests/conftest.py) is the same whether the cases ran in one
process or in pytest-xdist workers (CI proposal P2). Runs the hooks with fakes; no xdist needed."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

CONFTEST = Path(__file__).resolve().parents[1] / "conftest.py"


def _fresh_conftest() -> ModuleType:
    spec = importlib.util.spec_from_file_location("od2_conftest_under_test", CONFTEST)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Reporter:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write_sep(self, sep: str, title: str) -> None:
        self.lines.append(f"[{title}]")

    def write_line(self, line: str) -> None:
        self.lines.append(line)


WORKER_A = {
    "delayed restart census": Counter({"RECOVERED": 30, "SAFE_BLOCK INSTRUMENT_GAP": 10}),
    "instant restart property": Counter({"RECOVERED": 22, "SAFE_BLOCK EMPTY_MINUTE": 2}),
}
WORKER_B = {"delayed restart census": Counter({"RECOVERED": 15, "SAFE_BLOCK INSTRUMENT_GAP": 5})}
ALL = {
    "delayed restart census": WORKER_A["delayed restart census"]
    + WORKER_B["delayed restart census"],
    "instant restart property": WORKER_A["instant restart property"],
}


def _scenarios(report: dict[str, Counter[str]]) -> ModuleType:
    module = ModuleType("paper_scenarios")
    module.OUTCOME_REPORT = report  # type: ignore[attr-defined]
    return module


def _summary(conftest: ModuleType, path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setenv("TSC_RESTART_REPORT", str(path))
    rep = _Reporter()
    conftest.pytest_terminal_summary(rep)
    return rep.lines


def test_workers_and_one_process_print_and_write_the_same_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # one process, as today: the module global holds every case
    serial = _fresh_conftest()
    monkeypatch.setitem(sys.modules, "paper_scenarios", _scenarios(ALL))
    serial_lines = _summary(serial, tmp_path / "serial.json", monkeypatch)
    assert serial_lines[0] == "[OD-2 restart outcomes (gate cases)]"
    assert serial_lines[1].startswith(
        "delayed restart census: 60 cases, RECOVERED 45, SAFE_BLOCK 15"
    )

    # two workers: each sends its own counts at the end of its session ...
    outputs = []
    for part in (WORKER_A, WORKER_B):
        worker = _fresh_conftest()
        monkeypatch.setitem(sys.modules, "paper_scenarios", _scenarios(part))
        config = SimpleNamespace(workeroutput={})
        worker.pytest_sessionfinish(SimpleNamespace(config=config))
        outputs.append(json.loads(json.dumps(config.workeroutput)))  # what crosses the channel
    # ... and the controller, which ran no case itself, prints their sum
    controller = _fresh_conftest()
    monkeypatch.delitem(sys.modules, "paper_scenarios")
    for out in outputs:
        controller.pytest_testnodedown(SimpleNamespace(workeroutput=out), None)
    parallel_lines = _summary(controller, tmp_path / "parallel.json", monkeypatch)

    assert parallel_lines == serial_lines
    assert (tmp_path / "parallel.json").read_bytes() == (tmp_path / "serial.json").read_bytes()


def test_no_cases_print_nothing_and_a_worker_without_output_adds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conftest = _fresh_conftest()
    monkeypatch.delitem(sys.modules, "paper_scenarios", raising=False)
    conftest.pytest_testnodedown(SimpleNamespace(workeroutput=None), "crashed")
    conftest.pytest_testnodedown(SimpleNamespace(), None)
    assert _summary(conftest, tmp_path / "r.json", monkeypatch) == []
    assert not (tmp_path / "r.json").exists()
    # a serial session (no workeroutput) sends nothing
    session: Any = SimpleNamespace(config=SimpleNamespace())
    conftest.pytest_sessionfinish(session)
    assert not hasattr(session.config, "workeroutput")
