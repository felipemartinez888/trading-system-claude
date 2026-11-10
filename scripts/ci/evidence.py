"""Exact-commit CI evidence check (operational-readiness CI proposal P1).

Usage: python scripts/ci/evidence.py <commit> [--repo owner/name] [--no-logs]

A commit has CI evidence only through a run of the CI workflow (``.github/workflows/ci.yml``)
that:

* ran on exactly that commit (``head_sha``), from a ``push`` or a ``workflow_dispatch``;
  a ``pull_request`` run tests a merge commit, not the branch commit, and never counts;
* completed with conclusion ``success`` (cancelled, failed, skipped or running never count);
* has every job of the full acceptance set (``REQUIRED_JOBS``) with conclusion ``success`` in
  its latest attempt; a job that was skipped, for example in a benchmark-only or a docs-only
  run, does not count;
* has no earlier attempt that ended in ``failure`` (or another unsuccessful conclusion other
  than ``cancelled``). A rerun that turned a failure green is listed and refused, unless
  ``--allow-rerun-after-failure`` is given: whether it counts is the owner's call.

Exit 0 when at least one run qualifies, 1 when none does, 2 when the GitHub API cannot be read
(fail closed: an unreadable answer is never evidence). Read-only: it calls ``gh api`` and never
reruns, cancels or changes anything.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from typing import Any

WORKFLOW_PATH = ".github/workflows/ci.yml"
EVIDENCE_EVENTS = ("push", "workflow_dispatch")
REQUIRED_JOBS = (
    "python (ubuntu-latest)",
    "python (windows-latest)",
    "m2 cross-os gate (ubuntu-latest)",
    "m2 cross-os gate (windows-latest)",
    "frontend (ubuntu-latest)",
    "frontend (windows-latest)",
    "safety guards",
)
_PYTEST_LINE = re.compile(r"\b(\d+) passed\b[^\n]*\bin [\d.]+s")
_SHA = re.compile(r"^[0-9a-f]{40}$")


class ApiError(RuntimeError):
    """The GitHub API could not be read; the check fails closed."""


Api = Callable[[str], Any]
Logs = Callable[[int], str]


def gh_api(path: str) -> Any:
    proc = subprocess.run(
        ["gh", "api", path], capture_output=True, text=True, encoding="utf-8", check=False
    )
    if proc.returncode != 0:
        raise ApiError(f"gh api {path}: {proc.stderr.strip() or proc.returncode}")
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise ApiError(f"gh api {path}: not JSON ({exc})") from exc


def gh_job_log(repo: str) -> Logs:
    def fetch(job_id: int) -> str:
        proc = subprocess.run(
            ["gh", "api", f"repos/{repo}/actions/jobs/{job_id}/logs"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return proc.stdout if proc.returncode == 0 else ""

    return fetch


def check_run(run: dict[str, Any], jobs: list[dict[str, Any]]) -> list[str]:
    """Reasons the run is not evidence (empty: it is)."""
    reasons = []
    if run.get("event") not in EVIDENCE_EVENTS:
        reasons.append(f"event {run.get('event')!r} is not evidence for a branch commit")
    if run.get("status") != "completed":
        reasons.append(f"status {run.get('status')!r}")
    if run.get("conclusion") != "success":
        reasons.append(f"conclusion {run.get('conclusion')!r}")
    by_name = {j.get("name"): j for j in jobs}
    for name in REQUIRED_JOBS:
        job = by_name.get(name)
        if job is None:
            reasons.append(f"job {name!r} missing")
        elif job.get("conclusion") != "success":
            reasons.append(f"job {name!r} {job.get('conclusion')!r}")
    return reasons


def pytest_total(log: str) -> str | None:
    found = _PYTEST_LINE.findall(log)
    return f"{found[-1]} passed" if found else None


def m2_passed(log: str) -> int:
    return sum(1 for line in log.splitlines() if " PASSED" in line and "::test_golden" in line)


def earlier_attempts(run: dict[str, Any], api: Api, repo: str) -> list[tuple[int, str | None]]:
    """(attempt, conclusion) of every attempt before the latest one."""
    latest = int(run.get("run_attempt") or 1)
    out = []
    for k in range(1, latest):
        info = api(f"repos/{repo}/actions/runs/{run['id']}/attempts/{k}")
        out.append((k, info.get("conclusion")))
    return out


def evaluate(
    sha: str, repo: str, api: Api, logs: Logs | None, allow_rerun_after_failure: bool = False
) -> tuple[int, list[str]]:
    if not _SHA.match(sha):
        return 2, [f"not a full 40-character commit id: {sha!r}"]
    try:
        listing = api(f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100")
        runs = [
            r
            for r in listing.get("workflow_runs", [])
            if r.get("head_sha") == sha and r.get("path") == WORKFLOW_PATH
        ]
        out: list[str] = [f"commit {sha}: {len(runs)} CI run(s)"]
        qualified = 0
        for run in sorted(runs, key=lambda r: r.get("created_at") or ""):
            attempt = int(run.get("run_attempt") or 1)
            jobs = api(
                f"repos/{repo}/actions/runs/{run['id']}/attempts/{attempt}/jobs?per_page=100"
            ).get("jobs", [])
            reasons = check_run(run, jobs)
            failed_before = [
                (k, c)
                for k, c in earlier_attempts(run, api, repo)
                if c not in ("success", "cancelled")
            ]
            if failed_before and not allow_rerun_after_failure:
                reasons.append(
                    "earlier attempt(s) "
                    + ", ".join(f"{k} {c}" for k, c in failed_before)
                    + " (a rerun to green counts only with --allow-rerun-after-failure)"
                )
            head = (
                f"run {run['id']} attempt {attempt} ({run.get('event')}, {run.get('status')}, "
                f"{run.get('conclusion')}, created {run.get('created_at')})"
            )
            if reasons:
                out.append(f"NOT EVIDENCE {head}: " + "; ".join(reasons))
                continue
            qualified += 1
            out.append(f"EVIDENCE {head}")
            for k, c in failed_before:
                out.append(f"  WARNING: attempt {k} of this run ended {c!r}; accepted by option")
            for job in sorted(jobs, key=lambda j: str(j.get("name"))):
                detail = ""
                if logs is not None and str(job.get("name", "")).startswith("python ("):
                    detail = f": {pytest_total(logs(int(job['id']))) or 'total not found in log'}"
                elif logs is not None and str(job.get("name", "")).startswith("m2 cross-os"):
                    detail = f": {m2_passed(logs(int(job['id'])))} golden tests PASSED"
                out.append(f"  {job.get('name')}: {job.get('conclusion')}{detail}")
    except ApiError as exc:
        return 2, [f"GitHub API not readable, no verdict: {exc}"]
    except Exception as exc:  # any surprise is "no verdict" (exit 2), never "no evidence" (1)
        return 2, [f"unexpected API answer, no verdict: {type(exc).__name__}: {exc}"]
    out.append(
        f"verdict: {'EVIDENCE' if qualified else 'NO EVIDENCE'} "
        f"({qualified} qualifying run(s) for exactly this commit)"
    )
    return (0 if qualified else 1), out


def _full_sha(ref: str) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ref


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Exact-commit CI evidence check (read-only)")
    p.add_argument("commit", help="commit id or local ref; resolved to the full id with git")
    p.add_argument("--repo", default="felipemartinez888/trading-system-claude")
    p.add_argument("--no-logs", action="store_true", help="skip reading job logs for totals")
    p.add_argument(
        "--allow-rerun-after-failure",
        action="store_true",
        help="count a run whose latest attempt passed after an earlier attempt failed",
    )
    args = p.parse_args(argv)
    code, lines = evaluate(
        _full_sha(args.commit),
        args.repo,
        gh_api,
        None if args.no_logs else gh_job_log(args.repo),
        allow_rerun_after_failure=args.allow_rerun_after_failure,
    )
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
