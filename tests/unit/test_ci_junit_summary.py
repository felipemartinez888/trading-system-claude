"""CI test reporting script (CI proposal P0). The JUnit files are written here."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parents[2]


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"ci_{name}", REPO / "scripts" / "ci" / f"{name}.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


junit = _load("junit_summary")

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="5" time="12.5">
<testcase classname="tests.unit.a" name="test_ok" time="1.0"/>
<testcase classname="tests.unit.a" name="test_slow[x]" time="9.0"/>
<testcase classname="tests.unit.b" name="test_skip" time="0.0"><skipped message="no"/></testcase>
<testcase classname="tests.unit.b" name="test_bad" time="2.0"><failure message="boom"/></testcase>
<testcase classname="tests.unit.c" name="test_err" time="0.5"><error message="setup"/></testcase>
</testsuite></testsuites>
"""


def _write_junit(tmp_path: Path, text: str = JUNIT, name: str = "j.xml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_junit_summary_counts_every_outcome_and_lists_the_slowest(tmp_path: Path) -> None:
    out = tmp_path / "o.json"
    text = junit.summarize(_write_junit(tmp_path), "serial (linux)", out, top=2)
    assert "test cases: 5 (passed 2, failed 1, errors 1, skipped 1)" in text
    assert "pytest suite time: 12.5 s" in text
    assert text.index("test_slow[x]") < text.index("test_bad")  # slowest first
    assert "test_ok" not in text  # only the top 2
    data = json.loads(out.read_text("utf-8"))
    assert data["tests"] == 5
    assert ["tests.unit.b::test_bad", "failed"] in data["outcomes"]
    assert data["outcomes"] == sorted(data["outcomes"])


def test_outcome_comparison_requires_the_same_tests_with_the_same_outcomes(tmp_path: Path) -> None:
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    junit.summarize(_write_junit(tmp_path, name="a.xml"), "a", a, 1)
    junit.summarize(_write_junit(tmp_path, name="b.xml"), "b", b, 1)
    assert junit.main(["compare", str(a), str(b)]) == 0
    changed = JUNIT.replace(
        'name="test_ok" time="1.0"/>', 'name="test_ok" time="1.0"><skipped/></testcase>'
    )
    junit.summarize(_write_junit(tmp_path, changed, "c.xml"), "c", b, 1)
    ok, lines = junit.compare_outcomes(a, b)
    assert not ok
    assert any("test_ok passed" in line for line in lines)
    assert junit.main(["compare", str(a), str(b)]) == 1
    err_case = '<testcase classname="tests.unit.c" name="test_err" time="0.5">'
    missing = JUNIT.replace(err_case + '<error message="setup"/></testcase>', "")
    assert missing != JUNIT
    junit.summarize(_write_junit(tmp_path, missing, "d.xml"), "d", b, 1)
    assert junit.main(["compare", str(a), str(b)]) == 1  # a test that did not run is a difference


def test_comparisons_fail_closed_on_missing_or_unreadable_input(tmp_path: Path) -> None:
    a = tmp_path / "a.json"
    a.write_text("{}", "utf-8")
    assert junit.main(["compare", str(a), str(tmp_path / "absent.json")]) == 2
    assert junit.main(["compare", str(a), str(a)]) == 2  # no "outcomes" key: no verdict
    assert junit.main(["compare-json", str(a), str(tmp_path / "absent.json")]) == 2
    # summarize is reporting only: a missing report is noted, never a pass or a failure
    assert junit.main(["summarize", str(tmp_path / "none.xml"), "--label", "x"]) == 0


def test_freeze_comparison_ignores_only_the_named_packages(tmp_path: Path) -> None:
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("numpy==2.3.0\npytest==8.4.0\n", "utf-8")
    b.write_text("numpy==2.3.0\npytest==8.4.0\npytest-xdist==3.8.0\nexecnet==2.1.1\n", "utf-8")
    ignore = ["--ignore", "pytest-xdist", "--ignore", "execnet"]
    assert junit.main(["compare-freeze", str(a), str(b), *ignore]) == 0
    b.write_text("numpy==2.3.1\npytest==8.4.0\npytest-xdist==3.8.0\n", "utf-8")
    ok, lines = junit.compare_freeze(a, b, {"pytest-xdist", "execnet"})
    assert not ok
    assert lines == ["numpy: numpy==2.3.0 vs numpy==2.3.1"]


def _outcomes(path: Path, pairs: list[list[str]]) -> Path:
    path.write_text(json.dumps({"outcomes": pairs}), "utf-8")
    return path


def test_outcome_comparison_counts_duplicates_and_refuses_empty_reports(tmp_path: Path) -> None:
    one = _outcomes(tmp_path / "one.json", [["t::a", "passed"]])
    two = _outcomes(tmp_path / "two.json", [["t::a", "passed"], ["t::a", "passed"]])
    assert junit.main(["compare", str(one), str(two)]) == 1  # a test run twice is a difference
    empty = _outcomes(tmp_path / "empty.json", [])
    assert junit.main(["compare", str(empty), str(empty)]) == 1  # 0 cases never pass
    assert junit.main(["compare", str(one), str(one)]) == 0


def test_json_comparison_fails_on_any_difference(tmp_path: Path) -> None:
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    report = {"delayed restart census": {"cases": 60, "RECOVERED": 45, "SAFE_BLOCK": 15}}
    a.write_text(json.dumps(report), "utf-8")
    b.write_text(json.dumps(report, indent=2), "utf-8")  # same content, other layout
    assert junit.main(["compare-json", str(a), str(b)]) == 0
    report["delayed restart census"]["RECOVERED"] = 44
    b.write_text(json.dumps(report), "utf-8")
    assert junit.main(["compare-json", str(a), str(b)]) == 1


def test_freeze_names_are_normalized_and_direct_references_are_kept(tmp_path: Path) -> None:
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("Foo_Bar==1.0\ntsclaude @ file:///work\n", "utf-8")
    b.write_text("foo-bar==1.0\ntsclaude @ file:///work\n", "utf-8")
    ok, _ = junit.compare_freeze(a, b, set())
    assert not ok  # the line text differs (name spelling), so it is reported, not hidden
    b.write_text("Foo_Bar==1.0\ntsclaude @ file:///elsewhere\n", "utf-8")
    ok, lines = junit.compare_freeze(a, b, set())
    assert not ok
    assert lines == ["tsclaude: tsclaude @ file:///work vs tsclaude @ file:///elsewhere"]
    assert junit.compare_freeze(a, a, {"foo-bar"})[1] == ["identical: 1 packages"]
