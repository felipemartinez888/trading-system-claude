"""Exact-commit CI evidence check (CI proposal P1). No network: the GitHub API is a recorded
fake."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SHA = "0123456789abcdef0123456789abcdef01234567"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"ci_{name}", REPO / "scripts" / "ci" / f"{name}.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


evidence = _load("evidence")


def _job(name: str, conclusion: str = "success", job_id: int = 1) -> dict[str, Any]:
    return {"name": name, "conclusion": conclusion, "id": job_id}


def _full_jobs(**override: str) -> list[dict[str, Any]]:
    return [_job(n, override.get(n, "success"), i) for i, n in enumerate(evidence.REQUIRED_JOBS, 1)]


def _api(runs: list[dict[str, Any]], jobs: dict[int, list[dict[str, Any]]]) -> Any:
    def call(path: str) -> Any:
        if path.startswith("repos/o/r/actions/runs?head_sha="):
            assert f"head_sha={SHA}" in path
            return {"workflow_runs": runs}
        for run_id, js in jobs.items():
            if f"/runs/{run_id}/attempts/" in path:
                return {"jobs": js}
        raise AssertionError(path)

    return call


def _run(run_id: int, **kw: Any) -> dict[str, Any]:
    base = {
        "id": run_id,
        "head_sha": SHA,
        "path": ".github/workflows/ci.yml",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "run_attempt": 1,
        "created_at": f"2026-11-10T00:00:{run_id:02d}Z",
    }
    return {**base, **kw}


def test_evidence_needs_a_complete_successful_run_of_exactly_that_commit() -> None:
    log = {
        1: "== 2365 passed, 1 skipped in 900.0s ==",
        3: "x::test_golden_a PASSED\nx::test_golden_b PASSED",
    }
    code, lines = evidence.evaluate(
        SHA, "o/r", _api([_run(1)], {1: _full_jobs()}), lambda job_id: log.get(job_id, "")
    )
    assert code == 0
    assert lines[-1].startswith("verdict: EVIDENCE")
    assert any("python (ubuntu-latest): success: 2365 passed" in line for line in lines)
    assert any(
        "m2 cross-os gate (ubuntu-latest): success: 2 golden tests PASSED" in line for line in lines
    )


@pytest.mark.parametrize(
    ("run", "jobs", "reason"),
    [
        (_run(2, conclusion="cancelled"), _full_jobs(), "conclusion 'cancelled'"),
        (_run(2, status="in_progress", conclusion=None), _full_jobs(), "status 'in_progress'"),
        (_run(2, event="pull_request"), _full_jobs(), "event 'pull_request'"),
        # a benchmark-only or docs-only run: the full acceptance jobs were skipped or absent
        (_run(2), _full_jobs(**{"frontend (windows-latest)": "skipped"}), "'skipped'"),
        (_run(2), _full_jobs()[:-1], "job 'safety guards' missing"),
    ],
)
def test_anything_short_of_a_full_success_is_not_evidence(
    run: dict[str, Any], jobs: list[dict[str, Any]], reason: str
) -> None:
    code, lines = evidence.evaluate(SHA, "o/r", _api([run], {2: jobs}), None)
    assert code == 1
    assert any(line.startswith("NOT EVIDENCE") and reason in line for line in lines)
    assert lines[-1].startswith("verdict: NO EVIDENCE")


def test_runs_of_other_commits_or_workflows_are_ignored_and_errors_give_no_verdict() -> None:
    other = [_run(4, head_sha="f" * 40), _run(5, path=".github/workflows/other.yml")]
    code, lines = evidence.evaluate(SHA, "o/r", _api(other, {}), None)
    assert code == 1
    assert lines[0].endswith(": 0 CI run(s)")

    def broken(path: str) -> Any:
        raise evidence.ApiError("HTTP 502")

    assert evidence.evaluate(SHA, "o/r", broken, None)[0] == 2
    assert evidence.evaluate("abc123", "o/r", broken, None)[0] == 2  # short ids are resolved first


def _attempts_api(
    run: dict[str, Any],
    jobs_by_attempt: dict[int, list[dict[str, Any]]],
    conclusions: dict[int, str],
) -> Any:
    def call(path: str) -> Any:
        if "/actions/runs?head_sha=" in path:
            return {"workflow_runs": [run]}
        for k, js in jobs_by_attempt.items():
            if path.startswith(f"repos/o/r/actions/runs/{run['id']}/attempts/{k}/jobs"):
                return {"jobs": js}
        for k, c in conclusions.items():
            if path == f"repos/o/r/actions/runs/{run['id']}/attempts/{k}":
                return {"conclusion": c}
        raise AssertionError(path)

    return call


def test_the_latest_attempt_decides_and_a_rerun_after_a_failure_is_refused_by_default() -> None:
    run = _run(7, run_attempt=3)
    failing = _full_jobs(**{"python (windows-latest)": "failure"})
    api = _attempts_api(
        run, {1: failing, 2: failing, 3: _full_jobs()}, {1: "failure", 2: "failure"}
    )
    code, lines = evidence.evaluate(SHA, "o/r", api, None)
    assert code == 1
    assert any("earlier attempt(s) 1 failure, 2 failure" in line for line in lines)
    code, lines = evidence.evaluate(SHA, "o/r", api, None, allow_rerun_after_failure=True)
    assert code == 0
    assert any("attempt 3" in line and line.startswith("EVIDENCE") for line in lines)
    assert sum("WARNING: attempt" in line for line in lines) == 2


def test_a_rerun_of_a_cancelled_attempt_is_evidence_and_only_the_latest_jobs_count() -> None:
    run = _run(8, run_attempt=2)
    cancelled = _full_jobs(**{"python (ubuntu-latest)": "cancelled"})
    api = _attempts_api(run, {1: cancelled, 2: _full_jobs()}, {1: "cancelled"})
    assert evidence.evaluate(SHA, "o/r", api, None)[0] == 0
    # the latest attempt failing is never rescued by an earlier green one
    run = _run(9, run_attempt=2, conclusion="failure")
    api = _attempts_api(run, {1: _full_jobs(), 2: cancelled}, {1: "success"})
    assert evidence.evaluate(SHA, "o/r", api, None)[0] == 1


@pytest.mark.parametrize(
    "answer",
    [
        RuntimeError("socket closed"),  # not an ApiError: still no verdict
        [],  # a list where an object was expected
        {"workflow_runs": [{"head_sha": SHA, "path": ".github/workflows/ci.yml"}]},  # no id
    ],
)
def test_any_unexpected_answer_gives_no_verdict(answer: Any) -> None:
    def call(path: str) -> Any:
        if isinstance(answer, Exception):
            raise answer
        return answer

    code, lines = evidence.evaluate(SHA, "o/r", call, None)
    assert code == 2
    assert "no verdict" in lines[-1]
